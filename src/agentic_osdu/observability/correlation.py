"""Correlation context propagation based on context-local state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    correlation_id: str | None = None
    job_id: str | None = None
    tool_id: str | None = None
    request_id: str | None = None


_CORRELATION_CONTEXT: ContextVar[CorrelationContext | None] = ContextVar(
    "agentic_osdu_correlation_context",
    default=None,
)


def get_correlation_context() -> CorrelationContext:
    """Return the current immutable correlation context."""

    return _CORRELATION_CONTEXT.get() or CorrelationContext()


@contextmanager
def correlation_context(
    correlation_id: str | None = None,
    *,
    job_id: str | None = None,
    tool_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[CorrelationContext]:
    """Merge context fields for a synchronous or asynchronous execution scope."""

    current = get_correlation_context()
    updated = replace(
        current,
        correlation_id=correlation_id if correlation_id is not None else current.correlation_id,
        job_id=job_id if job_id is not None else current.job_id,
        tool_id=tool_id if tool_id is not None else current.tool_id,
        request_id=request_id if request_id is not None else current.request_id,
    )
    token = _CORRELATION_CONTEXT.set(updated)
    try:
        yield updated
    finally:
        _CORRELATION_CONTEXT.reset(token)


__all__ = [
    "CorrelationContext",
    "correlation_context",
    "get_correlation_context",
]
