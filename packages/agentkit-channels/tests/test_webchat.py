"""The <agentkit-chat> component in a real (headless) browser against a real server.

Skipped when Playwright or its Chromium isn't installed (CI installs both)."""

import base64
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from agent_framework import tool
from fastapi.testclient import TestClient

from agentkit.channels import AgUiChannel, WebChat
from agentkit.hosting import AgentKitSettings, approve_if, build_agent, create_app
from agentkit.testing import ScriptedChatClient, reply, tool_call

REFUNDS: list[tuple[str, float]] = []


@tool(approval_mode="always_require")
def issue_refund(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    REFUNDS.append((order_id, amount))
    return f"Refunded ${amount:.2f} on {order_id}"


@pytest.fixture(autouse=True)
def _reset():
    REFUNDS.clear()


def make_app(client, **settings):
    s = AgentKitSettings(**{"environment": "test", "guardrail_mode": "heuristic", "_env_file": None,
                            "service_name": "Order Desk", **settings})
    return create_app(
        lambda s: build_agent(name="orders", instructions="x", tools=[issue_refund], settings=s, client=client,
                              approval_rules=[approve_if("issue_refund", lambda a: a["amount"] <= 50)]),
        settings=s, configure_telemetry=False, channels=[AgUiChannel(), WebChat()],
    )


def test_page_and_script_are_served_with_a_strict_csp():
    with TestClient(make_app(ScriptedChatClient())) as http:
        page = http.get("/chat")
        script = http.get("/chat/agentkit-chat.js")
    assert page.status_code == 200 and "<title>Order Desk</title>" in page.text
    assert '<agentkit-chat endpoint="/v1/agui"' in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert script.headers["content-type"].startswith("text/javascript")
    assert "customElements.define" in script.text


# ------------------------------------------------------------------ browser
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


def open_chat(browser, url, headers=None, poll_ms=None):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    if headers:
        page.set_extra_http_headers(headers)
    page.goto(url + "/chat")
    if poll_ms:
        page.evaluate(f"document.querySelector('agentkit-chat').setAttribute('poll-ms', '{poll_ms}')")
    return page, errors


def say(page, text):
    box = page.locator("agentkit-chat textarea")
    box.fill(text)
    box.press("Enter")


def test_chat_approve_flow_in_a_browser(browser):
    client = ScriptedChatClient(script=[
        reply("Order A1 has shipped."),
        tool_call("issue_refund", order_id="A1", amount=80),
        reply("Refunded $80.00 on A1."),
    ])
    with Server(make_app(client)) as url:
        page, errors = open_chat(browser, url)
        say(page, "where is A1?")
        page.locator("agentkit-chat .assistant", has_text="Order A1 has shipped.").wait_for()

        say(page, "refund $80 on A1")
        panel = page.locator("agentkit-chat .approval")
        panel.wait_for()
        assert "Approve issue_refund?" in panel.inner_text() and "80" in panel.inner_text()
        assert REFUNDS == []
        panel.get_by_role("button", name="Approve").click()
        page.locator("agentkit-chat .assistant", has_text="Refunded $80.00 on A1.").wait_for()
        assert panel.get_by_role("button", name="Approve").is_disabled()  # can't be clicked twice
    assert REFUNDS == [("A1", 80.0)]
    assert errors == []


def test_model_output_is_rendered_as_text_not_html(browser):
    payload = '<img src=x onerror="document.title=\'pwned\'">'
    with Server(make_app(ScriptedChatClient(script=[reply(payload)]))) as url:
        page, errors = open_chat(browser, url)
        say(page, "hi")
        bubble = page.locator("agentkit-chat .assistant")
        bubble.wait_for()
        assert bubble.inner_text() == payload
        assert page.locator("agentkit-chat img").count() == 0
        assert page.title() == "Order Desk"


def principal(name, *roles):
    claims = [{"typ": "roles", "val": r} for r in roles]
    return {"x-ms-client-principal-name": name,
            "x-ms-client-principal": base64.b64encode(json.dumps({"claims": claims}).encode()).decode()}


def test_separation_shows_waiting_for_approver(browser):
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80), reply("Refunded.")])
    with Server(make_app(client, approver_role="Refunds.Approve")) as url:
        page, errors = open_chat(browser, url, headers=principal("ana"), poll_ms=200)
        say(page, "refund $80 on A1")
        panel = page.locator("agentkit-chat .approval")
        panel.wait_for()
        assert "Waiting for an approver (Refunds.Approve)" in panel.inner_text()
        assert panel.get_by_role("button").count() == 0

        say(page, "any news?")  # still paused: a clear message, not a crash
        page.locator("agentkit-chat .error", has_text="Still waiting for a decision").wait_for()
        assert REFUNDS == []

        # an approver decides elsewhere (Teams, the approvals API); the page notices by itself
        session_id = "agui-" + page.evaluate("document.querySelector('agentkit-chat').threadId")
        with httpx.Client(base_url=url) as http:
            pending = http.get(f"/v1/sessions/{session_id}/approvals", headers=principal("olga", "Refunds.Approve"))
            decided = http.post(f"/v1/sessions/{session_id}/approvals", headers=principal("olga", "Refunds.Approve"),
                                json={"decisions": [{"id": pending.json()[0]["id"], "approved": True}]})
        assert decided.status_code == 200
        panel.get_by_text("An approver has decided").wait_for(timeout=5000)
    assert REFUNDS == [("A1", 80.0)]
    # the only console error is the browser's own log of the expected 409
    assert all("status of 409" in e for e in errors), errors
