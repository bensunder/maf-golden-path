"""E-mail sign-in in a real browser: the sign-in gate (signin.py) in front of a real agent with its console,
both served by uvicorn. A listed guest types their e-mail and lands on the Playground as themselves; anyone else
is turned away and never reaches the agent."""

import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from agentkit.channels import Console
from agentkit.hosting import AgentKitSettings, build_agent, create_app
from agentkit.testing import ScriptedChatClient, reply

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("signin_browser", ROOT / "deploy" / "vps" / "signin" / "signin.py")
signin = importlib.util.module_from_spec(spec)
sys.modules["signin_browser"] = signin
spec.loader.exec_module(signin)

DK = "dkumar1@bechtel.com"


class Server:
    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


@pytest.fixture
def site():
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", service_name="payments",
                                require_user=True, user_header="x-forwarded-email", user_fallback_header="",
                                principal_claims_header="", memory="session", _env_file=None)
    agent_app = create_app(lambda s: build_agent(name="payments", instructions="x", settings=s,
                                                 client=ScriptedChatClient(script=[reply("Hello DK.")])),
                           settings=settings, configure_telemetry=False, channels=[Console(title="Payments")])
    with Server(agent_app) as upstream:
        gate = signin.create_app(signin.Settings.from_env({
            "OAUTH2_PROXY_COOKIE_SECRET": "c" * 32, "OAUTH2_PROXY_UPSTREAMS": upstream,
            "OAUTH2_PROXY_COOKIE_SECURE": "false", "SIGNIN_EMAILS": f"{DK},bbahunut@bechtel.com",
            "SIGNIN_EXPIRES": "2099-01-01T00:00:00+00:00", "SIGNIN_TITLE": "Payments agent"}))
        with Server(gate) as url:
            yield url


def test_a_listed_guest_types_their_email_and_lands_on_the_playground(browser, site):
    page = browser.new_page()
    page.goto(f"{site}/console/playground")
    assert "/oauth2/sign_in" in page.url and page.get_by_text("Payments agent").is_visible()
    page.get_by_label("Work e-mail").fill(DK)
    page.get_by_role("button", name="Continue").click()
    page.wait_for_url(f"{site}/console/playground")
    overview = page.request.get(f"{site}/v1/console/overview").json()
    assert overview["caller"]["user"] == DK  # the agent sees DK, set by the gate, not by the browser
    reply_ = page.request.post(f"{site}/v1/chat", data={"message": "hi"}).json()
    assert reply_["reply"] == "Hello DK."
    page.close()


def test_anyone_else_is_turned_away(browser, site):
    page = browser.new_page()
    page.goto(f"{site}/console/playground")
    page.get_by_label("Work e-mail").fill("someone.else@bechtel.com")
    page.get_by_role("button", name="Continue").click()
    assert page.get_by_role("alert").inner_text() == "This e-mail address doesn't have access."
    assert page.request.get(f"{site}/v1/console/overview").status == 401
    page.close()
