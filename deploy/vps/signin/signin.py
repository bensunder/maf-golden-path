"""Email sign-in for a VPS stack (SIGN_IN=email): the sign-in proxy for people on a short list, no identity provider.

It takes the place of oauth2-proxy in front of an agent (or the platform router) and keeps its contract: it is the
only way in, it answers on 4180, and it passes the signed-in person on as ``X-Forwarded-Email`` after removing
any identity header the browser sent.

Who may sign in, all from the environment (agentctl.py writes it from .env):

* ``SIGNIN_EMAILS``: guests. They type their e-mail address and are in. Anyone who knows a listed address can
  sign in as that person, so keep the list short and give it an end date.
* ``SIGNIN_EXPIRES``: when guests' access ends (ISO 8601, e.g. ``2026-10-30T23:59:59-06:00``). Sessions end then too.
* ``SIGNIN_ADMINS``: people with power over the stack (platform and memory admins). They also need the admin
  passcode (``SIGNIN_ADMIN_PASSCODE_HASH``, scrypt), because an e-mail address alone is easy to guess.

Sessions are HMAC-signed cookies (``OAUTH2_PROXY_COOKIE_SECRET``), HttpOnly, Secure, SameSite=Lax, for
``SIGNIN_SESSION_HOURS`` (12). Every request re-checks the session against the current lists and end date, so
removing someone (edit .env, ``docker compose up -d``) or reaching the end date signs them out at once.
Failed attempts are limited per client address and per e-mail. Each sign-in is logged as one JSON line.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("agentkit.signin")

EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
PREFIX = "/oauth2"
CSRF_COOKIE = "_agentkit_signin_csrf"

# Hop-by-hop headers (RFC 9110 7.6.1) and anything that could claim an identity: never forwarded from the browser.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding",
        "upgrade", "host", "content-length"}
_IDENTITY = {"authorization", "x-forwarded-email", "x-forwarded-user", "x-forwarded-preferred-username",
             "x-forwarded-groups", "x-forwarded-access-token", "x-agentkit-prefix", "x-agentkit-platform",
             "x-agentkit-delegation", "x-agentkit-user"}
_IDENTITY_PREFIXES = ("x-auth-request-", "x-ms-client-principal", "x-ms-token-")
_RESPONSE_DROP = {"connection", "keep-alive", "transfer-encoding", "trailer", "upgrade", "content-length"}

_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# ------------------------------------------------------------------------------------------------- passcode
def hash_passcode(passcode: str, *, n: int = 2 ** 15, r: int = 8, p: int = 1) -> str:
    """``scrypt:N:r:p:salt:hash`` (base64url, no ``$`` so .env and Compose leave it alone)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(passcode.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"scrypt:{n}:{r}:{p}:{b64(salt)}:{b64(digest)}"


def verify_passcode(passcode: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split(":")
        if kind != "scrypt":
            return False
        unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        got = hashlib.scrypt(passcode.encode(), salt=unb64(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, unb64(digest))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------------------------------------- settings
def _emails(raw: str | None) -> frozenset[str]:
    out = set()
    for part in (raw or "").split(","):
        email = part.strip().lower()
        if not email:
            continue
        if not EMAIL.fullmatch(email):
            raise ValueError(f"not an e-mail address: {email!r}")
        out.add(email)
    return frozenset(out)


def _when(raw: str | None) -> float | None:
    if not (raw or "").strip():
        return None
    value = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T23:59:59+00:00"  # a bare date: the end of that day, UTC
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("SIGNIN_EXPIRES needs a time zone offset, e.g. 2026-10-30T23:59:59-06:00")
    return stamp.timestamp()


@dataclass(frozen=True)
class Settings:
    upstream: str
    secret: bytes
    guests: frozenset[str] = frozenset()
    admins: frozenset[str] = frozenset()
    passcode_hash: str = ""
    expires: float | None = None
    session_hours: float = 12.0
    cookie_name: str = "_agentkit_signin"
    cookie_secure: bool = True
    title: str = "Agents"
    max_failures: int = 10
    failure_window: float = 900.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        secret = env.get("OAUTH2_PROXY_COOKIE_SECRET") or env.get("SIGNIN_COOKIE_SECRET") or ""
        if len(secret) < 16:
            raise ValueError("a cookie secret of 16+ characters is required (COOKIE_SECRET)")
        upstream = (env.get("OAUTH2_PROXY_UPSTREAMS") or env.get("SIGNIN_UPSTREAM") or "").split(",")[0].strip()
        if not upstream.startswith(("http://", "https://")):
            raise ValueError("an upstream URL is required (OAUTH2_PROXY_UPSTREAMS)")
        admins = _emails(env.get("SIGNIN_ADMINS"))
        guests = _emails(env.get("SIGNIN_EMAILS")) - admins  # an admin always needs the passcode
        passcode_hash = (env.get("SIGNIN_ADMIN_PASSCODE_HASH") or "").strip()
        if admins and not passcode_hash.startswith("scrypt:"):
            raise ValueError("admins need SIGNIN_ADMIN_PASSCODE_HASH (python3 agentctl.py passcode)")
        if not guests and not admins:
            raise ValueError("nobody may sign in: set SIGNIN_EMAILS or SIGNIN_ADMINS")
        hours = float(env.get("SIGNIN_SESSION_HOURS") or 12)
        if not 0.25 <= hours <= 24 * 7:
            raise ValueError("SIGNIN_SESSION_HOURS must be between 0.25 and 168")
        name = env.get("OAUTH2_PROXY_COOKIE_NAME") or "_agentkit_signin"
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
            raise ValueError("bad cookie name")
        return cls(
            upstream=upstream.rstrip("/"),
            secret=hashlib.sha256(b"agentkit-signin-v1\x00" + secret.encode()).digest(),
            guests=guests, admins=admins, passcode_hash=passcode_hash,
            expires=_when(env.get("SIGNIN_EXPIRES")), session_hours=hours, cookie_name=name,
            cookie_secure=(env.get("OAUTH2_PROXY_COOKIE_SECURE", "true").lower() != "false"),
            title=(env.get("SIGNIN_TITLE") or "Agents")[:80],
        )

    def access(self, email: str, now: float) -> str | None:
        """None when ``email`` may be signed in now, otherwise why not."""
        if email in self.admins:
            return None
        if email not in self.guests:
            return "denied"
        if self.expires is not None and now >= self.expires:
            return "expired"
        return None


# ------------------------------------------------------------------------------------------------- sessions
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(settings: Settings, email: str, now: float) -> tuple[str, float]:
    exp = now + settings.session_hours * 3600
    if email not in settings.admins and settings.expires is not None:
        exp = min(exp, settings.expires)
    body = _b64(json.dumps({"e": email, "iat": int(now), "exp": int(exp)}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(settings.secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def read_session(settings: Settings, value: str | None, now: float) -> str | None:
    """The signed-in e-mail, if the cookie is genuine, unexpired and the person may still sign in."""
    if not value or value.count(".") != 1:
        return None
    body, sig = value.split(".")
    want = _b64(hmac.new(settings.secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        claims = json.loads(_unb64(body))
        email, exp = str(claims["e"]), float(claims["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    if now >= exp or settings.access(email, now) is not None:
        return None
    return email


# ------------------------------------------------------------------------------------------------- limits
@dataclass
class Limiter:
    """Failed attempts per key in a sliding window, with a bounded number of keys."""

    limit: int
    window: float
    max_keys: int = 10_000
    hits: OrderedDict[str, deque[float]] = field(default_factory=OrderedDict)

    def blocked(self, key: str, now: float) -> bool:
        q = self.hits.get(key)
        if not q:
            return False
        while q and now - q[0] >= self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, key: str, now: float) -> None:
        q = self.hits.pop(key, None) or deque()
        q.append(now)
        self.hits[key] = q
        while len(self.hits) > self.max_keys:
            self.hits.popitem(last=False)


def client_address(request: Request) -> str:
    """The browser's address: the last X-Forwarded-For entry (added by Caddy/nginx in front), else the peer."""
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return forwarded[-1] if forwarded else (request.client.host if request.client else "unknown")


def safe_target(raw: str | None) -> str:
    """Where to go after signing in: a path on this site only (no other host, no scheme, no backslashes)."""
    value = (raw or "/").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value if not value.startswith(PREFIX + "/") else "/"


# ------------------------------------------------------------------------------------------------- pages
_STYLE = """
:root{color-scheme:light dark;--bg:#fafafa;--card:#fff;--fg:#18181b;--muted:#71717a;--line:#e4e4e7;--accent:#18181b;
--accent-fg:#fff;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#09090b;--card:#18181b;--fg:#fafafa;--muted:#a1a1aa;--line:#3f3f46;
--accent:#fafafa;--accent-fg:#18181b;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
main{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:18px;margin:0 0 4px}p{margin:0 0 18px;color:var(--muted);font-size:14px}
label{display:block;font-size:13px;font-weight:600;margin:0 0 6px}
input{width:100%;height:40px;border:1px solid var(--line);border-radius:8px;padding:0 12px;font:inherit;
background:transparent;color:var(--fg);margin:0 0 14px}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;height:40px;border:0;border-radius:8px;background:var(--accent);color:var(--accent-fg);font:inherit;
font-weight:600;cursor:pointer}
.err{color:var(--bad);font-size:14px;margin:0 0 14px}
"""


def page(settings: Settings, *, rd: str, csrf: str, email: str = "", error: str = "", passcode: bool = False) -> str:
    esc = html.escape
    err = f'<p class="err" role="alert">{esc(error)}</p>' if error else ""
    pass_field = (
        '<label for="passcode">Admin passcode</label>'
        '<input id="passcode" name="passcode" type="password" autocomplete="current-password" required autofocus>'
    ) if passcode else ""
    email_focus = "" if passcode else " autofocus"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · {esc(settings.title)}</title>
<style>{_STYLE}</style></head><body><main>
<h1>Sign in</h1><p>{esc(settings.title)}</p>{err}
<form method="post" action="{PREFIX}/sign_in">
<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="rd" value="{esc(rd)}">
<label for="email">Work e-mail</label>
<input id="email" name="email" type="email" autocomplete="email" required value="{esc(email)}"{email_focus}>
{pass_field}<button type="submit">Continue</button></form></main></body></html>"""


# ------------------------------------------------------------------------------------------------- app
async def _raw(upstream: httpx.Response):
    """The upstream body exactly as sent (still compressed if it was), chunk by chunk, so streams stay streams."""
    if upstream.is_stream_consumed:  # already read (e.g. a test transport)
        yield upstream.content
        return
    async for chunk in upstream.aiter_raw():
        yield chunk


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
               clock=time.time) -> Starlette:
    settings = settings or Settings.from_env()
    per_address = Limiter(settings.max_failures, settings.failure_window)
    per_email = Limiter(settings.max_failures, settings.failure_window)
    client = httpx.AsyncClient(base_url=settings.upstream, transport=transport, follow_redirects=False,
                               timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0))

    def cookie(response: Response, name: str, value: str, max_age: int, *, same_site: str = "lax") -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", secure=settings.cookie_secure, httponly=True,
                            samesite=same_site)

    def form_page(request: Request, *, status: int = 200, **kwargs) -> HTMLResponse:
        csrf = secrets.token_urlsafe(24)
        rd = kwargs.pop("rd", None) or safe_target(request.query_params.get("rd"))
        response = HTMLResponse(page(settings, rd=rd, csrf=csrf, **kwargs), status_code=status, headers=_PAGE_HEADERS)
        cookie(response, CSRF_COOKIE, csrf, 1800, same_site="strict")
        return response

    def audit(event: str, request: Request, email: str = "", **extra) -> None:
        line = {"event": event, "email": email, "client": client_address(request), "at": int(clock()), **extra}
        log.info(json.dumps(line, separators=(",", ":")))

    async def sign_in_page(request: Request) -> Response:
        if read_session(settings, request.cookies.get(settings.cookie_name), clock()):
            return RedirectResponse(safe_target(request.query_params.get("rd")), status_code=302)
        return form_page(request)

    async def sign_in(request: Request) -> Response:
        now = clock()
        address = client_address(request)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return Response("unsupported form", status_code=415)
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()[:254]
        rd = safe_target(str(form.get("rd") or "/"))
        token = str(form.get("csrf") or "")
        if not token or not hmac.compare_digest(token, request.cookies.get(CSRF_COOKIE, "")):
            audit("sign_in_refused", request, email, reason="csrf")
            return form_page(request, status=400, rd=rd, email=email,
                             error="Your sign-in page expired. Please try again.")
        if per_address.blocked(address, now) or (email and per_email.blocked(email, now)):
            audit("sign_in_refused", request, email, reason="rate_limited")
            return form_page(request, status=429, rd=rd, email=email,
                             error="Too many attempts. Wait 15 minutes and try again.")
        if not EMAIL.fullmatch(email):
            return form_page(request, status=400, rd=rd, email=email, error="Enter your e-mail address.")
        why = settings.access(email, now)
        if why:
            per_address.fail(address, now)
            per_email.fail(email, now)
            audit("sign_in_refused", request, email, reason=why)
            message = ("Access to this site has ended." if why == "expired"
                       else "This e-mail address doesn't have access.")
            return form_page(request, status=403, rd=rd, email=email, error=message)
        if email in settings.admins:
            passcode = str(form.get("passcode") or "")
            if not passcode:
                return form_page(request, rd=rd, email=email, passcode=True)
            if not verify_passcode(passcode, settings.passcode_hash):
                per_address.fail(address, now)
                per_email.fail(email, now)
                audit("sign_in_refused", request, email, reason="passcode")
                return form_page(request, status=403, rd=rd, email=email, passcode=True,
                                 error="That passcode isn't right.")
        value, exp = make_session(settings, email, now)
        audit("signed_in", request, email, until=int(exp))
        response = RedirectResponse(rd, status_code=303)
        cookie(response, settings.cookie_name, value, max(60, int(exp - now)))
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    async def sign_out(request: Request) -> Response:
        email = read_session(settings, request.cookies.get(settings.cookie_name), clock()) or ""
        if email:
            audit("signed_out", request, email)
        response = RedirectResponse(f"{PREFIX}/sign_in", status_code=303)
        response.delete_cookie(settings.cookie_name, path="/")
        return response

    async def ping(_: Request) -> Response:
        return Response("OK", media_type="text/plain")

    async def proxy(request: Request) -> Response:
        email = read_session(settings, request.cookies.get(settings.cookie_name), clock())
        if not email:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"{PREFIX}/sign_in?rd={quote(safe_target(target), safe='')}", status_code=302)
            return JSONResponse({"detail": "Sign in first."}, status_code=401, headers={"Cache-Control": "no-store"})
        headers = [(k, v) for k, v in request.headers.items()
                   if k.lower() not in _HOP and k.lower() not in _IDENTITY
                   and not k.lower().startswith(_IDENTITY_PREFIXES) and k.lower() != "cookie"]
        kept_cookies = "; ".join(f"{k}={v}" for k, v in request.cookies.items()
                                 if k not in (settings.cookie_name, CSRF_COOKIE))
        if kept_cookies:
            headers.append(("cookie", kept_cookies))
        headers += [("x-forwarded-email", email), ("x-forwarded-user", email)]
        if "x-forwarded-proto" not in {k.lower() for k, _ in headers}:
            headers.append(("x-forwarded-proto", request.url.scheme))
        path = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
        url = path + (f"?{request.url.query}" if request.url.query else "")
        body = request.stream() if request.method not in ("GET", "HEAD", "OPTIONS") else None
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                         stream=True)
        except httpx.HTTPError as exc:
            log.warning("upstream unreachable: %s", type(exc).__name__)
            return JSONResponse({"detail": "The service is starting or unavailable. Try again shortly."},
                                status_code=502)
        out = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in _RESPONSE_DROP]
        response = StreamingResponse(_raw(upstream), status_code=upstream.status_code,
                                     background=BackgroundTask(upstream.aclose))
        response.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out]
        return response

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            yield
        finally:
            await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[
        Route(f"{PREFIX}/sign_in", sign_in_page, methods=["GET"]),
        Route(f"{PREFIX}/sign_in", sign_in, methods=["POST"]),
        Route(f"{PREFIX}/start", sign_in_page, methods=["GET"]),
        Route(f"{PREFIX}/sign_out", sign_out, methods=["GET", "POST"]),
        Route(f"{PREFIX}/ping", ping, methods=["GET"]),
        Route("/{path:path}", proxy, methods=methods),
    ], lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    import uvicorn

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        sys.exit(f"sign-in can't start: {exc}")
    host, _, port = (os.getenv("OAUTH2_PROXY_HTTP_ADDRESS") or "0.0.0.0:4180").rpartition(":")
    log.info(json.dumps({"event": "started", "guests": len(settings.guests), "admins": len(settings.admins),
                         "expires": settings.expires}))
    uvicorn.run(create_app(settings), host=host or "0.0.0.0", port=int(port), proxy_headers=False,
                server_header=False, log_level="warning")


if __name__ == "__main__":
    main()
