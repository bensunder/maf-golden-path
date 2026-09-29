"""One call to wire OpenTelemetry for an agent service."""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from .processor import RunContextSpanProcessor

__all__ = ["langsmith_exporter", "setup_telemetry"]

logger = logging.getLogger(__name__)
_CONFIGURED = False


def _azure_monitor_exporters(connection_string: str) -> list[Any]:
    try:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry-exporter "
            "is not installed. Install agentkit-telemetry[azure]."
        ) from exc
    return [
        AzureMonitorTraceExporter(connection_string=connection_string),
        AzureMonitorMetricExporter(connection_string=connection_string),
        AzureMonitorLogExporter(connection_string=connection_string),
    ]


def langsmith_exporter(api_key: str, *, project: str, endpoint: str = "https://api.smith.langchain.com/otel") -> Any:
    """An OTLP/HTTP trace exporter to LangSmith (``endpoint`` is the OTel base: EU, APAC or self-hosted differ).

    LangSmith maps the GenAI span conventions MAF emits (models, tokens, tools), so every agent run shows up as a
    trace; one person's request across several agents is one trace, because calls between agents carry W3C
    trace context. Prompts and answers are only included where content capture is on (never in prod)."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("LANGSMITH_API_KEY is set but the OTLP exporter isn't installed. "
                           "Install agentkit-telemetry[langsmith].") from exc
    # the in-code exporter takes the full signal URL (only the env-var form appends /v1/traces itself)
    return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces",
                            headers={"x-api-key": api_key, "Langsmith-Project": project})


def setup_telemetry(
    *,
    service_name: str,
    service_version: str = "0.0.0",
    environment: str = "local",
    team: str | None = None,
    otlp_endpoint: str | None = None,
    appinsights_connection_string: str | None = None,
    capture_message_content: bool = False,
    console: bool = False,
    pseudonymize_user_ids: bool = True,
    exporters: list[Any] | None = None,
    langsmith_api_key: str | None = None,
    langsmith_project: str | None = None,
    langsmith_endpoint: str = "https://api.smith.langchain.com/otel",
) -> None:
    """Configure tracing, metrics and logs once per process.

    Policy enforced here so teams do not have to remember it:
    * prompt/response content capture is refused in ``prod``;
    * every span carries service, environment, team and request context;
    * Azure Monitor is used when a connection string is present, OTLP when an endpoint is, LangSmith when an
      API key is (any combination).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    if capture_message_content and environment == "prod":
        raise ValueError("capture_message_content is not allowed in prod (prompts may contain personal data).")

    from agent_framework.observability import configure_otel_providers

    appinsights_connection_string = appinsights_connection_string or os.getenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING"
    )
    all_exporters = list(exporters or [])
    if appinsights_connection_string:
        all_exporters.extend(_azure_monitor_exporters(appinsights_connection_string))
    if langsmith_api_key:
        all_exporters.append(langsmith_exporter(langsmith_api_key, project=langsmith_project or service_name,
                                                endpoint=langsmith_endpoint))

    resource_attributes = {"deployment.environment.name": environment}
    if team:
        resource_attributes["agentkit.team"] = team

    kwargs: dict[str, Any] = {
        "service_name": service_name,
        "service_version": service_version,
        "resource_attributes": resource_attributes,
        "enable_sensitive_data": capture_message_content,
        "enable_console_exporters": console,
    }
    if otlp_endpoint:
        kwargs["otlp_endpoint"] = otlp_endpoint
    if all_exporters:
        kwargs["exporters"] = all_exporters
    configure_otel_providers(**kwargs)

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        static = {"agentkit.team": team} if team else {}
        provider.add_span_processor(
            RunContextSpanProcessor(static_attributes=static, pseudonymize_user_ids=pseudonymize_user_ids)
        )
    else:  # pragma: no cover - only when another SDK owns the provider
        logger.warning("Tracer provider is %s; request-context enrichment disabled.", type(provider).__name__)
    _CONFIGURED = True
