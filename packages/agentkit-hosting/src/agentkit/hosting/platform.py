"""Requests from the VPS platform: signed, so an agent can tell them from anything else on the network.

On a server with the platform service, agents share a Docker network and learn who the user is from the
``X-Forwarded-Email`` header. Without more, any container on that network could claim to be anyone. With
``AGENTKIT_PLATFORM_KEY`` set (``agentctl.py`` writes a different key for every agent), the platform signs
every request it sends this agent — the method, path, user, delegation token and body, with a timestamp —
and :class:`PlatformSignatureMiddleware` refuses any request without a valid signature (probes excepted).
Signatures can't be replayed: each (with its random nonce) is accepted once, within two minutes.

The delegation token (``X-Agentkit-Delegation``) says who this request acts for and through which agents it
came. The platform issues and checks it; the agent only passes it back when it calls another agent, and reads
its (signature-covered) chain for the audit trail.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

from fastapi.responses import JSONResponse

__all__ = [
    "DELEGATION_HEADER",
    "PlatformSignatureMiddleware",
    "SIGNATURE_HEADER",
    "SIGNED_AT_HEADER",
    "delegation_claims",
    "request_mac",
]

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-agentkit-signature"
SIGNED_AT_HEADER = "x-agentkit-signed-at"
NONCE_HEADER = "x-agentkit-nonce"
DELEGATION_HEADER = "x-agentkit-delegation"
USER_HEADER = "x-forwarded-email"
MAX_SKEW_SECONDS = 120
MAX_SIGNED_BODY = 8 * 1024 * 1024
UNSIGNED_PATHS = frozenset({"/healthz", "/readyz"})  # probes: Docker and the fleet check them
_HEX64 = re.compile(r"[0-9a-f]{64}")


def request_mac(key_hex: str, signed_at: int, nonce: str, method: str, target: str, user: str, delegation: str,
                body: bytes) -> str:
    """The signature over one request. ``target`` is the raw path and query, exactly as sent."""
    message = "\n".join(["agentkit-request-v1", str(signed_at), nonce, method.upper(), target, user, delegation,
                         hashlib.sha256(body).hexdigest()])
    return hmac.new(bytes.fromhex(key_hex), message.encode("utf-8"), hashlib.sha256).hexdigest()


def delegation_claims(token: str | None) -> dict[str, Any]:
    """The claims inside a delegation token, *without* checking its signature (the platform does that; an
    agent reads them only from requests whose platform signature it has verified). Empty when malformed."""
    if not token or token.count(".") != 1:
        return {}
    payload = token.split(".", 1)[0]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


class _Seen:
    """Signatures accepted recently (a replay of one is refused)."""

    def __init__(self, limit: int = 50_000) -> None:
        self._items: OrderedDict[str, float] = OrderedDict()
        self._limit = limit

    def add(self, signature: str, now: float) -> bool:
        while self._items and (next(iter(self._items.values())) < now - 2 * MAX_SKEW_SECONDS or len(self._items) > self._limit):
            self._items.popitem(last=False)
        if signature in self._items:
            return False
        self._items[signature] = now
        return True


class PlatformSignatureMiddleware:
    """ASGI middleware: only requests the platform signed for this agent get through (probes excepted)."""

    def __init__(self, app: Any, key_hex: str) -> None:
        bytes.fromhex(key_hex)  # a malformed key fails at startup, not per request
        self.app = app
        self.key = key_hex
        self.seen = _Seen()

    async def _refuse(self, scope, receive, send, status: int, detail: str) -> None:
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in UNSIGNED_PATHS:
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for k, v in scope["headers"]:
            name = k.decode("latin-1").lower()
            if name in headers:  # a repeated identity or signature header is never ambiguous: refuse
                if name in (SIGNATURE_HEADER, SIGNED_AT_HEADER, NONCE_HEADER, DELEGATION_HEADER, USER_HEADER):
                    await self._refuse(scope, receive, send, 400, f"repeated {name} header")
                    return
                continue
            headers[name] = v.decode("latin-1")
        signature, signed_at = headers.get(SIGNATURE_HEADER, ""), headers.get(SIGNED_AT_HEADER, "")
        nonce = headers.get(NONCE_HEADER, "")
        if not _HEX64.fullmatch(signature) or not (signed_at.isascii() and signed_at.isdigit()) or len(signed_at) > 12 or not 16 <= len(nonce) <= 64 or not nonce.isascii():
            await self._refuse(scope, receive, send, 401, "requests to this agent must come through the platform")
            return
        now = time.time()
        if abs(now - int(signed_at)) > MAX_SKEW_SECONDS:
            await self._refuse(scope, receive, send, 401, "the platform's signature has expired")
            return

        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_SIGNED_BODY:
                await self._refuse(scope, receive, send, 413, "request body too large")
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)

        target = scope.get("raw_path") or scope["path"].encode("latin-1")
        target_text = target.decode("latin-1") + (("?" + scope["query_string"].decode("latin-1")) if scope.get("query_string") else "")
        expected = request_mac(self.key, int(signed_at), nonce, scope["method"], target_text, headers.get(USER_HEADER, ""),
                               headers.get(DELEGATION_HEADER, ""), body)
        if not hmac.compare_digest(expected, signature):
            logger.warning("refused %s %s: bad platform signature", scope["method"], scope["path"])
            await self._refuse(scope, receive, send, 401, "requests to this agent must come through the platform")
            return
        if not self.seen.add(signature, now):
            await self._refuse(scope, receive, send, 401, "that request was already handled (replay refused)")
            return

        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
