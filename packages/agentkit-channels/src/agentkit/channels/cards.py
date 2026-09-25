"""Adaptive Cards for human approvals (schema 1.5, what Teams renders).

Tool arguments are produced by the model, so every value shown to an approver is treated as
untrusted: Markdown is escaped (no injected links or formatting) and long values are truncated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "VERB_APPROVE",
    "VERB_REJECT",
    "VERB_SUBMIT",
    "approval_card",
    "decided_card",
    "notice_card",
    "plain",
]

VERB_APPROVE = "agentkit.approvals.approve"
VERB_REJECT = "agentkit.approvals.reject"
VERB_SUBMIT = "agentkit.approvals.submit"  # several pending calls, decided one by one

_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<])")
_MAX_VALUE = 300


def plain(value: Any, limit: int = _MAX_VALUE) -> str:
    """Render an untrusted value as literal text: JSON for structures, Markdown escaped, truncated."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())  # no line breaks: they start new Markdown blocks
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return _MD_SPECIAL.sub(r"\\\1", text)


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "AdaptiveCard", "$schema": _SCHEMA, "version": "1.5", "body": body}
    if actions:
        card["actions"] = actions
    return card


def _facts(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "FactSet", "facts": [{"title": plain(k, 60), "value": plain(v)} for k, v in arguments.items()]}


def _time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts or 0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def approval_card(
    session_id: str,
    pending: Sequence[Mapping[str, Any]],
    *,
    requested_by: str | None,
    agent_name: str,
    separation: bool,
) -> dict[str, Any]:
    """The card an approver (or, in confirmation mode, the requester) acts on."""
    ids = [p["id"] for p in pending]
    who = plain(requested_by or "a user", 80)
    heading = "Approval needed" if separation else "Please confirm"
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": heading, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": f"{plain(agent_name, 80)} wants to run an action for {who}.", "wrap": True,
         "isSubtle": True, "spacing": "None"},
    ]
    for item in pending:
        container: dict[str, Any] = {
            "type": "Container", "separator": True,
            "items": [{"type": "TextBlock", "text": plain(item["tool"], 80), "weight": "Bolder", "wrap": True},
                      _facts(item.get("arguments") or {})],
        }
        if len(pending) > 1:
            container["items"].append({
                "type": "Input.ChoiceSet", "id": f"decision_{item['id']}", "style": "compact", "value": "approve",
                "choices": [{"title": "Approve", "value": "approve"}, {"title": "Reject", "value": "reject"}],
            })
        body.append(container)
    body.append({"type": "Input.Text", "id": "comment", "placeholder": "Comment (optional)", "maxLength": 1000,
                 "isMultiline": False})
    data = {"session_id": session_id, "approval_ids": ids}
    if len(pending) > 1:
        actions = [{"type": "Action.Execute", "title": "Submit decisions", "verb": VERB_SUBMIT, "data": data,
                    "style": "positive"}]
    else:
        actions = [
            {"type": "Action.Execute", "title": "Approve", "verb": VERB_APPROVE, "data": data, "style": "positive"},
            {"type": "Action.Execute", "title": "Reject", "verb": VERB_REJECT, "data": data, "style": "destructive"},
        ]
    return _card(body, actions)


def decided_card(
    pending: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, bool],
    *,
    decided_by: str | None,
    comment: str | None = None,
    decided_at: float | None = None,
) -> dict[str, Any]:
    """Replaces the approval card once decided: no buttons left to click twice."""
    body: list[dict[str, Any]] = []
    for item in pending:
        approved = decisions.get(item["id"], False)
        body.append({
            "type": "Container", "separator": bool(body),
            "items": [
                {"type": "TextBlock", "wrap": True, "weight": "Bolder", "color": "Good" if approved else "Attention",
                 "text": f"{'Approved' if approved else 'Rejected'}: {plain(item['tool'], 80)}"},
                _facts(item.get("arguments") or {}),
            ],
        })
    footer = f"by {plain(decided_by or 'unknown', 80)} at {_time(decided_at)}"
    body.append({"type": "TextBlock", "text": footer, "isSubtle": True, "wrap": True, "spacing": "Small"})
    if comment:
        body.append({"type": "TextBlock", "text": f"Comment: {plain(comment, 1000)}", "wrap": True})
    return _card(body)


def notice_card(text: str) -> dict[str, Any]:
    """A card with a single line of (trusted, kit-authored) text, e.g. 'already decided'."""
    return _card([{"type": "TextBlock", "text": text, "wrap": True}])
