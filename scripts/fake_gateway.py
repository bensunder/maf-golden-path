"""A tiny stand-in for the AI gateway (Azure OpenAI chat completions shape).

It echoes the last user message, supports streaming, and records request headers so a
smoke test can assert what the real gateway would receive. For local wiring checks only.

    uvicorn scripts.fake_gateway:app --port 9100
"""

from __future__ import annotations

import json
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
SEEN: list[dict] = []
JUDGED: list[bool] = []


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _reply(body: dict) -> str:
    user = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), {})
    return f"echo: {_text(user)}"


_REFUND = re.compile(r"refund\s+([A-Za-z]\d{4})\s+\$?(\d+(?:\.\d+)?)", re.IGNORECASE)


def _tool_step(body: dict) -> dict | None:
    """Deterministic 'model': 'refund A1002 $129' calls issue_refund when that tool is offered;
    after a tool result, it reports the result. Lets the smoke test drive approvals end to end.
    Judge prompts (agentkit's [agentkit-judge] marker) get a score: 5 if the graded response
    contains "Refunded" or "echo", else 2, so the smoke test can exercise the quality gate."""
    messages = body.get("messages", [])
    if messages and "[agentkit-judge]" in _text(messages[0]):
        graded = _text(messages[-1]).split("RESPONSE:", 1)[-1].split("TOOL RESULTS:", 1)[0]
        good = "Refunded" in graded or "echo" in graded
        JUDGED.append(good)
        return {"role": "assistant", "content": json.dumps({"score": 5 if good else 2, "reason": "fake judge"})}
    offered = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return {"role": "assistant", "content": f"tool said: {_text(last)}"}
    match = _REFUND.search(_text(last)) if last.get("role") == "user" else None
    if match and "issue_refund" in offered:
        args = {"order_id": match.group(1).upper(), "amount": float(match.group(2)), "reason": "smoke test"}
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{len(messages)}", "type": "function",
             "function": {"name": "issue_refund", "arguments": json.dumps(args)}}]}
    return None


@app.post("/openai/deployments/{deployment}/chat/completions")
async def chat(deployment: str, request: Request):
    body = await request.json()
    SEEN.append({"deployment": deployment, "headers": dict(request.headers), "stream": bool(body.get("stream"))})
    usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    base = {"id": "chatcmpl-fake", "created": int(time.time()), "model": deployment}
    step = _tool_step(body)
    if step is not None and not body.get("stream"):
        finish = "tool_calls" if step.get("tool_calls") else "stop"
        return JSONResponse({**base, "object": "chat.completion",
                             "choices": [{"index": 0, "message": step, "finish_reason": finish}], "usage": usage})
    text = _reply(body)
    if body.get("stream"):

        def chunks():
            for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
                delta = {**base, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}]}
                yield f"data: {json.dumps(delta)}\n\n"
            done = {**base, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return JSONResponse(
        {**base, "object": "chat.completion",
         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
         "usage": usage}
    )


@app.get("/_seen")
async def seen():
    return SEEN


@app.get("/_judged")
async def judged():
    return JUDGED
