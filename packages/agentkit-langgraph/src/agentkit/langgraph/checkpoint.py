"""LangGraph checkpoints kept in the agent's session, so the kit's session store persists them.

A graph paused for approval must resume on whichever replica gets the decision, after a restart too. The
kit already stores each conversation (Cosmos DB on Azure, Redis on a VPS, locked per conversation), so the
graph's latest checkpoint rides along in ``AgentSession.state``: only the newest checkpoint of each
namespace, the channel values it references, and its pending writes (which is what resuming needs).
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

__all__ = ["STATE_KEY", "load_saver", "safe_serializer", "save_saver"]

STATE_KEY = "agentkit.langgraph"


def _b(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _u(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _typed(value: tuple[str, bytes]) -> list[str]:
    return [value[0], _b(value[1])]


def _untyped(value: list[str]) -> tuple[str, bytes]:
    return value[0], _u(value[1])


def save_saver(saver: InMemorySaver, thread_id: str) -> dict[str, Any]:
    """The latest checkpoint per namespace of ``thread_id``, JSON-safe."""
    out: dict[str, Any] = {"checkpoints": [], "blobs": [], "writes": []}
    for ns, checkpoints in saver.storage.get(thread_id, {}).items():
        if not checkpoints:
            continue
        latest = max(checkpoints)  # checkpoint ids are time-ordered (uuid6)
        checkpoint, metadata, parent = checkpoints[latest]
        out["checkpoints"].append({"ns": ns, "id": latest, "checkpoint": _typed(checkpoint),
                                   "metadata": _typed(metadata), "parent": parent})
        versions = saver.serde.loads_typed(checkpoint).get("channel_versions", {})
        for channel, version in versions.items():
            blob = saver.blobs.get((thread_id, ns, channel, version))
            if blob is not None:
                out["blobs"].append({"ns": ns, "channel": channel, "version": version, "value": _typed(blob)})
        for (task_id, idx), (tid, channel, value, path) in saver.writes.get((thread_id, ns, latest), {}).items():
            out["writes"].append({"ns": ns, "id": latest, "task_id": task_id, "idx": idx, "channel": channel,
                                  "value": _typed(value), "path": path})
    return out


def safe_serializer(extra_types: Iterable[type] = ()) -> JsonPlusSerializer:
    """Checkpoints are read back with LangGraph's strict allow-list (messages, LangGraph's own types, and the
    ``extra_types`` a graph's state uses), never by importing whatever a stored payload names: a session store
    someone else could write to must not become a way to run code in this agent."""
    serde = JsonPlusSerializer(allowed_msgpack_modules=None)
    return serde.with_msgpack_allowlist(list(extra_types)) if extra_types else serde


def load_saver(state: dict[str, Any] | None, thread_id: str, *, extra_types: Iterable[type] = ()) -> InMemorySaver:
    """An in-memory saver holding the checkpoint saved by :func:`save_saver` (empty when there is none)."""
    saver = InMemorySaver(serde=safe_serializer(extra_types))
    if not state:
        return saver
    for c in state.get("checkpoints", []):
        saver.storage[thread_id][c["ns"]][c["id"]] = (_untyped(c["checkpoint"]), _untyped(c["metadata"]), c["parent"])
    for b in state.get("blobs", []):
        saver.blobs[(thread_id, b["ns"], b["channel"], b["version"])] = _untyped(b["value"])
    for w in state.get("writes", []):
        saver.writes[(thread_id, w["ns"], w["id"])][(w["task_id"], w["idx"])] = (
            w["task_id"], w["channel"], _untyped(w["value"]), w["path"])
    return saver
