"""The platform's keys, delegation tokens and request signatures.

Everything derives from ``PLATFORM_SECRET_KEY`` (in .env): one key per agent for signing the requests the
platform sends it (``agentctl.py`` writes each agent its own), and one key the platform alone holds for the
delegation tokens that say who a request acts for and which agents it came through. The request format must
match ``agentkit.hosting.platform.request_mac`` exactly (a test checks both).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

__all__ = ["agent_key", "delegation_key", "mint", "request_mac", "signed_headers", "verify"]

DELEGATION_HEADER = "x-agentkit-delegation"
SIGNATURE_HEADER = "x-agentkit-signature"
SIGNED_AT_HEADER = "x-agentkit-signed-at"
NONCE_HEADER = "x-agentkit-nonce"


def _master(secret: str) -> bytes:
    return hashlib.sha256(b"agentkit-platform-v1:" + secret.encode("utf-8")).digest()


def _derive(secret: str, purpose: str) -> bytes:
    return hmac.new(_master(secret), purpose.encode("utf-8"), hashlib.sha256).digest()


def agent_key(secret: str, agent: str) -> str:
    """The key an agent verifies the platform's signatures with (hex). ``agent`` is its name, or "sample"."""
    return _derive(secret, "agent:" + agent).hex()


def delegation_key(secret: str) -> bytes:
    return _derive(secret, "delegation")


def request_mac(key_hex: str, signed_at: int, nonce: str, method: str, target: str, user: str, delegation: str,
                body: bytes) -> str:
    message = "\n".join(["agentkit-request-v1", str(signed_at), nonce, method.upper(), target, user, delegation,
                         hashlib.sha256(body).hexdigest()])
    return hmac.new(bytes.fromhex(key_hex), message.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(secret: str, agent: str, method: str, target: str, user: str, delegation: str, body: bytes) -> dict[str, str]:
    import secrets

    now, nonce = int(time.time()), secrets.token_hex(12)  # a retried request is a new request, not a replay
    return {SIGNED_AT_HEADER: str(now), NONCE_HEADER: nonce,
            SIGNATURE_HEADER: request_mac(agent_key(secret, agent), now, nonce, method, target, user, delegation, body)}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(key: bytes, *, user: str, audience: str, chain: list[str], root: str, expires: float, kind: str = "turn") -> str:
    """A delegation token: this request, for ``audience``, acts for ``user`` and came through ``chain``. ``kind`` is
    "decide" only for the request in which the person submits an approval (the only time decisions may pass)."""
    payload = _b64(json.dumps({"v": 1, "u": user, "a": audience, "c": chain, "r": root, "x": int(expires), "k": kind},
                              separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return payload + "." + _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def verify(key: bytes, token: str | None, *, now: float | None = None) -> dict[str, Any] | None:
    """The claims of a genuine, unexpired token, or None."""
    if not token or token.count(".") != 1 or len(token) > 4096:
        return None
    payload, signature = token.split(".")
    if not (payload.isascii() and signature.isascii()):
        return None
    expected = _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        return None
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or claims.get("v") != 1:
        return None
    if not isinstance(claims.get("u"), str) or not claims["u"] or not isinstance(claims.get("a"), str):
        return None
    if not isinstance(claims.get("c"), list) or not all(isinstance(a, str) for a in claims["c"]):
        return None
    if not isinstance(claims.get("x"), int) or claims["x"] < (now if now is not None else time.time()):
        return None
    if claims.get("k", "turn") not in ("turn", "decide"):
        return None
    return claims
