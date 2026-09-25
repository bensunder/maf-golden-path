"""One call to wire OpenTelemetry for an agent service."""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from .processor import RunContextSpanProcessor

__all__ = ["setup_telemetry"]

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
) -> None:
    """Configure tracing, metrics and logs once per process.

    Policy enforced here so teams do not have to remember it:
    * prompt/response content capture is refused in ``prod``;
    * every span carries service, environment, team and request context;
    * Azure Monitor is used when a connection string is present, OTLP when an endpoint is.
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
