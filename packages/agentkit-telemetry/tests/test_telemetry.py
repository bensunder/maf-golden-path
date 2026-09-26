import subprocess
import sys
import textwrap

import pytest
from agent_framework import Agent, tool
from opentelemetry import trace

from agentkit.telemetry import RunContextSpanProcessor, run_context, setup_telemetry
from agentkit.testing import ScriptedChatClient, reply, tool_call


@tool
def ping(x: str) -> str:
    """Echo."""
    return x


@pytest.fixture(scope="module")
def enriched(request):
    from agentkit.testing import install_span_recorder

    recorder = install_span_recorder()
    trace.get_tracer_provider().add_span_processor(
        RunContextSpanProcessor(static_attributes={"agentkit.team": "payments"}, salt="t")
    )
    return recorder


async def test_every_span_gets_request_context(enriched):
    enriched.clear()
    client = ScriptedChatClient(script=[tool_call("ping", x="1"), reply("done")])
    agent = Agent(client, name="ctx-agent", tools=[ping])
    with run_context(user_id="ben@example.com", session_id="s-1", tenant_id="contoso", channel="teams"):
        await agent.run("hi")

    spans = enriched.spans()
    kinds = {s.name.split(" ")[0] for s in spans}
    assert {"chat", "execute_tool", "invoke_agent"} <= kinds
    for span in spans:
        attrs = span.attributes
        assert attrs["session.id"] == "s-1"
        assert attrs["agentkit.tenant.id"] == "contoso"
        assert attrs["agentkit.team"] == "payments"
        assert attrs["agentkit.channel"] == "teams"
        # raw user id never leaves the process
        assert "enduser.id" not in attrs
        assert attrs["enduser.pseudo.id"] != "ben@example.com"


async def test_no_context_outside_block(enriched):
    enriched.clear()
    await Agent(ScriptedChatClient(script=[reply("ok")]), name="plain").run("hi")
    assert all("session.id" not in s.attributes for s in enriched.spans())


async def test_user_assertion_never_reaches_spans(enriched):
    enriched.clear()
    with run_context(user_id="u", user_assertion="eyJ.secret.token") as ctx:
        await Agent(ScriptedChatClient(script=[reply("ok")]), name="tok").run("hi")
    assert "secret" not in repr(ctx)
    for span in enriched.spans():
        assert all("secret" not in str(v) for v in span.attributes.values())


def test_nested_context_merges():
    with run_context(user_id="u", tenant_id="t"):
        with run_context(session_id="s", step="2") as inner:
            assert (inner.user_id, inner.tenant_id, inner.session_id) == ("u", "t", "s")
            assert inner.attributes == {"step": "2"}


def test_prod_refuses_content_capture():
    with pytest.raises(ValueError, match="prod"):
        setup_telemetry(service_name="x", environment="prod", capture_message_content=True)


def test_setup_and_run_metrics_in_clean_process():
    """setup_telemetry mutates global providers, so exercise it in a fresh interpreter."""
    script = textwrap.dedent(
        """
        import asyncio
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        reader = InMemoryMetricReader()
        metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from agent_framework import Agent, AgentMiddleware, AgentResponse, Message
        from agentkit.telemetry import setup_telemetry, AgentRunMetricsMiddleware, run_context
        from agentkit.testing import ScriptedChatClient, reply

        spans = InMemorySpanExporter()
        setup_telemetry(service_name="svc", service_version="1.2.3", environment="dev", team="payments", exporters=[spans])
        setup_telemetry(service_name="ignored")  # idempotent

        class Refuse(AgentMiddleware):
            async def process(self, ctx, nxt):
                ctx.result = AgentResponse(messages=[Message(role="assistant", contents=["no"])],
                                           additional_properties={"agentkit.blocked": "test:detail"})

        async def main():
            ok = Agent(ScriptedChatClient(script=[reply("fine")]), name="m", middleware=[AgentRunMetricsMiddleware()])
            blocked = Agent(ScriptedChatClient(), name="m", middleware=[AgentRunMetricsMiddleware(), Refuse()])
            with run_context(session_id="abc"):
                await ok.run("hi")
                await blocked.run("hi")
                streaming = Agent(ScriptedChatClient(script=[reply("streamed")]), name="m",
                                  middleware=[AgentRunMetricsMiddleware()])
                stream = streaming.run("hi", stream=True)
                async for _ in stream:
                    pass
                await stream.get_final_response()
        asyncio.run(main())

        import opentelemetry.trace as t
        t.get_tracer_provider().force_flush()
        finished = spans.get_finished_spans()
        agent_spans = [s for s in finished if s.name.startswith("invoke_agent")]
        assert agent_spans, [s.name for s in finished]
        s = agent_spans[0]
        assert s.resource.attributes["service.name"] == "svc"
        assert s.resource.attributes["deployment.environment.name"] == "dev"
        assert s.attributes["agentkit.team"] == "payments"
        assert s.attributes["session.id"] == "abc"

        outcomes = {}
        for rm in reader.get_metrics_data().resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == "agentkit.agent.runs":
                        for p in m.data.data_points:
                            key = p.attributes["outcome"]
                            if key == "blocked":
                                assert p.attributes["blocked_reason"] == "test", dict(p.attributes)
                            outcomes[key] = outcomes.get(key, 0) + p.value
        assert outcomes == {"ok": 2, "blocked": 1}, outcomes
        print("OK")
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stdout + proc.stderr
