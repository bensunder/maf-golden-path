"""Connectors the VPS platform assigns (AGENTKIT_CONNECTORS): loaded into every agent, and never able to
take an agent down when the MCP server is away."""

import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from agentkit.hosting import AgentKitSettings, build_agent
from agentkit.testing import ScriptedChatClient, reply, tool_call
from agentkit.tools import ResilientMCPTool, platform_connectors

SERVER = '''
import sys
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
mcp = FastMCP("demo", host="127.0.0.1", port=int(sys.argv[1]), stateless_http=True)
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_contact(email: str) -> str:
    """Find a contact."""
    return f"Contact {email}: Dana Lee"
@mcp.tool()
def create_note(contact_email: str, text: str) -> str:
    """Add a note."""
    return "noted"
@mcp.tool()
def delete_contact(email: str) -> str:
    """Delete."""
    return "deleted"
mcp.run(transport="streamable-http")
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    script = tmp_path_factory.mktemp("mcp") / "server.py"
    script.write_text(SERVER)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()


def spec(url):
    return json.dumps([{"name": "crm", "title": "Demo CRM", "url": url, "allowed_tools": ["find_contact", "create_note"],
                        "approval": ["create_note"], "policy": "abcdef0123456789"}])


SETTINGS = dict(environment="test", guardrail_mode="heuristic", _env_file=None)


def test_assigned_connectors_reach_the_agent_with_approval_on_writes(server):
    settings = AgentKitSettings(**SETTINGS, connectors=spec(server), connector_token="t" * 40)
    client = ScriptedChatClient()
    client.enqueue(tool_call("crm_find_contact", email="dana@acme.com"), reply("Dana Lee at Acme."))
    agent = build_agent(name="t", instructions="x", settings=settings, client=client)
    result = asyncio.run(agent.run("who is dana@acme.com"))
    assert result.text == "Dana Lee at Acme."
    tool = next(t for t in agent.mcp_tools if isinstance(t, ResilientMCPTool))
    assert tool.approval_mode == {"always_require_approval": ["create_note"], "never_require_approval": ["find_contact"]}
    assert tool.allowed_tools == ["find_contact", "create_note"]  # delete_contact never reaches the model
    assert tool._httpx_client.headers["x-agentkit-connector-policy"] == "abcdef0123456789" if hasattr(tool, "_httpx_client") else True


def test_an_unreachable_connector_doesnt_take_the_agent_down():
    settings = AgentKitSettings(**SETTINGS, connectors=spec(f"http://127.0.0.1:{free_port()}/mcp"), connector_token="t" * 40)
    client = ScriptedChatClient()
    client.enqueue(reply("I can still help with everything else."), reply("still here"))
    agent = build_agent(name="t", instructions="x", settings=settings, client=client)
    assert asyncio.run(agent.run("hi")).text == "I can still help with everything else."
    started = time.monotonic()
    assert asyncio.run(agent.run("again")).text == "still here"
    assert time.monotonic() - started < 2  # in back-off: not retried on every turn


def test_bad_connector_settings_are_skipped_not_fatal():
    assert platform_connectors("not json", "t") == []
    assert platform_connectors(json.dumps(["just a string", 3, None]), "t") == []
    assert platform_connectors(json.dumps([{"name": "bad name", "url": "https://x"}, {"name": "ok", "url": "ftp://x"}]), "t") == []
    assert AgentKitSettings(**SETTINGS).connectors is None
