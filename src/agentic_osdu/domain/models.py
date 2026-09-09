"""Immutable Pydantic contracts shared by deterministic tools and infrastructure."""

from __future__ import annotations

import ntpath
import posixpath
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Never, SupportsIndex, cast
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)

type SemanticVersion = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$",
        max_length=128,
    ),
]
type StableCode = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]{1,127}$"),
]
type ToolId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^TOOL-(?:00[1-9]|0[12]\d|030)$"),
]
type Sha256 = Annotated[
    str,
    StringConstraints(strict=True, to_lower=True, pattern=r"^[0-9a-f]{64}$"),
]
type JsonPointer = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^(?:/(?:[^~/]|~[01])*)*$", max_length=2048),
]
type SafeText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1024),
]
type WorkspaceId = UUID
type FileId = UUID
type ManifestId = UUID
type EvidenceId = UUID
type ProvenanceId = UUID
type JobId = UUID
type RequestId = UUID

_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an RFC3339 UTC offset")
    return value


type Rfc3339Timestamp = Annotated[datetime, AfterValidator(_aware_datetime)]


def _bounded_json(
    value: JsonValue,
    *,
    depth: int = 0,
    max_depth: int = 6,
    max_items: int = 64,
    max_string_length: int = 2048,
    reject_secret_keys: bool = True,
) -> None:
    if depth > max_depth:
        raise ValueError("structured details exceed the maximum depth")
    if isinstance(value, str):
        if len(value) > max_string_length:
            raise ValueError("structured detail string exceeds its maximum length")
        return
    if isinstance(value, list):
        if len(value) > max_items:
            raise ValueError("structured details exceed the maximum item count")
        for item in value:
            _bounded_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                reject_secret_keys=reject_secret_keys,
            )
        return
    if isinstance(value, dict):
        if len(value) > max_items:
            raise ValueError("structured details exceed the maximum item count")
        for key, item in value.items():
            if len(key) > 128:
                raise ValueError("structured detail key exceeds 128 characters")
            if reject_secret_keys and _SECRET_KEY.search(key):
                raise ValueError("structured details must not contain secret-bearing keys")
            _bounded_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                reject_secret_keys=reject_secret_keys,
            )


class FrozenJsonDict(dict[str, Any]):
    """JSON object that remains serializable while rejecting mutation."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("structured contract values are immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: str) -> None:
        del key
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: str, default: Any = None) -> Any:
        del key, default
        self._immutable()

    def popitem(self) -> tuple[str, Any]:
        self._immutable()

    def setdefault(self, key: str, default: Any = None) -> Any:
        del key, default
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._immutable()

    def __ior__(self, value: Any) -> FrozenJsonDict:  # type: ignore[override,misc]
        del value
        self._immutable()


class FrozenJsonList(list[Any]):
    """JSON array that remains serializable while rejecting mutation."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("structured contract values are immutable")

    def __setitem__(self, key: Any, value: Any) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: Any) -> None:
        del key
        self._immutable()

    def append(self, value: Any) -> None:
        del value
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def extend(self, values: Any) -> None:
        del values
        self._immutable()

    def insert(self, index: SupportsIndex, value: Any) -> None:
        del index, value
        self._immutable()

    def pop(self, index: SupportsIndex = -1) -> Any:
        del index
        self._immutable()

    def remove(self, value: Any) -> None:
        del value
        self._immutable()

    def reverse(self) -> None:
        self._immutable()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._immutable()

    def __iadd__(self, values: Any) -> FrozenJsonList:  # type: ignore[misc]
        del values
        self._immutable()

    def __imul__(self, value: SupportsIndex) -> FrozenJsonList:
        del value
        self._immutable()


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return FrozenJsonList(_freeze_json(item) for item in value)
    return value


def _freeze_json_dict(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _freeze_json(value))


class ContractModel(BaseModel):
    """Strict, immutable base for all object-shaped domain contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ContractRootModel(RootModel[str]):
    """Strict, immutable base for scalar string contracts."""

    model_config = ConfigDict(frozen=True, strict=True)


class WorkspaceRelativePath(ContractRootModel):
    """Normalized workspace-relative path capability without traversal."""

    @field_validator("root")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("workspace-relative path must be non-empty")
        normalized = value.replace("\\", "/")
        drive, _ = ntpath.splitdrive(value)
        if drive or ntpath.isabs(value) or posixpath.isabs(value) or normalized.startswith("//"):
            raise ValueError("workspace-relative path must not be absolute")
        components = normalized.split("/")
        if any(component in {"", ".", ".."} for component in components):
            raise ValueError("workspace-relative path must be normalized without traversal")
        return normalized


class ApprovedAbsolutePath(ContractRootModel):
    """Absolute path explicitly approved by a caller; policy still governs access."""

    @field_validator("root")
    @classmethod
    def validate_absolute_path(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("approved absolute path must be non-empty")
        drive, tail = ntpath.splitdrive(value)
        windows_absolute = bool(drive and tail.startswith(("\\", "/"))) or value.startswith(
            ("\\\\", "//")
        )
        if not windows_absolute and not posixpath.isabs(value):
            raise ValueError("approved path must be absolute")
        return value


class OSDUKind(ContractRootModel):
    """Bounded OSDU kind identifier."""

    @field_validator("root")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if len(value) > 512 or not re.fullmatch(
            r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+",
            value,
        ):
            raise ValueError("OSDU kind must contain four bounded colon-separated segments")
        return value


class FormatId(StrEnum):
    """Stable supported input-format identifiers."""

    SEGY = "FMT-001"
    LAS = "FMT-002"
    JSON_WELL_LOG = "FMT-003"
    DLIS = "FMT-004"
    LIS_LTI = "FMT-005"
    CSV = "FMT-006"
    P190 = "FMT-007"
    SGP = "FMT-008"
    DAT = "FMT-009"
    TEXT = "FMT-010"
    PDF = "FMT-011"
    OSDU_MANIFEST = "FMT-012"
    OSDU_SCHEMA = "FMT-013"


class DataCategory(StrEnum):
    """Top-level oil-and-gas data classification."""

    SEISMIC = "seismic"
    WELL_LOG = "well_log"
    NAVIGATION = "navigation"
    GRID = "grid"
    INTERPRETATION = "interpretation"
    SUPPORTING_DOCUMENT = "supporting_document"
    MANIFEST = "manifest"
    SCHEMA = "schema"
    UNKNOWN = "unknown"


class DataSubtype(StrEnum):
    """Supported normalized subtypes."""

    SEGY = "segy"
    LAS = "las"
    JSON_WELL_LOG = "json_well_log"
    DLIS = "dlis"
    LIS_LTI = "lis_lti"
    CSV = "csv"
    P190 = "p190"
    SGP = "sgp"
    HORIZON = "horizon"
    FAULT = "fault"
    TEXT = "text"
    PDF = "pdf"
    OSDU_MANIFEST = "osdu_manifest"
    OSDU_SCHEMA = "osdu_schema"
    UNKNOWN = "unknown"


class StackType(StrEnum):
    PRE_STACK = "pre_stack"
    POST_STACK = "post_stack"
    MIXED = "mixed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class DataDomain(StrEnum):
    TIME = "time"
    DEPTH = "depth"
    MIXED = "mixed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ProcessingLevel(StrEnum):
    RAW = "raw"
    PROCESSED = "processed"
    DERIVED = "derived"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class SurveyType(StrEnum):
    TWO_D = "2d"
    THREE_D = "3d"
    FOUR_D = "4d"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class WellDataType(StrEnum):
    LOG = "log"
    TRAJECTORY = "trajectory"
    WELLBORE = "wellbore"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class TrustLevel(StrEnum):
    AUTHORITATIVE = "authoritative"
    VERIFIED = "verified"
    DERIVED = "derived"
    HEURISTIC = "heuristic"
    UNTRUSTED = "untrusted"


class ToolErrorCategory(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    UNSUPPORTED = "unsupported"
    PARSE = "parse"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    IO = "io"
    INTERNAL = "internal"


class SideEffectKind(StrEnum):
    NONE = "none"
    POLICY_WRITE = "policy_write"
    FILE_WRITE = "file_write"
    FILE_EXPORT = "file_export"
    STATE_MUTATION = "state_mutation"
    CACHE_WRITE = "cache_write"
    JOB_EVENT = "job_event"


class ReviewStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"


class ReviewDecisionValue(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_CHANGES = "needs_changes"


class ReviewTargetType(StrEnum):
    GENERATED_CANDIDATE = "generated_candidate"
    MANIFEST_ASSOCIATION = "manifest_association"


class GenerationStatus(StrEnum):
    PROPOSED = "proposed"
    GENERATED = "generated"
    EXISTING = "existing"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ValidationStatus(StrEnum):
    NOT_RUN = "not_run"
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class LearningModelStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class JobEventType(StrEnum):
    CREATED = "job.created"
    STARTED = "job.started"
    STAGE_CHANGED = "job.stage_changed"
    ITEM_STARTED = "job.item_started"
    PROGRESSED = "job.progressed"
    ITEM_SUCCEEDED = "job.item_succeeded"
    ITEM_FAILED = "job.item_failed"
    CANCELLATION_REQUESTED = "job.cancellation_requested"
    CANCELLED = "job.cancelled"
    SUCCEEDED = "job.succeeded"
    PARTIALLY_SUCCEEDED = "job.partially_succeeded"
    FAILED = "job.failed"
    INTERRUPTED = "job.interrupted"


class PathClassification(StrEnum):
    WORKSPACE_RELATIVE = "workspace_relative"
    APPROVED_ABSOLUTE_ROOT = "approved_absolute_root"
    UNC = "unc"
    DEVICE = "device"
    DRIVE_RELATIVE = "drive_relative"
    TRAVERSAL = "traversal"
    LINK = "link"
    REPARSE_POINT = "reparse_point"
    PROHIBITED = "prohibited"


class ActorRef(ContractModel):
    actor_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w.@:-]+$")]
    display_name: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class ClassificationDimensions(ContractModel):
    inline_count: Annotated[int, Field(ge=0)] | None = None
    crossline_count: Annotated[int, Field(ge=0)] | None = None
    trace_count: Annotated[int, Field(ge=0)] | None = None
    sample_count: Annotated[int, Field(ge=0)] | None = None
    row_count: Annotated[int, Field(ge=0)] | None = None
    column_count: Annotated[int, Field(ge=0)] | None = None
    frame_count: Annotated[int, Field(ge=0)] | None = None
    channel_count: Annotated[int, Field(ge=0)] | None = None


class EvidenceRecord(ContractModel):
    evidence_id: EvidenceId
    evidence_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    rule_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9][A-Z0-9_.-]*$")]
    summary: SafeText
    location: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    observed_value: Annotated[str, Field(max_length=1024)] | None = None
    trust_level: TrustLevel
    source_file_id: FileId | None = None


class ProvenanceRecord(ContractModel):
    provenance_id: ProvenanceId
    source_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    source_ref: Annotated[str, Field(min_length=1, max_length=512)]
    tool_id: ToolId
    tool_version: SemanticVersion
    recorded_at: Rfc3339Timestamp
    source_sha256: Sha256 | None = None
    parent_provenance_ids: tuple[ProvenanceId, ...] = ()
    transformation: Annotated[str, Field(min_length=1, max_length=512)] | None = None


class ToolError(ContractModel):
    code: StableCode
    category: ToolErrorCategory
    message: SafeText
    retryable: bool
    path: WorkspaceRelativePath | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value)
        return _freeze_json_dict(value)


class SideEffectRecord(ContractModel):
    side_effect_id: UUID
    kind: SideEffectKind
    target: Annotated[str, Field(min_length=1, max_length=256)]
    description: SafeText
    occurred_at: Rfc3339Timestamp
    idempotency_key: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    prior_version: Annotated[int, Field(ge=0)] | None = None
    new_version: Annotated[int, Field(ge=0)] | None = None


class FormatCandidate(ContractModel):
    format_id: FormatId
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    evidence_ids: tuple[EvidenceId, ...]


class FormatDetectionResult(ContractModel):
    detection_id: UUID = Field(default_factory=uuid4)
    file_id: FileId
    candidates: tuple[FormatCandidate, ...]
    detector_version: SemanticVersion
    detected_at: Rfc3339Timestamp | None = None

    @model_validator(mode="after")
    def candidates_are_ranked(self) -> FormatDetectionResult:
        confidences = [candidate.confidence for candidate in self.candidates]
        if confidences != sorted(confidences, reverse=True):
            raise ValueError("format candidates must be ranked by descending confidence")
        if len({candidate.format_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("format candidates must have unique format identifiers")
        return self


class ClassificationRecord(ContractModel):
    classification_id: UUID
    file_id: FileId
    format_id: FormatId | None
    category: DataCategory
    subtype: DataSubtype
    dimensions: ClassificationDimensions
    stack: StackType
    domain: DataDomain
    processing: ProcessingLevel
    survey: SurveyType
    well: WellDataType
    osdu_kind: OSDUKind | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    detection_id: UUID
    extraction_ids: tuple[UUID, ...]
    evidence_ids: tuple[EvidenceId, ...]
    trust_level: TrustLevel


class WorkspaceRef(ContractModel):
    workspace_id: WorkspaceId
    canonical_root: ApprovedAbsolutePath
    read_only: bool = True
    allowed_output_subpaths: tuple[WorkspaceRelativePath, ...] = ()
    policy_fingerprint: Sha256
    created_by: ActorRef
    created_at: Rfc3339Timestamp


class FileAssetRef(ContractModel):
    file_id: FileId
    workspace_id: WorkspaceId
    relative_path: WorkspaceRelativePath
    size_bytes: Annotated[int, Field(ge=0)]
    modified_at: Rfc3339Timestamp
    sha256: Sha256 | None = None
    discovery_version: Annotated[int, Field(ge=1)]


class FileSampleRef(ContractModel):
    sample_id: UUID
    file_id: FileId
    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(ge=0)]
    sha256: Sha256
    encoding: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class MetadataExtractionRef(ContractModel):
    extraction_id: UUID
    file_id: FileId
    file_sha256: Sha256
    format_id: FormatId
    parser_version: SemanticVersion
    extracted_at: Rfc3339Timestamp
    evidence_ids: tuple[EvidenceId, ...] = ()
    provenance_ids: tuple[ProvenanceId, ...] = ()
    trust_level: TrustLevel


class ManifestDocumentRef(ContractModel):
    manifest_id: ManifestId
    path: WorkspaceRelativePath
    sha256: Sha256
    document_kind: OSDUKind | None = None
    normalized_content_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    generated: bool = False


class ManifestRecordRef(ContractModel):
    manifest_id: ManifestId
    record_id: Annotated[str, Field(min_length=1, max_length=512)]
    kind: OSDUKind
    json_pointer: JsonPointer
    surrogate_ids: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = ()


class DatasetReference(ContractModel):
    manifest_id: ManifestId
    record_id: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    value: Annotated[str, Field(min_length=1, max_length=2048)]
    normalized_value: Annotated[str, Field(min_length=1, max_length=2048)]
    json_pointer: JsonPointer
    relative_path: WorkspaceRelativePath | None = None


class ManifestAssociation(ContractModel):
    association_id: UUID
    file_id: FileId
    manifest_id: ManifestId
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    method: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")]
    evidence_ids: tuple[EvidenceId, ...]
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    trust_level: TrustLevel = TrustLevel.HEURISTIC
    target_version: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class LearningExampleRef(ContractModel):
    example_id: UUID
    source_file_id: FileId
    manifest_id: ManifestId
    association_id: UUID
    source_sha256: Sha256
    manifest_sha256: Sha256
    review_status: ReviewStatus
    generated_manifest: bool = False

    @model_validator(mode="after")
    def generated_examples_are_ineligible(self) -> LearningExampleRef:
        if self.generated_manifest:
            raise ValueError("generated manifests are not eligible learning examples")
        if self.review_status is not ReviewStatus.APPROVED:
            raise ValueError("learning examples require an approved association")
        return self


class LearningModelVersionRef(ContractModel):
    learning_model_id: UUID
    category: DataCategory
    version: Annotated[int, Field(ge=1)]
    status: LearningModelStatus
    example_ids: tuple[UUID, ...]
    model_sha256: Sha256


class GeneratedCandidateRef(ContractModel):
    candidate_id: UUID
    source_file_id: FileId
    learning_model_id: UUID
    candidate_sha256: Sha256
    proposed_path: WorkspaceRelativePath
    generation_status: GenerationStatus = GenerationStatus.PROPOSED
    validation_status: ValidationStatus = ValidationStatus.NOT_RUN
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    trust_level: TrustLevel = TrustLevel.HEURISTIC
    review_required: bool = True

    @model_validator(mode="after")
    def preserve_generated_candidate_trust(self) -> GeneratedCandidateRef:
        if self.trust_level is not TrustLevel.HEURISTIC or not self.review_required:
            raise ValueError("generated candidates must be heuristic and review-required")
        return self


class ManifestJsonDocument(ContractModel):
    sha256: Sha256
    content: dict[str, JsonValue]

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(
            value,
            max_depth=32,
            max_items=100_000,
            max_string_length=1_048_576,
            reject_secret_keys=False,
        )
        return _freeze_json_dict(value)


class GeneratedManifestCandidate(ContractModel):
    reference: GeneratedCandidateRef
    document: ManifestJsonDocument

    @model_validator(mode="after")
    def content_hash_matches_reference(self) -> GeneratedManifestCandidate:
        if self.document.sha256 != self.reference.candidate_sha256:
            raise ValueError("generated document hash must match the candidate reference")
        return self


class SchemaCatalogRef(ContractModel):
    schema_catalog_id: UUID
    revision: Annotated[str, Field(min_length=1, max_length=256)]
    source: Annotated[str, Field(min_length=1, max_length=512)]
    catalog_sha256: Sha256
    active: bool
    activated_at: Rfc3339Timestamp | None = None


class JobRef(ContractModel):
    job_id: JobId
    job_type: Annotated[str, Field(min_length=1, max_length=128)]
    status: JobStatus
    stage: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    cancellation_requested: bool = False
    created_at: Rfc3339Timestamp
    updated_at: Rfc3339Timestamp


class JobEventRef(ContractModel):
    job_id: JobId
    sequence: Annotated[int, Field(ge=1)]
    event_type: JobEventType
    timestamp: Rfc3339Timestamp
    current_item: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    safe_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("safe_payload")
    @classmethod
    def validate_safe_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value)
        return _freeze_json_dict(value)

    @field_validator("counts")
    @classmethod
    def freeze_counts(cls, value: dict[str, int]) -> dict[str, int]:
        return cast(dict[str, int], _freeze_json(value))


class ReviewDecision(ContractModel):
    decision_id: UUID
    actor: ActorRef
    target_type: ReviewTargetType
    target_id: UUID
    target_version: Annotated[str, Field(min_length=1, max_length=128)]
    decision: ReviewDecisionValue
    reason: Annotated[str, Field(min_length=1, max_length=2048)]
    decided_at: Rfc3339Timestamp


class ReviewDecisionReceipt(ContractModel):
    decision_id: UUID
    target_id: UUID
    target_version: Annotated[str, Field(min_length=1, max_length=128)]
    review_status: ReviewStatus
    recorded_at: Rfc3339Timestamp
    state_version: Annotated[int, Field(ge=1)]


class AuditEventRef(ContractModel):
    audit_event_id: UUID
    request_id: RequestId
    actor: ActorRef
    tool_id: ToolId
    tool_version: SemanticVersion
    result_status: Annotated[
        str,
        Field(pattern=r"^(?:succeeded|partially_succeeded|cancelled|failed)$"),
    ]
    side_effect_ids: tuple[UUID, ...] = ()
    occurred_at: Rfc3339Timestamp


DOMAIN_MODELS: tuple[type[BaseModel], ...] = (
    WorkspaceRelativePath,
    ApprovedAbsolutePath,
    OSDUKind,
    ActorRef,
    ClassificationDimensions,
    EvidenceRecord,
    ProvenanceRecord,
    ToolError,
    SideEffectRecord,
    FormatCandidate,
    FormatDetectionResult,
    ClassificationRecord,
    WorkspaceRef,
    FileAssetRef,
    FileSampleRef,
    MetadataExtractionRef,
    ManifestDocumentRef,
    ManifestRecordRef,
    DatasetReference,
    ManifestAssociation,
    LearningExampleRef,
    LearningModelVersionRef,
    GeneratedCandidateRef,
    ManifestJsonDocument,
    GeneratedManifestCandidate,
    SchemaCatalogRef,
    JobRef,
    JobEventRef,
    ReviewDecision,
    ReviewDecisionReceipt,
    AuditEventRef,
)

__all__ = (
    "DOMAIN_MODELS",
    "ActorRef",
    "ApprovedAbsolutePath",
    "AuditEventRef",
    "ClassificationDimensions",
    "ClassificationRecord",
    "ContractModel",
    "DataCategory",
    "DataDomain",
    "DataSubtype",
    "DatasetReference",
    "EvidenceRecord",
    "FileAssetRef",
    "FileSampleRef",
    "FormatCandidate",
    "FormatDetectionResult",
    "FormatId",
    "GeneratedCandidateRef",
    "GeneratedManifestCandidate",
    "GenerationStatus",
    "JobEventRef",
    "JobEventType",
    "JobRef",
    "JobStatus",
    "LearningExampleRef",
    "LearningModelStatus",
    "LearningModelVersionRef",
    "ManifestAssociation",
    "ManifestDocumentRef",
    "ManifestJsonDocument",
    "ManifestRecordRef",
    "MetadataExtractionRef",
    "OSDUKind",
    "PathClassification",
    "ProcessingLevel",
    "ProvenanceRecord",
    "ReviewDecision",
    "ReviewDecisionReceipt",
    "ReviewDecisionValue",
    "ReviewStatus",
    "ReviewTargetType",
    "Rfc3339Timestamp",
    "SafeText",
    "SchemaCatalogRef",
    "SemanticVersion",
    "Sha256",
    "SideEffectKind",
    "SideEffectRecord",
    "StableCode",
    "StackType",
    "SurveyType",
    "ToolError",
    "ToolErrorCategory",
    "ToolId",
    "TrustLevel",
    "ValidationStatus",
    "WellDataType",
    "WorkspaceRef",
    "WorkspaceRelativePath",
)
