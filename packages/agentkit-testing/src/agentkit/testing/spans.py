"""Capture OpenTelemetry spans emitted by MAF agents in tests."""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

__all__ = ["SpanRecorder", "install_span_recorder"]


class SpanRecorder:
    """Thin wrapper over an in-memory exporter with query helpers."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()

    def clear(self) -> None:
        self.exporter.clear()

    def spans(self, name_contains: str | None = None) -> Sequence[ReadableSpan]:
        spans = self.exporter.get_finished_spans()
        if name_contains is None:
            return spans
        return [s for s in spans if name_contains in s.name]

    def names(self) -> list[str]:
        return [s.name for s in self.exporter.get_finished_spans()]

    def attributes(self, name_contains: str) -> list[dict]:
        return [dict(s.attributes or {}) for s in self.spans(name_contains)]


_RECORDER: SpanRecorder | None = None


def install_span_recorder() -> SpanRecorder:
    """Attach an in-memory exporter to the global tracer provider (once per process)."""
    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    recorder = SpanRecorder()
    provider.add_span_processor(SimpleSpanProcessor(recorder.exporter))

    from agent_framework.observability import enable_instrumentation

    enable_instrumentation(force=True)
    _RECORDER = recorder
    return recorder
