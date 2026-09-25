"""A tiny stand-in for the AI gateway (Azure OpenAI chat completions shape).

It echoes the last user message, supports streaming, and records request headers so a
smoke test can assert what the real gateway would receive. For local wiring checks only.

    uvicorn scripts.fake_gateway:app --port 9100
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
SEEN: list[dict] = []


def _reply(body: dict) -> str:
    user = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), {})
    content = user.get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return f"echo: {content}"


@app.post("/openai/deployments/{deployment}/chat/completions")
async def chat(deployment: str, request: Request):
    body = await request.json()
    SEEN.append({"deployment": deployment, "headers": dict(request.headers), "stream": bool(body.get("stream"))})
    text = _reply(body)
    usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    base = {"id": "chatcmpl-fake", "created": int(time.time()), "model": deployment}
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
