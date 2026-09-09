"""Read-only source capabilities and shared format-extraction outcomes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel

from agentic_osdu.domain.models import EvidenceRecord, TrustLevel, WorkspaceRelativePath

CancellationCheck = Callable[[], bool]


class FormatExtractionError(RuntimeError):
    """Stable, bounded parser failure without raw input or path disclosure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class FormatSource(Protocol):
    """A read-only file capability resolved by the approved discovery boundary."""

    @property
    def file_id(self) -> UUID: ...

    @property
    def relative_path(self) -> WorkspaceRelativePath | None: ...

    @property
    def size_bytes(self) -> int: ...

    def open_binary(self, *, max_bytes: int | None = None) -> AbstractContextManager[BinaryIO]: ...

    def open_native(self) -> AbstractContextManager[object]: ...


@dataclass(frozen=True, slots=True)
class BytesFormatSource:
    """Immutable in-memory source used by fixtures and bounded compositions."""

    file_id: UUID
    content: bytes
    relative_path: WorkspaceRelativePath | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    def open_binary(self, *, max_bytes: int | None = None) -> AbstractContextManager[BinaryIO]:
        bounded = self.content if max_bytes is None else self.content[:max_bytes]
        return BytesIO(bounded)

    def open_native(self) -> AbstractContextManager[object]:
        return BytesIO(self.content)


@dataclass(frozen=True, slots=True)
class ExtractionOutcome[OutputT: BaseModel]:
    output: OutputT
    evidence: tuple[EvidenceRecord, ...]


def validate_source(request_file_id: UUID, source: FormatSource) -> None:
    if request_file_id != source.file_id:
        raise FormatExtractionError(
            "FILE_CHANGED",
            "The source capability does not match the requested discovered file.",
        )


def check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise FormatExtractionError("CANCELLED", "The extraction was cancelled.")


def evidence(
    file_id: UUID,
    rule_id: str,
    summary: str,
    *,
    location: str,
    observed_value: str | None = None,
    trust: TrustLevel = TrustLevel.VERIFIED,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=uuid5(file_id, f"{rule_id}:{location}:{observed_value or ''}"),
        evidence_type="metadata_field",
        rule_id=rule_id,
        summary=summary,
        location=location,
        observed_value=observed_value,
        trust_level=trust,
        source_file_id=file_id,
    )


__all__ = [
    "BytesFormatSource",
    "CancellationCheck",
    "ExtractionOutcome",
    "FormatExtractionError",
    "FormatSource",
    "check_cancelled",
    "evidence",
    "validate_source",
]
