"""Structured JSON logging with required fields and bounded safe payloads."""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

from agentic_osdu.observability.correlation import get_correlation_context
from agentic_osdu.observability.redaction import Redactor

_EVENT_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_TOOL_ID = re.compile(r"^TOOL-(?:00[1-9]|0[12]\d|030)$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_STATUSES = {
    "started",
    "succeeded",
    "partially_succeeded",
    "cancelled",
    "failed",
    "skipped",
    "unknown",
}


class StructuredJsonFormatter(logging.Formatter):
    """Emit one deterministic JSON object without formatting raw log messages."""

    def __init__(self, *, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        context = get_correlation_context()
        event_code = getattr(record, "event_code", "UNSPECIFIED_EVENT")
        if not isinstance(event_code, str) or not _EVENT_CODE.fullmatch(event_code):
            event_code = "INVALID_EVENT_CODE"
        status = getattr(record, "status", "unknown")
        if not isinstance(status, str) or status not in _STATUSES:
            status = "unknown"
        tool_id = getattr(record, "tool_id", None) or context.tool_id
        if not isinstance(tool_id, str) or not _TOOL_ID.fullmatch(tool_id):
            tool_id = None
        tool_version = getattr(record, "tool_version", None)
        if not isinstance(tool_version, str) or not _SEMVER.fullmatch(tool_version):
            tool_version = None
        duration = getattr(record, "duration_ms", None)
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration < 0
        ):
            duration = None

        event: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "correlation_id": self.redactor.redact(context.correlation_id),
            "job_id": self.redactor.redact(getattr(record, "job_id", None) or context.job_id),
            "tool_id": tool_id,
            "tool_version": tool_version,
            "event_code": event_code,
            "duration_ms": duration,
            "status": status,
            "details": self.redactor.redact(getattr(record, "details", {})),
        }
        if record.exc_info is not None and record.exc_info[0] is not None:
            event["exception"] = {
                "type": self.redactor._safe_type_name(record.exc_info[0].__name__)
            }
        encoded = self._encode(event)
        if len(encoded.encode("utf-8")) > self.redactor.limits.max_log_bytes:
            event["details"] = "[TRUNCATED:LOG_SIZE]"
            event.pop("exception", None)
            encoded = self._encode(event)
        if len(encoded.encode("utf-8")) > self.redactor.limits.max_log_bytes:
            minimal = {
                key: event[key]
                for key in (
                    "timestamp",
                    "level",
                    "correlation_id",
                    "job_id",
                    "tool_id",
                    "tool_version",
                    "event_code",
                    "duration_ms",
                    "status",
                )
            }
            minimal["details"] = "[TRUNCATED:LOG_SIZE]"
            encoded = self._encode(minimal)
        if len(encoded.encode("utf-8")) > self.redactor.limits.max_log_bytes:
            minimal.update(
                correlation_id=None,
                job_id=None,
                tool_id=None,
                tool_version=None,
                duration_ms=None,
            )
            encoded = self._encode(minimal)
        if len(encoded.encode("utf-8")) > self.redactor.limits.max_log_bytes:
            encoded = self._encode(
                {
                    "timestamp": event["timestamp"],
                    "level": "error",
                    "correlation_id": None,
                    "job_id": None,
                    "tool_id": None,
                    "tool_version": None,
                    "event_code": "LOG_SIZE_EXCEEDED",
                    "duration_ms": None,
                    "status": "unknown",
                    "details": "[TRUNCATED:LOG_SIZE]",
                }
            )
        return encoded

    @staticmethod
    def _encode(event: dict[str, Any]) -> str:
        return json.dumps(
            event,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


__all__ = ["StructuredJsonFormatter"]
