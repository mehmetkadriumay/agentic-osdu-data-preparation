"""Backend-neutral metrics, tracing, and audit-event interfaces."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from agentic_osdu.domain.models import (
    ActorRef,
    ContractModel,
    ProvenanceRecord,
    Rfc3339Timestamp,
    SemanticVersion,
    SideEffectRecord,
    ToolError,
)
from agentic_osdu.observability.redaction import Redactor


class MetricsSink(Protocol):
    """Minimal metric operations compatible with common telemetry adapters."""

    def increment(
        self,
        name: str,
        value: int = 1,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...


class NoOpMetrics:
    """Metrics backend used when no local or external exporter is configured."""

    def increment(
        self,
        name: str,
        value: int = 1,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, labels

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, labels

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, labels


class Span(Protocol, AbstractContextManager["Span"]):
    """OpenTelemetry-compatible subset owned by this project."""

    def set_attribute(self, name: str, value: str | int | float | bool) -> None: ...

    def add_event(
        self,
        name: str,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> None: ...

    def record_exception(self, error: BaseException) -> None: ...

    def end(self) -> None: ...


class Tracer(Protocol):
    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> Span: ...


class NoOpSpan:
    """A context-manager span that records no content and calls no exception repr."""

    __slots__ = ("_ended",)

    def __init__(self) -> None:
        self._ended = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.end()

    def set_attribute(self, name: str, value: str | int | float | bool) -> None:
        del name, value

    def add_event(
        self,
        name: str,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> None:
        del name, attributes

    def record_exception(self, error: BaseException) -> None:
        del error

    def end(self) -> None:
        self._ended = True


class NoOpTracer:
    """Trace backend used when no exporter is configured."""

    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> NoOpSpan:
        del name, attributes
        return NoOpSpan()


class AuditEventStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AuditEvent(ContractModel):
    """Append-only event contract for a later persistence implementation."""

    audit_event_id: UUID
    request_id: UUID
    correlation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    actor: ActorRef
    tool_id: str = Field(pattern=r"^TOOL-(?:00[1-9]|0[12]\d|030)$")
    tool_version: SemanticVersion
    status: AuditEventStatus
    side_effects: tuple[SideEffectRecord, ...] = ()
    errors: tuple[ToolError, ...] = ()
    provenance: tuple[ProvenanceRecord, ...] = ()
    occurred_at: Rfc3339Timestamp

    @model_validator(mode="after")
    def reject_sensitive_audit_content(self) -> AuditEvent:
        bounded_content = {
            "side_effects": [item.model_dump(mode="json") for item in self.side_effects],
            "errors": [item.model_dump(mode="json") for item in self.errors],
            "provenance": [item.model_dump(mode="json") for item in self.provenance],
        }
        if Redactor().redact(bounded_content) != bounded_content:
            raise ValueError("audit events must contain only bounded, redaction-safe values")
        return self


class AuditSink(Protocol):
    """Interface only; durable audit persistence belongs to EPIC-007."""

    def emit(self, event: AuditEvent) -> None: ...


class NoOpAuditSink:
    """No-persistence audit sink for deployments without a configured store."""

    def emit(self, event: AuditEvent) -> None:
        del event


__all__ = [
    "AuditEvent",
    "AuditEventStatus",
    "AuditSink",
    "MetricsSink",
    "NoOpAuditSink",
    "NoOpMetrics",
    "NoOpSpan",
    "NoOpTracer",
    "Span",
    "Tracer",
]
