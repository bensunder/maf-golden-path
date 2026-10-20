"""deploy/vps/signin: email sign-in (SIGN_IN=email). Who gets in, for how long, what reaches the agent, and what
never does. The upstream is a fake that echoes what it received, so header handling is checked on the wire."""

import importlib.util
import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("signin", ROOT / "deploy" / "vps" / "signin" / "signin.py")
signin = importlib.util.module_from_spec(spec)
sys.modules["signin"] = signin  # dataclasses look their module up while the file is still loading
spec.loader.exec_module(signin)

DK, BHARAT, BEN = "dkumar1@bechtel.com", "bbahunut@bechtel.com", "ben@example.com"
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc).timestamp()
END = "2026-10-30T23:59:59-06:00"
PASSCODE = "correct horse battery staple"
HASH = signin.hash_passcode(PASSCODE, n=2 ** 12)


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def echo(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/stream":
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"data: one\n\ndata: two\n\n")
    if request.url.path == "/redirect":
        return httpx.Response(302, headers={"location": "/elsewhere", "set-cookie": "app=1; Path=/"})
    body = request.read()
    return httpx.Response(200, json={"path": request.url.raw_path.decode(), "method": request.method,
                                     "headers": dict(request.headers), "body": body.decode()})


def env(**extra) -> dict:
    return {"OAUTH2_PROXY_COOKIE_SECRET": "x" * 32, "OAUTH2_PROXY_UPSTREAMS": "http://platform:8000",
            "OAUTH2_PROXY_COOKIE_SECURE": "false", "SIGNIN_EMAILS": f"{DK}, {BHARAT.upper()}",
            "SIGNIN_EXPIRES": END, "SIGNIN_ADMINS": BEN, "SIGNIN_ADMIN_PASSCODE_HASH": HASH, **extra}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def http(clock):
    app = signin.create_app(signin.Settings.from_env(env()), transport=httpx.MockTransport(echo), clock=clock)
    with TestClient(app, base_url="https://agent.example") as client:
        yield client


def csrf(http) -> str:
    page = http.get("/oauth2/sign_in")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def sign_in(http, email, passcode=None, rd="/agents/payments/console/playground"):
    data = {"email": email, "csrf": csrf(http), "rd": rd}
    if passcode is not None:
        data["passcode"] = passcode
    return http.post("/oauth2/sign_in", data=data, follow_redirects=False)


# ------------------------------------------------------------------------------------------- who gets in
@pytest.mark.parametrize("email", [DK, BHARAT, "  DKumar1@Bechtel.com "])
def test_listed_guests_get_in_with_just_their_email(http, email):
    r = sign_in(http, email)
    assert r.status_code == 303 and r.headers["location"] == "/agents/payments/console/playground"
    seen = http.get("/agents/payments/console/playground").json()
    assert seen["headers"]["x-forwarded-email"] == email.strip().lower()


@pytest.mark.parametrize("email", ["someone@bechtel.com", "dkumar1@bechtel.co", "attacker@example.com"])
def test_anyone_else_is_refused(http, email):
    r = sign_in(http, email)
    assert r.status_code == 403 and "doesn&#x27;t have access" in r.text
    assert http.get("/v1/console/overview").status_code == 401


def test_access_ends_on_the_end_date_even_mid_session(http, clock):
    clock.now = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc).timestamp()  # Oct 30, 20:00 in Denver
    assert sign_in(http, DK).status_code == 303
    clock.now = datetime(2026, 10, 31, 5, 59, 58, tzinfo=timezone.utc).timestamp()  # 23:59:58 in Denver
    assert http.get("/x").status_code == 200
    clock.now = datetime(2026, 10, 31, 6, 0, 0, tzinfo=timezone.utc).timestamp()  # Oct 31 00:00 in Denver
    assert http.get("/x").status_code == 401  # the session ended with the access
    r = sign_in(http, DK)
    assert r.status_code == 403 and "has ended" in r.text


def test_sessions_last_twelve_hours(http, clock):
    sign_in(http, DK)
    clock.now += 11.9 * 3600
    assert http.get("/x").status_code == 200
    clock.now += 0.2 * 3600
    assert http.get("/x").status_code == 401


def test_removing_someone_signs_them_out(clock):
    transport = httpx.MockTransport(echo)
    before = signin.create_app(signin.Settings.from_env(env()), transport=transport, clock=clock)
    with TestClient(before, base_url="https://agent.example") as http:
        sign_in(http, BHARAT)
        session = http.cookies.get("_agentkit_signin")
    after = signin.create_app(signin.Settings.from_env(env(SIGNIN_EMAILS=DK)), transport=transport, clock=clock)
    with TestClient(after, base_url="https://agent.example", cookies={"_agentkit_signin": session}) as http:
        assert http.get("/x").status_code == 401


# --------------------------------------------------------------------------------------------- admins
def test_admins_also_need_the_passcode(http):
    first = sign_in(http, BEN)
    assert first.status_code == 200 and 'name="passcode"' in first.text  # asked for it, not refused
    assert http.get("/x").status_code == 401
    wrong = sign_in(http, BEN, passcode="guess")
    assert wrong.status_code == 403 and "isn&#x27;t right" in wrong.text
    ok = sign_in(http, BEN, passcode=PASSCODE)
    assert ok.status_code == 303
    assert http.get("/x").json()["headers"]["x-forwarded-email"] == BEN


def test_an_admin_listed_as_a_guest_still_needs_the_passcode():
    settings = signin.Settings.from_env(env(SIGNIN_EMAILS=f"{DK},{BEN}"))
    assert BEN not in settings.guests and BEN in settings.admins


def test_admin_access_doesnt_end_with_the_guests(http, clock):
    clock.now = datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()
    assert sign_in(http, BEN, passcode=PASSCODE).status_code == 303


@pytest.mark.parametrize("extra,message", [
    ({"SIGNIN_ADMIN_PASSCODE_HASH": ""}, "passcode"),
    ({"SIGNIN_EMAILS": "", "SIGNIN_ADMINS": ""}, "nobody"),
    ({"SIGNIN_EMAILS": "not-an-email"}, "e-mail"),
    ({"SIGNIN_EXPIRES": "2026-10-30T23:59:59"}, "time zone"),
    ({"OAUTH2_PROXY_COOKIE_SECRET": "short"}, "secret"),
    ({"OAUTH2_PROXY_UPSTREAMS": ""}, "upstream"),
])
def test_unsafe_configurations_refuse_to_start(extra, message):
    with pytest.raises(ValueError, match=message):
        signin.Settings.from_env(env(**extra))


def test_passcodes_are_stored_as_scrypt_hashes():
    stored = signin.hash_passcode("s3cret-passcode", n=2 ** 12)
    assert stored.startswith("scrypt:") and "s3cret" not in stored and "$" not in stored
    assert signin.verify_passcode("s3cret-passcode", stored)
    assert not signin.verify_passcode("s3cret-passcodE", stored)
    assert not signin.verify_passcode("x", "garbage")


# ----------------------------------------------------------------------------------- what reaches the agent
def test_identity_headers_from_the_browser_never_reach_the_agent(http):
    sign_in(http, DK)
    http.cookies.set("theme", "dark")
    seen = http.get("/v1/console/overview", headers={
        "X-Forwarded-Email": BEN, "X-Forwarded-User": BEN, "X-MS-CLIENT-PRINCIPAL-NAME": BEN,
        "X-Auth-Request-Email": BEN, "Authorization": "Bearer stolen", "X-Agentkit-Prefix": "/agents/evil",
        "X-Agentkit-Platform": "1", "X-Agentkit-Confirm": "payments",
    }).json()["headers"]
    assert seen["x-forwarded-email"] == DK and seen["x-forwarded-user"] == DK
    for name in ("x-ms-client-principal-name", "x-auth-request-email", "authorization", "x-agentkit-prefix",
                 "x-agentkit-platform"):
        assert name not in seen
    assert seen["x-agentkit-confirm"] == "payments"  # the console's own confirmation header still works
    assert "_agentkit_signin" not in seen.get("cookie", "") and "theme=dark" in seen["cookie"]


def test_requests_and_streams_pass_through_unchanged(http):
    sign_in(http, DK)
    posted = http.post("/agents/payments/v1/chat?x=1%202", json={"message": "hi"}).json()
    assert posted["method"] == "POST" and posted["path"] == "/agents/payments/v1/chat?x=1%202"
    assert json.loads(posted["body"]) == {"message": "hi"}
    stream = http.get("/stream")
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text == "data: one\n\ndata: two\n\n"
    redirect = http.get("/redirect", follow_redirects=False)
    assert redirect.status_code == 302 and redirect.headers["location"] == "/elsewhere"
    assert "app=1" in redirect.headers["set-cookie"]


def test_pages_redirect_to_sign_in_and_apis_get_401(http):
    page = http.get("/agents/payments/console/playground?tab=1", headers={"accept": "text/html"},
                    follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/oauth2/sign_in?rd=%2Fagents%2Fpayments%2Fconsole%2Fplayground%3Ftab%3D1"
    api = http.get("/v1/console/overview")
    assert api.status_code == 401 and api.json() == {"detail": "Sign in first."}


def test_upstream_down_is_a_clean_502(clock):
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app = signin.create_app(signin.Settings.from_env(env()), transport=httpx.MockTransport(down), clock=clock)
    with TestClient(app, base_url="https://agent.example") as http:
        sign_in(http, DK)
        assert http.get("/x").status_code == 502


# ------------------------------------------------------------------------------------------- abuse
def test_forged_or_tampered_sessions_are_rejected(http, clock):
    settings = signin.Settings.from_env(env())
    other = replace(settings, secret=b"y" * 32)
    forged, _ = signin.make_session(other, DK, clock())
    http.cookies.set("_agentkit_signin", forged)
    assert http.get("/x").status_code == 401
    genuine, _ = signin.make_session(settings, DK, clock())
    body, sig = genuine.split(".")
    tampered = signin._b64(json.dumps({"e": BEN, "iat": 0, "exp": 9e9}).encode()) + "." + sig
    http.cookies.set("_agentkit_signin", tampered)
    assert http.get("/x").status_code == 401


def test_sign_in_needs_the_pages_csrf_token(http):
    http.get("/oauth2/sign_in")
    r = http.post("/oauth2/sign_in", data={"email": DK, "csrf": "forged", "rd": "/"}, follow_redirects=False)
    assert r.status_code == 400 and http.get("/x").status_code == 401
    assert http.post("/oauth2/sign_in", content=json.dumps({"email": DK}),
                     headers={"content-type": "application/json"}).status_code == 415


def test_repeated_failures_are_blocked(http):
    for _ in range(10):
        sign_in(http, "guess@bechtel.com")
    r = sign_in(http, DK)  # the same address is blocked for a while, even with a listed e-mail
    assert r.status_code == 429


@pytest.mark.parametrize("rd", ["https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)",
                                "/oauth2/sign_out"])
def test_after_sign_in_it_only_goes_to_this_site(http, rd):
    r = sign_in(http, DK, rd=rd)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_sign_out_ends_the_session(http):
    sign_in(http, DK)
    assert http.get("/x").status_code == 200
    http.get("/oauth2/sign_out", follow_redirects=False)
    assert http.get("/x").status_code == 401


def test_cookies_are_secure_by_default(clock):
    app = signin.create_app(signin.Settings.from_env(env(OAUTH2_PROXY_COOKIE_SECURE="true")),
                            transport=httpx.MockTransport(echo), clock=clock)
    with TestClient(app, base_url="https://agent.example") as http:
        page = http.get("/oauth2/sign_in")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = http.post("/oauth2/sign_in", data={"email": DK, "csrf": token, "rd": "/"}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "_agentkit_signin=" in cookie and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert page.headers["cache-control"] == "no-store"


def test_each_sign_in_is_logged(http, caplog):
    caplog.set_level("INFO", logger="agentkit.signin")
    sign_in(http, DK)
    http.cookies.clear()
    sign_in(http, "nobody@bechtel.com")
    events = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    assert [(e["event"], e["email"]) for e in events] == [("signed_in", DK), ("sign_in_refused", "nobody@bechtel.com")]
    assert events[1]["reason"] == "denied" and all("passcode" not in json.dumps(e) for e in events)
