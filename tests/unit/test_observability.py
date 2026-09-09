from __future__ import annotations

import io
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_osdu.domain.models import ActorRef, SideEffectKind, SideEffectRecord
from agentic_osdu.observability import (
    AuditEvent,
    AuditEventStatus,
    NoOpAuditSink,
    NoOpMetrics,
    NoOpTracer,
    RedactionLimits,
    Redactor,
    StructuredJsonFormatter,
    correlation_context,
    get_correlation_context,
)


def test_correlation_context_is_nested_and_restored() -> None:
    assert get_correlation_context().correlation_id is None
    with correlation_context("corr-outer", job_id="job-1"):
        assert get_correlation_context().correlation_id == "corr-outer"
        with correlation_context("corr-inner", tool_id="TOOL-004"):
            current = get_correlation_context()
            assert current.correlation_id == "corr-inner"
            assert current.tool_id == "TOOL-004"
        assert get_correlation_context().correlation_id == "corr-outer"
    assert get_correlation_context().correlation_id is None


def test_redactor_is_recursive_bounded_and_never_calls_unknown_repr() -> None:
    class Dangerous:
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

        def __str__(self) -> str:
            raise AssertionError("str must not be called")

    redactor = Redactor(
        RedactionLimits(max_depth=2, max_items=8, max_string_length=12, max_log_bytes=2048)
    )
    redacted = redactor.redact(
        {
            "safe": "abcdefghijklmnop",
            "password": "NeverDisplayThis",
            "items": list(range(10)),
            "deep": {"one": {"two": {"three": "hidden"}}},
            "object": Dangerous(),
        }
    )

    assert redacted["safe"] == "abcdefghijkl...[TRUNCATED]"
    assert redacted["[REDACTED_KEY:SECRET:1]"] == "[REDACTED:SECRET]"
    assert redacted["items"][-1] == "[TRUNCATED:ITEMS]"
    assert redacted["deep"]["one"] == "[TRUNCATED:DEPTH]"
    assert redacted["object"] == "[REDACTED:OBJECT:Dangerous]"


def test_structured_logging_redacts_nested_secrets_bytes_paths_and_exceptions() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("agentic_osdu.tests.security")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    raw_sample = b"\x00RAW-SEGY-SAMPLE-CONTENT\xff"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    token = "Bearer top-secret-access-token"
    windows_path = r"C:\Users\alice\confidential\survey.sgy"
    unc_path = r"\\server\share\private\manifest.json"
    posix_path = "/home/alice/private/manifest.json"

    try:
        raise ValueError(f"failure included {secret} at {windows_path}")
    except ValueError:
        with correlation_context("corr-safe", job_id="job-safe", tool_id="TOOL-003"):
            logger.exception(
                "ignored raw message %s",
                secret,
                extra={
                    "event_code": "FILE_SAMPLE_FAILED",
                    "tool_version": "1.0.0",
                    "duration_ms": 2.5,
                    "status": "failed",
                    "details": {
                        "raw_sample": raw_sample,
                        "api_token": token,
                        "nested": {
                            "source_path": windows_path,
                            "unc": unc_path,
                            "posix": posix_path,
                            "error": ValueError(
                                f"nested error {token} {raw_sample!r} {windows_path}"
                            ),
                        },
                    },
                },
            )

    emitted = stream.getvalue()
    event = json.loads(emitted)
    assert event["correlation_id"] == "corr-safe"
    assert event["job_id"] == "job-safe"
    assert event["tool_id"] == "TOOL-003"
    assert event["tool_version"] == "1.0.0"
    assert event["event_code"] == "FILE_SAMPLE_FAILED"
    assert event["status"] == "failed"
    assert event["exception"] == {"type": "ValueError"}
    assert {
        "timestamp",
        "level",
        "correlation_id",
        "job_id",
        "tool_id",
        "tool_version",
        "event_code",
        "duration_ms",
        "status",
    }.issubset(event)
    for prohibited in (
        "RAW-SEGY",
        secret,
        "top-secret",
        "alice",
        "confidential",
        "survey.sgy",
        "manifest.json",
        "ignored raw message",
    ):
        assert prohibited not in emitted
    assert "[REDACTED:BYTES]" in emitted
    assert "[REDACTED:SECRET]" in emitted
    assert "[REDACTED:PATH]" in emitted


def test_structured_log_size_is_bounded_with_deterministic_marker() -> None:
    formatter = StructuredJsonFormatter(
        redactor=Redactor(
            RedactionLimits(
                max_depth=4,
                max_items=10,
                max_string_length=64,
                max_log_bytes=600,
            )
        )
    )
    record = logging.LogRecord(
        name="bounded",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="not emitted",
        args=(),
        exc_info=None,
    )
    record.event_code = "BOUNDED_EVENT"
    record.status = "succeeded"
    record.details = {"values": ["x" * 64 for _ in range(10)]}

    encoded = formatter.format(record).encode("utf-8")
    assert len(encoded) <= 600
    assert b"[TRUNCATED:LOG_SIZE]" in encoded


def test_encoded_content_generic_paths_and_long_context_never_leak_or_exceed_limit() -> None:
    formatter = StructuredJsonFormatter(
        redactor=Redactor(
            RedactionLimits(
                max_depth=4,
                max_items=10,
                max_string_length=512,
                max_log_bytes=512,
            )
        )
    )
    record = logging.LogRecord(
        name="hostile",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="ignored",
        args=(),
        exc_info=None,
    )
    record.event_code = "HOSTILE_EVENT"
    record.status = "failed"
    record.details = {
        "content_base64": "UkFXLVNFQ1JFVC1GSUxFLUNPTlRFTlQ=",
        "path": "/data/customer/survey.sgy",
    }

    with correlation_context("c" * 400, job_id="j" * 400):
        emitted = formatter.format(record)

    assert len(emitted.encode()) <= 512
    assert "UkFXLVNFQ1JFVC1GSUxFLUNPTlRFTlQ=" not in emitted
    assert "/data/customer/survey.sgy" not in emitted
    assert "[REDACTED:CONTENT]" in emitted or "[TRUNCATED:LOG_SIZE]" in emitted


def test_no_backend_observability_and_audit_contracts_are_safe_noops() -> None:
    metrics = NoOpMetrics()
    metrics.increment("files.discovered", 1, {"format": "FMT-001"})
    metrics.observe("duration", 1.5)
    metrics.set_gauge("workers", 0)

    tracer = NoOpTracer()
    with tracer.start_span("tool.execute", attributes={"tool.id": "TOOL-001"}) as span:
        span.set_attribute("status", "succeeded")
        span.add_event("tool.completed", {"count": 1})
        span.record_exception(ValueError("must never be represented"))

    audit = AuditEvent(
        audit_event_id=uuid4(),
        request_id=uuid4(),
        correlation_id="corr-audit",
        actor=ActorRef(actor_id="operator"),
        tool_id="TOOL-001",
        tool_version="1.0.0",
        status=AuditEventStatus.SUCCEEDED,
        side_effects=(
            SideEffectRecord(
                side_effect_id=uuid4(),
                kind=SideEffectKind.NONE,
                target="workspace",
                description="No mutation.",
                occurred_at=datetime.now(UTC),
            ),
        ),
        occurred_at=datetime.now(UTC),
    )
    sink = NoOpAuditSink()
    sink.emit(audit)
    assert json.loads(audit.model_dump_json())["status"] == "succeeded"
    unsafe_effect = SideEffectRecord(
        side_effect_id=uuid4(),
        kind=SideEffectKind.FILE_WRITE,
        target=r"C:\Users\alice\private\output.json",
        description="Unsafe absolute target.",
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(
        ValidationError,
        match="audit events must contain only bounded, redaction-safe values",
    ):
        AuditEvent.model_validate(
            {**audit.model_dump(mode="python"), "side_effects": (unsafe_effect,)}
        )


def test_redaction_limits_and_additional_json_types_fail_safe() -> None:
    for limits in (
        {"max_depth": 0},
        {"max_items": 0},
        {"max_string_length": 7},
        {"max_log_bytes": 511},
    ):
        with pytest.raises(ValueError, match="must be between"):
            RedactionLimits(**limits)

    @dataclass(frozen=True)
    class SafeValue:
        count: int

    redactor = Redactor()
    value = redactor.redact(
        {
            "nan": math.nan,
            "model": ActorRef(actor_id="operator"),
            "dataclass": SafeValue(count=2),
            1: "non-string-key",
            "content_bytes": "not bytes but still prohibited raw content",
            "credential_value": "hidden",
        }
    )
    assert value["nan"] == "[REDACTED:NON_FINITE_NUMBER]"
    assert value["model"] == {"actor_id": "operator", "display_name": None}
    assert value["dataclass"] == {"count": 2}
    assert value["[NON_STRING_KEY:3]"] == "non-string-key"
    assert value["content_bytes"] == "[REDACTED:CONTENT]"
    assert value["[REDACTED_KEY:SECRET:5]"] == "[REDACTED:SECRET]"
