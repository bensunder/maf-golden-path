"""AG-UI endpoint: any AG-UI client (CopilotKit, the agentkit web chat component, your own) can talk to
the agent, with human approvals as standard AG-UI *interrupts*.

    from agentkit.channels import AgUiChannel
    app = create_app(create_agent, channels=[AgUiChannel()])      # POST /v1/agui

Differences from MAF's generic AG-UI endpoint, on purpose:

* The server keeps the conversation (the agentkit session store, shared across replicas). Clients may
  send their whole history as AG-UI does, but only the newest user message is used, so a browser can't
  rewrite what the agent said earlier.
* The thread belongs to the signed-in user (Easy Auth); another user reusing the thread id gets 403.
* A paused run ends with ``RUN_FINISHED`` + ``outcome: {type: "interrupt", interrupts: [...]}``: one
  interrupt per pending approval (``id`` = approval id). The client resumes with
  ``resume: [{interrupt_id, status: "resolved", payload: {approved, comment}}]``; ``cancelled`` rejects.
  Who may resolve is the same rule as the JSON API: with an approver role, only an approver (never the
  requester under separation of duties); otherwise the requester. ``metadata.awaiting`` is ``"requester"``
  when this caller may decide, ``"approver"`` when someone else must.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from agentkit.hosting import (
    AgentKitSettings,
    Caller,
    ConversationError,
    ConversationService,
    Decision,
    SessionLockTimeout,
    TurnResult,
    http_caller,
)

__all__ = ["AgUiChannel", "agui_thread_session_id"]

logger = logging.getLogger(__name__)

_THREAD_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def agui_thread_session_id(thread_id: str) -> str:
    return f"agui-{thread_id}"


class AgUiChannel:
    def __init__(self, path: str = "/v1/agui", *, show_tool_calls: bool = True, show_tool_results: bool = False):
        """``show_tool_calls`` streams tool names and arguments (for "Looking up order…" chips);
        ``show_tool_results`` also streams raw tool output to the browser (off: it may hold data the
        agent was meant to summarise, not display)."""
        self.path = path
        self.show_tool_calls = show_tool_calls
        self.show_tool_results = show_tool_results
        self.service: ConversationService | None = None
        self.settings: AgentKitSettings | None = None

    def install(self, app: Any, service: ConversationService, settings: AgentKitSettings) -> None:
        from ag_ui.core import RunAgentInput

        self.service, self.settings = service, settings

        async def run(request: Request) -> StreamingResponse:
            try:
                payload = RunAgentInput.model_validate(await request.json())
            except ValueError as exc:
                raise HTTPException(422, f"not an AG-UI RunAgentInput: {exc}") from None
            caller = http_caller(request, settings, channel="agui")
            if not _THREAD_ID.match(payload.thread_id):
                raise HTTPException(400, "thread_id must be 1-128 characters of letters, digits, . _ : -")
            session_id = agui_thread_session_id(payload.thread_id)
            if payload.resume:
                decisions = self._decisions(payload.resume)
                await service.precheck_decision(caller, session_id, decisions)
                items = service.stream_decide(caller, session_id, decisions)
            else:
                message = self._last_user_text(payload.messages)
                await service.precheck_turn(caller, session_id, create=True)
                items = service.stream_turn(caller, message, session_id, create=True)
            events = self._events(items, payload.thread_id, payload.run_id, caller)
            return StreamingResponse(events, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        app.add_api_route(self.path, run, methods=["POST"], tags=["AG-UI"], response_model=None,
                          summary="Run the agent (AG-UI protocol, server-sent events)")

    # ------------------------------------------------------------ input
    @staticmethod
    def _last_user_text(messages: list[Any]) -> str:
        for message in reversed(messages):
            if getattr(message, "role", None) != "user":
                continue
            content = message.content
            if isinstance(content, str):
                text = content
            else:  # multimodal parts: text only for now
                text = "\n".join(p.text for p in content if getattr(p, "type", None) == "text")
            if text.strip():
                if len(text) > 100_000:
                    raise HTTPException(413, "message too long")
                return text
            break
        raise HTTPException(400, "the last message must be a user message with text (or send resume)")

    @staticmethod
    def _decisions(resume: list[Any]) -> dict[str, Decision]:
        decisions: dict[str, Decision] = {}
        for entry in resume:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.status == "cancelled":
                approved = False
            elif isinstance(payload.get("approved"), bool):
                approved = payload["approved"]
            else:
                raise HTTPException(400, f"resume for {entry.interrupt_id}: payload.approved must be true or false")
            comment = payload.get("comment") if isinstance(payload.get("comment"), str) else None
            decisions[entry.interrupt_id] = Decision(approved=approved, comment=(comment or "")[:1000] or None)
        return decisions

    # ------------------------------------------------------------ output
    def _interrupts(self, result: TurnResult, caller: Caller) -> list[Any]:
        from ag_ui.core import Interrupt

        role = self.settings.approver_role
        # Who decides: with an approver role, an approver (never the requester under separation of duties);
        # otherwise the requester. "requester" means this caller may decide it themselves.
        requester_decides = not role or (caller.is_approver and not self.settings.approval_separation)
        interrupts = []
        for item in result.pending:
            call = (item.get("request") or {}).get("function_call") or {}
            interrupts.append(Interrupt(
                id=item["id"],
                reason="tool_approval",
                message=f"Approve {item['tool']}?",
                tool_call_id=call.get("call_id"),
                response_schema={
                    "type": "object", "required": ["approved"],
                    "properties": {"approved": {"type": "boolean"}, "comment": {"type": "string", "maxLength": 1000}},
                },
                metadata={"tool": item["tool"], "arguments": item["arguments"],
                          "awaiting": "requester" if requester_decides else "approver",
                          "approver_role": self.settings.approver_role,
                          # the requester can poll this (GET) to learn when an approver has decided
                          "status_url": f"/v1/sessions/{result.session_id}/approvals"},
            ))
        return interrupts

    async def _events(self, items: AsyncIterator[Any], thread_id: str, run_id: str, caller: Caller):
        from ag_ui.core import (
            CustomEvent,
            RunErrorEvent,
            RunFinishedEvent,
            RunStartedEvent,
            TextMessageContentEvent,
            TextMessageEndEvent,
            TextMessageStartEvent,
            ToolCallArgsEvent,
            ToolCallEndEvent,
            ToolCallResultEvent,
            ToolCallStartEvent,
        )
        from ag_ui.core.events import RunFinishedInterruptOutcome, RunFinishedSuccessOutcome
        from ag_ui.encoder import EventEncoder

        encoder = EventEncoder()
        message_id: str | None = None
        open_calls: set[str] = set()

        def sse(event: Any) -> str:
            return encoder.encode(event)

        yield sse(RunStartedEvent(thread_id=thread_id, run_id=run_id))
        try:
            result: TurnResult | None = None
            async for item in items:
                if isinstance(item, TurnResult):
                    result = item
                    continue
                for content in item.contents or []:
                    if content.type == "text" and content.text:
                        if message_id is None:
                            message_id = str(uuid.uuid4())
                            yield sse(TextMessageStartEvent(message_id=message_id, role="assistant"))
                        yield sse(TextMessageContentEvent(message_id=message_id, delta=content.text))
                    elif content.type == "function_call" and self.show_tool_calls and content.call_id:
                        if content.call_id not in open_calls:
                            open_calls.add(content.call_id)
                            yield sse(ToolCallStartEvent(tool_call_id=content.call_id, tool_call_name=content.name or "",
                                                         parent_message_id=message_id))
                        args = content.arguments
                        if args:
                            yield sse(ToolCallArgsEvent(tool_call_id=content.call_id,
                                                        delta=args if isinstance(args, str) else json.dumps(args)))
                    elif content.type == "function_result" and content.call_id:
                        if content.call_id in open_calls:
                            open_calls.discard(content.call_id)
                            yield sse(ToolCallEndEvent(tool_call_id=content.call_id))
                        if self.show_tool_results:
                            yield sse(ToolCallResultEvent(message_id=str(uuid.uuid4()), tool_call_id=content.call_id,
                                                          content=json.dumps(content.result, default=str)[:20_000]))
            for call_id in sorted(open_calls):  # calls paused for approval have no result yet
                yield sse(ToolCallEndEvent(tool_call_id=call_id))
            assert result is not None
            if message_id is None and result.reply:  # nothing streamed (e.g. a guardrail refusal)
                message_id = str(uuid.uuid4())
                yield sse(TextMessageStartEvent(message_id=message_id, role="assistant"))
                yield sse(TextMessageContentEvent(message_id=message_id, delta=result.reply))
            if message_id is not None:
                yield sse(TextMessageEndEvent(message_id=message_id))
            if result.blocked:
                yield sse(CustomEvent(name="agentkit.blocked", value={"reason": result.blocked}))
            if result.citations:  # sources the answer cites as [n]
                yield sse(CustomEvent(name="agentkit.citations",
                                      value={"messageId": message_id,
                                             "citations": [c.to_dict() for c in result.citations]}))
            outcome = (RunFinishedInterruptOutcome(interrupts=self._interrupts(result, caller)) if result.pending
                       else RunFinishedSuccessOutcome())
            yield sse(RunFinishedEvent(thread_id=thread_id, run_id=run_id, outcome=outcome))
        except (ConversationError, SessionLockTimeout) as exc:  # lost a race after the precheck
            yield sse(RunErrorEvent(message=str(getattr(exc, "detail", exc)), code=type(exc).__name__))
        except Exception as exc:
            logger.exception("AG-UI run failed")
            yield sse(RunErrorEvent(message="The agent failed to respond. Please try again.", code=type(exc).__name__))
