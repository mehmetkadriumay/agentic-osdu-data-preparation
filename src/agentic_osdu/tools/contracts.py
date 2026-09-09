"""Common tool envelopes and the complete typed TOOL-001..TOOL-030 catalog."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator

from agentic_osdu.domain.models import (
    ActorRef,
    ApprovedAbsolutePath,
    ClassificationDimensions,
    ClassificationRecord,
    ContractModel,
    DataCategory,
    DataDomain,
    DatasetReference,
    EvidenceRecord,
    FileAssetRef,
    FileSampleRef,
    FormatDetectionResult,
    FormatId,
    GeneratedManifestCandidate,
    JobEventRef,
    JobRef,
    LearningExampleRef,
    ManifestAssociation,
    ManifestDocumentRef,
    ManifestJsonDocument,
    ManifestRecordRef,
    MetadataExtractionRef,
    OSDUKind,
    ProvenanceRecord,
    ReviewDecision,
    ReviewDecisionReceipt,
    ReviewStatus,
    Rfc3339Timestamp,
    SchemaCatalogRef,
    SemanticVersion,
    Sha256,
    SideEffectRecord,
    ToolError,
    TrustLevel,
    ValidationStatus,
    WorkspaceRelativePath,
)


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AccessMode(StrEnum):
    READ_ONLY = "read_only"
    POLICY_WRITE = "policy_write"
    TRANSACTIONAL_WRITE = "transactional_write"
    ATOMIC_FILE_WRITE = "atomic_file_write"
    MIXED = "mixed"


class SideEffectProfile(StrEnum):
    NONE = "none"
    WORKSPACE_POLICY = "workspace_policy"
    VERSIONED_CACHE = "versioned_cache"
    INVENTORY_STATE = "inventory_state"
    ASSOCIATION_STATE = "association_state"
    LEARNING_STATE = "learning_state"
    JOB_STATE = "job_state"
    REVIEW_STATE = "review_state"
    GENERATED_FILES = "generated_files"
    EXPORT_FILES = "export_files"


class SampleMode(StrEnum):
    PREFIX = "prefix"
    RANGE = "range"
    TEXT_SAMPLE = "text_sample"


class EncodingPolicy(StrEnum):
    BINARY = "binary"
    STRICT_UTF8 = "strict_utf8"
    DETECT_BOUNDED = "detect_bounded"


class InterpretationSubtype(StrEnum):
    SGP = "sgp"
    DAT = "dat"
    TEXT = "text"
    PDF = "pdf"


class SchemaCatalogSource(StrEnum):
    LOCAL_EXPORT = "local_export"
    APPROVED_REMOTE = "approved_remote"


class LearningMutationAction(StrEnum):
    CREATE = "create"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    CLEAR = "clear"


class JobControlAction(StrEnum):
    QUERY = "query"
    CANCEL = "cancel"


class ExportKind(StrEnum):
    REPORT = "report"
    APPROVED_MANIFEST = "approved_manifest"


class ToolRequest[InputT: BaseModel](ContractModel):
    """Exact common input envelope for every deterministic tool."""

    request_id: UUID
    workspace_id: UUID
    actor: ActorRef
    input: InputT
    cancellation_token_id: UUID | None = None
    expected_state_version: int | None = Field(default=None, ge=0)


class ToolResult[OutputT: BaseModel](ContractModel):
    """Exact common output envelope for every deterministic tool."""

    request_id: UUID
    tool_id: str = Field(pattern=r"^TOOL-(?:00[1-9]|0[12]\d|030)$")
    tool_version: SemanticVersion
    status: ToolResultStatus
    output: OutputT | None = None
    errors: tuple[ToolError, ...] = ()
    provenance: tuple[ProvenanceRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    trust_level: TrustLevel
    side_effects: tuple[SideEffectRecord, ...] = ()
    started_at: Rfc3339Timestamp
    finished_at: Rfc3339Timestamp

    @model_validator(mode="after")
    def validate_result_invariants(self) -> ToolResult[OutputT]:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if not self.provenance or not self.evidence:
            raise ValueError("every tool result requires provenance and evidence")
        if self.status is ToolResultStatus.SUCCEEDED:
            if self.output is None:
                raise ValueError("a succeeded result requires output")
            if self.errors:
                raise ValueError("a succeeded result must not contain errors")
        if self.status is ToolResultStatus.PARTIALLY_SUCCEEDED and (
            self.output is None or not self.errors
        ):
            raise ValueError("a partially succeeded result requires output and errors")
        if self.status is ToolResultStatus.CANCELLED and not self.errors:
            raise ValueError("a cancelled result requires a structured cancellation error")
        if self.status is ToolResultStatus.FAILED:
            if self.output is not None:
                raise ValueError("a failed result must not contain output")
            if not self.errors:
                raise ValueError("a failed result requires at least one structured error")
        return self


class RegisterWorkspaceInput(ContractModel):
    root_path: ApprovedAbsolutePath
    read_only: bool = True
    allowed_output_subpaths: tuple[WorkspaceRelativePath, ...] = ()

    @model_validator(mode="after")
    def require_read_only_source(self) -> RegisterWorkspaceInput:
        if not self.read_only:
            raise ValueError("source workspaces must be read-only")
        return self


class WorkspaceDescriptor(ContractModel):
    workspace_id: UUID
    canonical_root: ApprovedAbsolutePath
    read_only: bool
    allowed_output_subpaths: tuple[WorkspaceRelativePath, ...]
    policy_fingerprint: Sha256


class DiscoverFilesInput(ContractModel):
    workspace_id: UUID
    include_paths: tuple[WorkspaceRelativePath, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    max_files: int = Field(default=10_000, ge=1, le=1_000_000)
    max_total_bytes: int = Field(default=1 << 40, gt=0, le=1 << 50)
    follow_symlinks: bool = False


class DiscoveryBatch(ContractModel):
    discovery_id: UUID
    workspace_id: UUID
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    snapshot_at: Rfc3339Timestamp


class DiscoveryOutput(ContractModel):
    batch: DiscoveryBatch
    files: tuple[FileAssetRef, ...]


class ReadFileSampleInput(ContractModel):
    file_id: UUID
    mode: SampleMode
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    encoding_policy: EncodingPolicy = EncodingPolicy.BINARY


class FileSampleOutput(ContractModel):
    sample: FileSampleRef
    content_base64: str = Field(min_length=1, max_length=24 * 1024 * 1024)
    text_encoding: str | None = Field(default=None, max_length=64)
    truncated: bool

    def decoded_bytes(self) -> bytes:
        """Decode the bounded transport representation."""

        return base64.b64decode(self.content_base64, validate=True)


class DetectFormatInput(ContractModel):
    file_id: UUID
    sample_refs: tuple[UUID, ...]


class DetectFormatOutput(ContractModel):
    detection: FormatDetectionResult


class ClassifyDataInput(ContractModel):
    file_id: UUID
    detection: FormatDetectionResult
    extracted_metadata: tuple[ExtractedMetadataContract, ...] = ()


class ClassifyDataOutput(ContractModel):
    classification: ClassificationRecord


class SegyExtractionOptions(ContractModel):
    max_trace_samples: int = Field(default=32, ge=1, le=1024)
    inspect_textual_header: bool = True
    endian_hint: str | None = Field(default=None, pattern=r"^(?:big|little)$")


class ExtractSegyInput(ContractModel):
    file_id: UUID
    options: SegyExtractionOptions = Field(default_factory=SegyExtractionOptions)


class SegyBinaryHeader(ContractModel):
    job_id: int | None = None
    line_number: int | None = None
    reel_number: int | None = None
    data_traces_per_ensemble: int | None = Field(default=None, ge=0)
    auxiliary_traces_per_ensemble: int | None = Field(default=None, ge=0)
    sample_format_code: int | None = Field(default=None, ge=0)
    fixed_length_trace_flag: int | None = Field(default=None, ge=0, le=1)


class SegySurveyMetadata(ContractModel):
    survey_name: str | None = Field(default=None, max_length=256)
    coordinate_units: str | None = Field(default=None, max_length=64)
    measurement_system: str | None = Field(default=None, max_length=64)


class SegyTraceSample(ContractModel):
    trace_index: int = Field(ge=0)
    byte_offset: int = Field(ge=0)
    sample_count: int = Field(ge=0)
    inline_number: int | None = None
    crossline_number: int | None = None


class SegyMetadata(ContractModel):
    endian: str = Field(pattern=r"^(?:big|little|ambiguous)$")
    textual_header_encoding: str | None = Field(default=None, max_length=64)
    sample_interval_microseconds: int | None = Field(default=None, ge=0)
    samples_per_trace: int | None = Field(default=None, ge=0)
    sampled_trace_count: int = Field(ge=0)
    domain: DataDomain
    dimensions: ClassificationDimensions
    binary_header: SegyBinaryHeader
    survey_metadata: SegySurveyMetadata
    trace_samples: tuple[SegyTraceSample, ...]


class LasExtractionOptions(ContractModel):
    max_lines: int = Field(default=100_000, ge=1, le=10_000_000)
    include_curve_descriptions: bool = True
    encoding: str | None = Field(default=None, max_length=64)


class ExtractLasInput(ContractModel):
    file_id: UUID
    options: LasExtractionOptions = Field(default_factory=LasExtractionOptions)


class LasCurveMetadata(ContractModel):
    mnemonic: str = Field(min_length=1, max_length=64)
    unit: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=512)


class LasMetadata(ContractModel):
    version: str | None = Field(default=None, max_length=64)
    well_name: str | None = Field(default=None, max_length=256)
    sections: tuple[str, ...]
    curves: tuple[LasCurveMetadata, ...]
    row_count: int | None = Field(default=None, ge=0)


class JsonWellLogOptions(ContractModel):
    max_curves: int = Field(default=10_000, ge=1, le=100_000)
    max_rows: int = Field(default=10_000_000, ge=1)
    validate_cell_types: bool = True
    validate_index: bool = True


class ExtractJsonWellLogInput(ContractModel):
    file_id: UUID
    options: JsonWellLogOptions = Field(default_factory=JsonWellLogOptions)


class JsonCurveMetadata(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    unit: str | None = Field(default=None, max_length=64)
    value_type: str | None = Field(default=None, max_length=64)


class JsonLogSetMetadata(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    well_name: str | None = Field(default=None, max_length=256)
    curves: tuple[JsonCurveMetadata, ...]
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    index_curve: str | None = Field(default=None, max_length=256)
    data_uri: str | None = Field(default=None, min_length=1, max_length=2048)


class JsonWellLogMetadata(ContractModel):
    well_name: str | None = Field(default=None, max_length=256)
    curves: tuple[JsonCurveMetadata, ...]
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    index_curve: str | None = Field(default=None, max_length=256)
    log_set_count: int = Field(default=1, ge=1)
    log_sets: tuple[JsonLogSetMetadata, ...] = ()


class DlisExtractionOptions(ContractModel):
    max_logical_files: int = Field(default=128, ge=1, le=10_000)
    max_channels: int = Field(default=100_000, ge=1)


class ExtractDlisInput(ContractModel):
    file_id: UUID
    options: DlisExtractionOptions = Field(default_factory=DlisExtractionOptions)


class DlisFrameMetadata(ContractModel):
    logical_file_id: str = Field(min_length=1, max_length=256)
    frame_id: str = Field(min_length=1, max_length=256)
    channel_count: int = Field(ge=0)


class DlisChannelMetadata(ContractModel):
    logical_file_id: str = Field(min_length=1, max_length=256)
    channel_id: str = Field(min_length=1, max_length=256)
    mnemonic: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=64)


class DlisOriginMetadata(ContractModel):
    logical_file_id: str = Field(min_length=1, max_length=256)
    origin_id: str = Field(min_length=1, max_length=256)
    well_name: str | None = Field(default=None, max_length=256)


class DlisMetadata(ContractModel):
    logical_file_ids: tuple[str, ...]
    frames: tuple[DlisFrameMetadata, ...]
    channels: tuple[DlisChannelMetadata, ...]
    origins: tuple[DlisOriginMetadata, ...]
    well_name: str | None = Field(default=None, max_length=256)
    library_version: str = Field(min_length=1, max_length=128)


class LisExtractionOptions(ContractModel):
    max_records: int = Field(default=1_000_000, ge=1)
    encoding: str | None = Field(default=None, max_length=64)


class ExtractLisInput(ContractModel):
    file_id: UUID
    options: LisExtractionOptions = Field(default_factory=LisExtractionOptions)


class LisMetadata(ContractModel):
    record_count: int = Field(ge=0)
    record_types: tuple[str, ...]
    well_name: str | None = Field(default=None, max_length=256)
    truncated: bool = False


class CsvExtractionOptions(ContractModel):
    max_sample_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    delimiter_hint: str | None = Field(default=None, min_length=1, max_length=1)
    encoding: str | None = Field(default=None, max_length=64)


class ExtractCsvInput(ContractModel):
    file_id: UUID
    options: CsvExtractionOptions = Field(default_factory=CsvExtractionOptions)


class CsvRowSample(ContractModel):
    row_number: int = Field(ge=1)
    values: Annotated[tuple[str | None, ...], Field(max_length=1024)]


class CsvMetadata(ContractModel):
    delimiter: str = Field(min_length=1, max_length=1)
    headers: tuple[str, ...]
    sampled_row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    rows_consistent: bool
    sampled_rows: tuple[CsvRowSample, ...]


class P190ExtractionOptions(ContractModel):
    max_positions: int = Field(default=100_000, ge=1)
    infer_epsg: bool = True


class ExtractP190Input(ContractModel):
    file_id: UUID
    options: P190ExtractionOptions = Field(default_factory=P190ExtractionOptions)


class NavigationPosition(ContractModel):
    line_name: str = Field(min_length=1, max_length=128)
    point_number: int = Field(ge=0)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    easting: float | None = None
    northing: float | None = None
    water_depth: float | None = None


class P190HeaderField(ContractModel):
    code: str = Field(min_length=1, max_length=16)
    value: str = Field(max_length=512)
    line_number: int = Field(ge=1)


class P190LineSummary(ContractModel):
    line_name: str = Field(min_length=1, max_length=128)
    position_count: int = Field(ge=0)
    first_point_number: int | None = Field(default=None, ge=0)
    last_point_number: int | None = Field(default=None, ge=0)
    point_increment: int | None = None
    easting_min: float | None = None
    easting_max: float | None = None
    northing_min: float | None = None
    northing_max: float | None = None
    water_depth_min: float | None = None
    water_depth_max: float | None = None


class P190Metadata(ContractModel):
    headers: tuple[P190HeaderField, ...]
    line_names: tuple[str, ...]
    line_summaries: tuple[P190LineSummary, ...]
    position_count: int = Field(ge=0)
    sampled_positions: tuple[NavigationPosition, ...]
    inferred_epsg: int | None = Field(default=None, ge=1)


class InterpretationExtractionOptions(ContractModel):
    max_rows: int = Field(default=1_000_000, ge=1)
    companion_crs_path: WorkspaceRelativePath | None = None
    text_encoding: str | None = Field(default=None, max_length=64)


class ExtractInterpretationInput(ContractModel):
    file_id: UUID
    subtype: InterpretationSubtype
    options: InterpretationExtractionOptions = Field(
        default_factory=InterpretationExtractionOptions
    )


class SgpMetadata(ContractModel):
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    domain: DataDomain
    sampled_row_count: int = Field(default=0, ge=0)
    truncated: bool = False


class DatMetadata(ContractModel):
    interpretation_type: str = Field(pattern=r"^(?:horizon|fault|unknown)$")
    point_count: int = Field(ge=0)
    crs: str | None = Field(default=None, max_length=128)
    sampled_point_count: int = Field(default=0, ge=0)
    truncated: bool = False


class TextMetadata(ContractModel):
    encoding: str = Field(min_length=1, max_length=64)
    line_count: int = Field(ge=0)
    truncated: bool


class PdfMetadata(ContractModel):
    signature_valid: bool
    version: str | None = Field(default=None, max_length=32)
    size_bytes: int = Field(ge=0)


class InterpretationMetadataOutput(ContractModel):
    subtype: InterpretationSubtype
    sgp: SgpMetadata | None = None
    dat: DatMetadata | None = None
    text: TextMetadata | None = None
    pdf: PdfMetadata | None = None

    @model_validator(mode="after")
    def exactly_one_payload_matches_subtype(self) -> InterpretationMetadataOutput:
        payloads = {
            InterpretationSubtype.SGP: self.sgp,
            InterpretationSubtype.DAT: self.dat,
            InterpretationSubtype.TEXT: self.text,
            InterpretationSubtype.PDF: self.pdf,
        }
        if sum(payload is not None for payload in payloads.values()) != 1:
            raise ValueError("exactly one subtype metadata payload is required")
        if payloads[self.subtype] is None:
            raise ValueError("metadata payload must match the requested subtype")
        return self


class ExtractedMetadataContract(ContractModel):
    reference: MetadataExtractionRef
    payload: (
        SegyMetadata
        | LasMetadata
        | JsonWellLogMetadata
        | DlisMetadata
        | LisMetadata
        | CsvMetadata
        | P190Metadata
        | InterpretationMetadataOutput
    )

    @model_validator(mode="after")
    def payload_matches_format(self) -> ExtractedMetadataContract:
        expected_types: dict[FormatId, type[BaseModel]] = {
            FormatId.SEGY: SegyMetadata,
            FormatId.LAS: LasMetadata,
            FormatId.JSON_WELL_LOG: JsonWellLogMetadata,
            FormatId.DLIS: DlisMetadata,
            FormatId.LIS_LTI: LisMetadata,
            FormatId.CSV: CsvMetadata,
            FormatId.P190: P190Metadata,
            FormatId.SGP: InterpretationMetadataOutput,
            FormatId.DAT: InterpretationMetadataOutput,
            FormatId.TEXT: InterpretationMetadataOutput,
            FormatId.PDF: InterpretationMetadataOutput,
        }
        expected = expected_types.get(self.reference.format_id)
        if expected is None or not isinstance(self.payload, expected):
            raise ValueError("metadata payload must match its extraction format")
        return self


class ParseManifestsInput(ContractModel):
    workspace_id: UUID
    manifest_root: WorkspaceRelativePath
    paths: tuple[WorkspaceRelativePath, ...] = ()
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)


class ParsedManifest(ContractModel):
    document: ManifestDocumentRef
    content: ManifestJsonDocument
    parser_version: SemanticVersion
    parsed_at: Rfc3339Timestamp

    @model_validator(mode="after")
    def content_matches_document(self) -> ParsedManifest:
        if self.content.sha256 != self.document.sha256:
            raise ValueError("parsed manifest content hash must match its document reference")
        return self


class ParseManifestsOutput(ContractModel):
    manifests: tuple[ParsedManifest, ...]


class ExtractManifestRecordsInput(ContractModel):
    manifest: ParsedManifest


class ManifestRecordSet(ContractModel):
    manifest_id: UUID
    records: tuple[ManifestRecordRef, ...]


class ManifestComponentRelationship(ContractModel):
    source_record_id: str = Field(min_length=1, max_length=512)
    target_record_id: str = Field(min_length=1, max_length=512)
    relationship_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    json_pointer: str = Field(max_length=2048)


class ExtractManifestRecordsOutput(ContractModel):
    record_set: ManifestRecordSet
    dataset_references: tuple[DatasetReference, ...]
    component_relationships: tuple[ManifestComponentRelationship, ...]


class MatchingPolicyVersion(ContractModel):
    version: SemanticVersion
    policy_sha256: Sha256


class FileRecordContract(ContractModel):
    file: FileAssetRef
    detection: FormatDetectionResult | None = None
    classification: ClassificationRecord | None = None
    metadata_extractions: tuple[ExtractedMetadataContract, ...] = ()


class ManifestIndexContract(ContractModel):
    manifest_index_id: UUID
    manifest_ids: tuple[UUID, ...]
    manifest_documents: tuple[ManifestDocumentRef, ...] = ()
    records: tuple[ManifestRecordRef, ...]
    dataset_references: tuple[DatasetReference, ...]
    component_relationships: tuple[ManifestComponentRelationship, ...]
    index_sha256: Sha256
    built_at: Rfc3339Timestamp


class MatchManifestInput(ContractModel):
    file_record: FileRecordContract
    manifest_index: ManifestIndexContract
    matching_policy: MatchingPolicyVersion


class MatchManifestOutput(ContractModel):
    matches: tuple[ManifestAssociation, ...]


class LearnManifestPatternsInput(ContractModel):
    examples: tuple[LearningExampleRef, ...]
    category: DataCategory
    learning_policy_version: SemanticVersion


class LearningMaterialSnapshot(ContractModel):
    """Normalized material retained so category learning remains cumulative."""

    example_id: UUID
    source_path: WorkspaceRelativePath
    manifest: ManifestJsonDocument


class LearningModelContract(ContractModel):
    learning_model_id: UUID
    category: DataCategory
    version: int = Field(ge=1)
    model_sha256: Sha256
    example_ids: tuple[UUID, ...]
    example_identities: tuple[LearningExampleRef, ...]
    material_snapshots: tuple[LearningMaterialSnapshot, ...] = ()
    prototype: ManifestJsonDocument
    constants: tuple[LearningConstant, ...]
    prototype_source_path: WorkspaceRelativePath
    file_source_prefix: str = Field(default="", max_length=2048)
    work_product_envelope: dict[str, JsonValue]
    component_envelope: dict[str, JsonValue]
    dataset_envelope: dict[str, JsonValue]

    @model_validator(mode="after")
    def require_complete_example_identities(self) -> LearningModelContract:
        if self.example_ids != tuple(item.example_id for item in self.example_identities):
            raise ValueError("example_ids must exactly match persisted example identities")
        if self.material_snapshots and self.example_ids != tuple(
            item.example_id for item in self.material_snapshots
        ):
            raise ValueError("material_snapshots must exactly match persisted example identities")
        return self


class LearningConstant(ContractModel):
    json_pointer: str = Field(max_length=2048)
    value: str | int | float | bool | None
    source_example_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]


class LearningDelta(ContractModel):
    added_example_ids: tuple[UUID, ...]
    ignored_duplicate_ids: tuple[UUID, ...]
    conflicts: tuple[str, ...] = ()


class LearnManifestPatternsOutput(ContractModel):
    model: LearningModelContract
    delta: LearningDelta


class GenerateManifestInput(ContractModel):
    file_id: UUID
    learning_model_id: UUID
    generation_policy_version: SemanticVersion
    dry_run: bool = True


class ValidationPreflight(ContractModel):
    status: ValidationStatus
    error_count: int = Field(ge=0)
    schema_catalog_id: UUID | None = None


class GenerateManifestOutput(ContractModel):
    candidate: GeneratedManifestCandidate
    validation_preflight: ValidationPreflight


class GenerationFilters(ContractModel):
    categories: tuple[DataCategory, ...] = ()
    format_ids: tuple[FormatId, ...] = ()
    relative_prefixes: tuple[WorkspaceRelativePath, ...] = ()


class GenerateAllManifestsInput(ContractModel):
    inventory_id: UUID
    generation_policy_version: SemanticVersion
    filters: GenerationFilters = Field(default_factory=GenerationFilters)
    continue_on_error: bool = False
    dry_run: bool = True


class GenerationBatchResult(ContractModel):
    generated: int = Field(ge=0)
    existing: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    candidates: tuple[GeneratedManifestCandidate, ...] = ()


class ValidateSchemasInput(ContractModel):
    manifest: ParsedManifest | GeneratedManifestCandidate
    schema_catalog_id: UUID
    max_errors: int = Field(default=100, ge=1, le=10_000)


class ValidationIssue(ContractModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    json_pointer: str = Field(max_length=2048)
    message: str = Field(min_length=1, max_length=1024)
    scope: str = Field(min_length=1, max_length=512)
    kind: OSDUKind | None = None
    validator: str | None = Field(default=None, max_length=128)
    schema_uri: str | None = Field(default=None, max_length=2048)


class ValidationReport(ContractModel):
    status: ValidationStatus
    schema_conformant: bool | None
    semantic_correctness: Literal["not_assessed"] = "not_assessed"
    schema_catalog_id: UUID
    schema_revision: str = Field(min_length=1, max_length=256)
    catalog_sha256: Sha256
    catalog_source: str = Field(min_length=1, max_length=512)
    document_kind: OSDUKind | None
    issues: tuple[ValidationIssue, ...]
    validated_record_count: int = Field(ge=0)
    validated_schemas: tuple[str, ...] = ()
    errors_truncated: bool = False


class SchemaChecksum(ContractModel):
    relative_path: WorkspaceRelativePath
    sha256: Sha256


class LocalSchemaCatalogImport(ContractModel):
    source: Literal[SchemaCatalogSource.LOCAL_EXPORT]
    revision: str = Field(min_length=1, max_length=256)
    local_root: ApprovedAbsolutePath
    expected_checksums: Annotated[tuple[SchemaChecksum, ...], Field(min_length=1)]


class ApprovedRemoteSchemaCatalogRefresh(ContractModel):
    source: Literal[SchemaCatalogSource.APPROVED_REMOTE]
    revision: str = Field(min_length=1, max_length=256)
    remote_uri: str = Field(
        min_length=9,
        max_length=2048,
        pattern=r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[^\\\s]*)?$",
    )
    expected_checksums: Annotated[tuple[SchemaChecksum, ...], Field(min_length=1)]
    network_approval_id: UUID


class RefreshSchemaCatalogInput(
    RootModel[
        Annotated[
            LocalSchemaCatalogImport | ApprovedRemoteSchemaCatalogRefresh,
            Field(discriminator="source"),
        ]
    ]
):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RefreshSchemaCatalogOutput(ContractModel):
    catalog: SchemaCatalogRef


class InventoryMutation(ContractModel):
    inventory_id: UUID
    files: tuple[FileAssetRef, ...] = ()
    detections: tuple[FormatDetectionResult, ...] = ()
    extractions: tuple[MetadataExtractionRef, ...] = ()
    classifications: tuple[ClassificationRecord, ...] = ()
    remove_file_ids: tuple[UUID, ...] = ()


class PersistInventoryInput(ContractModel):
    mutation: InventoryMutation
    expected_state_version: int = Field(ge=0)


class InventorySnapshotRef(ContractModel):
    inventory_id: UUID
    state_version: int = Field(ge=1)
    file_count: int = Field(ge=0)
    created_at: Rfc3339Timestamp


class AssociationMutation(ContractModel):
    association: ManifestAssociation
    review_status: ReviewStatus
    reviewer_decision_id: UUID | None = None


class PersistAssociationsInput(ContractModel):
    mutations: tuple[AssociationMutation, ...]
    expected_state_version: int = Field(ge=0)


class AssociationSnapshotRef(ContractModel):
    state_version: int = Field(ge=1)
    association_ids: tuple[UUID, ...]
    recorded_at: Rfc3339Timestamp


class LearningModelMutation(ContractModel):
    action: LearningMutationAction
    learning_model_id: UUID | None = None
    model: LearningModelContract | None = None
    expected_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_action_fields(self) -> LearningModelMutation:
        if self.action is LearningMutationAction.CREATE:
            if self.model is None or self.learning_model_id is not None:
                raise ValueError("create requires a model and no separate model ID")
        elif self.action in {
            LearningMutationAction.ACTIVATE,
            LearningMutationAction.DEACTIVATE,
        }:
            if self.learning_model_id is None or self.model is not None:
                raise ValueError("activate and deactivate require only a model ID")
        elif self.model is not None:
            raise ValueError("clear must not include a model payload")
        return self


class PersistLearningModelInput(ContractModel):
    mutation: LearningModelMutation


class LearningModelVersionOutput(ContractModel):
    learning_model_id: UUID
    version: int = Field(ge=1)
    status: str = Field(pattern=r"^(?:draft|active|inactive)$")
    recorded_at: Rfc3339Timestamp


class JobStepDefinition(ContractModel):
    sequence: int = Field(ge=1)
    tool_id: str = Field(pattern=r"^TOOL-(?:00[1-9]|0[12]\d|030)$")
    input_ref: str = Field(min_length=1, max_length=512)


class JobDefinition(ContractModel):
    job_type: str = Field(min_length=1, max_length=128)
    steps: tuple[JobStepDefinition, ...]
    max_concurrency: int = Field(default=1, ge=1, le=64)
    continue_on_error: bool = False


class TrackJobInput(ContractModel):
    definition: JobDefinition
    deduplication_key: str = Field(min_length=1, max_length=256)


class TrackJobOutput(ContractModel):
    descriptor: JobDescriptor


class JobDescriptor(ContractModel):
    job: JobRef
    deduplication_key: str = Field(min_length=1, max_length=256)
    state_version: int = Field(ge=1)


class QueryOrCancelJobInput(ContractModel):
    action: JobControlAction
    job_id: UUID
    after_event_sequence: int | None = Field(default=None, ge=0)


class CancellationReceipt(ContractModel):
    job_id: UUID
    accepted: bool
    requested_at: Rfc3339Timestamp | None = None


class JobControlOutput(ContractModel):
    snapshot: JobSnapshot
    events: tuple[JobEventRef, ...]
    cancellation: CancellationReceipt | None = None


class JobSnapshot(ContractModel):
    job: JobRef
    last_event_sequence: int = Field(ge=0)
    counts: dict[str, int] = Field(default_factory=dict)


class ReviewQuery(ContractModel):
    search: str | None = Field(default=None, max_length=256)
    categories: tuple[DataCategory, ...] = ()
    review_statuses: tuple[ReviewStatus, ...] = ()
    sort_by: str = Field(default="relative_path", pattern=r"^(?:relative_path|format|status)$")
    descending: bool = False
    limit: int = Field(default=100, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0)


class BuildInventoryReviewInput(ContractModel):
    inventory_id: UUID
    query: ReviewQuery = Field(default_factory=ReviewQuery)


class InventoryReviewItem(ContractModel):
    file: FileAssetRef
    classification: ClassificationRecord | None = None
    association: ManifestAssociation | None = None


class ReviewSummaryCount(ContractModel):
    value: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=0)


class InventoryReviewView(ContractModel):
    inventory_id: UUID
    total_count: int = Field(ge=0)
    category_summaries: tuple[ReviewSummaryCount, ...]
    format_summaries: tuple[ReviewSummaryCount, ...]
    review_status_summaries: tuple[ReviewSummaryCount, ...]
    items: tuple[InventoryReviewItem, ...]


class BuildManifestReviewInput(ContractModel):
    manifest: ManifestDocumentRef | GeneratedManifestCandidate
    source_file_id: UUID | None = None


class GenerationDiff(ContractModel):
    changed_pointers: tuple[str, ...]
    added_pointers: tuple[str, ...]
    removed_pointers: tuple[str, ...]


class ManifestReviewView(ContractModel):
    manifest: ManifestDocumentRef | GeneratedManifestCandidate
    content: ManifestJsonDocument
    validation: ValidationReport | None = None
    provenance: tuple[ProvenanceRecord, ...] = ()
    association: ManifestAssociation | None = None
    generation_diff: GenerationDiff | None = None


class RecordReviewDecisionInput(ContractModel):
    decision: ReviewDecision


class RecordReviewDecisionOutput(ContractModel):
    receipt: ReviewDecisionReceipt


class ExportRequest(ContractModel):
    export_kind: ExportKind
    target_id: UUID
    output_root_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    relative_path: WorkspaceRelativePath
    expected_target_version: str = Field(min_length=1, max_length=128)


class ExportToolInput(ContractModel):
    request: ExportRequest


class ExportedFileRef(ContractModel):
    relative_path: WorkspaceRelativePath
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class ExportReceipt(ContractModel):
    export_id: UUID
    files: tuple[ExportedFileRef, ...]
    exported_at: Rfc3339Timestamp


class ExportToolOutput(ContractModel):
    receipt: ExportReceipt


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Immutable registry metadata; it does not contain executable behavior."""

    tool_id: str
    name: str
    purpose: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    access_mode: AccessMode
    side_effect_profile: SideEffectProfile
    default_trust: TrustLevel | None
    network_access: NetworkAccessProfile
    review_required_output: bool = False

    @property
    def result_trust(self) -> TrustLevel | Literal["inherited"]:
        return self.default_trust if self.default_trust is not None else "inherited"


class NetworkAccessProfile(StrEnum):
    DENIED = "denied"
    CONDITIONAL_APPROVAL = "conditional_approval"


def _tool(
    tool_id: str,
    name: str,
    purpose: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    *,
    access: AccessMode = AccessMode.READ_ONLY,
    side_effect: SideEffectProfile = SideEffectProfile.NONE,
    trust: TrustLevel | None = TrustLevel.VERIFIED,
    network: NetworkAccessProfile = NetworkAccessProfile.DENIED,
    review: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        name=name,
        purpose=purpose,
        version="1.0.0",
        input_model=input_model,
        output_model=output_model,
        access_mode=access,
        side_effect_profile=side_effect,
        default_trust=trust,
        network_access=network,
        review_required_output=review,
    )


_TOOLS = (
    _tool(
        "TOOL-001",
        "Register Approved Workspace",
        "Register an approved local workspace root.",
        RegisterWorkspaceInput,
        WorkspaceDescriptor,
        access=AccessMode.POLICY_WRITE,
        side_effect=SideEffectProfile.WORKSPACE_POLICY,
    ),
    _tool(
        "TOOL-002",
        "Discover Workspace Files",
        "Recursively discover files under approved include roots.",
        DiscoverFilesInput,
        DiscoveryOutput,
    ),
    _tool(
        "TOOL-003",
        "Read Bounded File Sample",
        "Read a bounded prefix, text sample, or byte range.",
        ReadFileSampleInput,
        FileSampleOutput,
    ),
    _tool(
        "TOOL-004",
        "Detect File Format",
        "Detect candidate formats from extension, magic bytes, and bounded structural checks.",
        DetectFormatInput,
        DetectFormatOutput,
        trust=TrustLevel.DERIVED,
    ),
    _tool(
        "TOOL-005",
        "Classify Oil and Gas Data",
        (
            "Classify domain category, subtype, stack, domain, processing, survey, well, "
            "and OSDU kind."
        ),
        ClassifyDataInput,
        ClassifyDataOutput,
        trust=TrustLevel.DERIVED,
    ),
    _tool(
        "TOOL-006",
        "Extract SEG-Y Metadata",
        "Extract SEG-Y headers, endian evidence, dimensions, and sampled trace structure.",
        ExtractSegyInput,
        SegyMetadata,
    ),
    _tool(
        "TOOL-007",
        "Extract LAS Metadata",
        "Extract LAS sections, curves, well metadata, and bounded statistics.",
        ExtractLasInput,
        LasMetadata,
    ),
    _tool(
        "TOOL-008",
        "Extract JSON Well Log Metadata",
        "Stream and validate JSON Well Log metadata, curves, and data shape.",
        ExtractJsonWellLogInput,
        JsonWellLogMetadata,
    ),
    _tool(
        "TOOL-009",
        "Extract DLIS Metadata",
        "Extract DLIS logical files, frames, channels, origins, and well metadata.",
        ExtractDlisInput,
        DlisMetadata,
    ),
    _tool(
        "TOOL-010",
        "Extract LIS/LTI Metadata",
        "Extract LIS/LTI record and well-log metadata.",
        ExtractLisInput,
        LisMetadata,
    ),
    _tool(
        "TOOL-011",
        "Extract CSV Metadata",
        "Extract CSV delimiter, headers, bounded row samples, and shape.",
        ExtractCsvInput,
        CsvMetadata,
    ),
    _tool(
        "TOOL-012",
        "Extract P1/90 Navigation Metadata",
        "Parse P1/90 headers, navigation positions, line summaries, and coordinate evidence.",
        ExtractP190Input,
        P190Metadata,
    ),
    _tool(
        "TOOL-013",
        "Extract Interpretation and Document Metadata",
        "Parse SGP, DAT, text, and PDF metadata through explicit subtype contracts.",
        ExtractInterpretationInput,
        InterpretationMetadataOutput,
    ),
    _tool(
        "TOOL-014",
        "Read and Parse Manifests",
        "Discover and parse JSON manifests within the approved manifest root.",
        ParseManifestsInput,
        ParseManifestsOutput,
    ),
    _tool(
        "TOOL-015",
        "Extract Manifest Records and Dataset References",
        "Extract manifest records, kinds, surrogate IDs, dataset paths, and relationships.",
        ExtractManifestRecordsInput,
        ExtractManifestRecordsOutput,
    ),
    _tool(
        "TOOL-016",
        "Match Manifest",
        "Score and rank data-to-manifest associations using explicit precedence rules.",
        MatchManifestInput,
        MatchManifestOutput,
        trust=TrustLevel.HEURISTIC,
        review=True,
    ),
    _tool(
        "TOOL-017",
        "Learn Manifest Patterns",
        "Learn category-scoped patterns from approved non-generated manifest pairs.",
        LearnManifestPatternsInput,
        LearnManifestPatternsOutput,
        trust=TrustLevel.DERIVED,
    ),
    _tool(
        "TOOL-018",
        "Generate One Manifest",
        "Generate one review-required manifest candidate using a selected learning model.",
        GenerateManifestInput,
        GenerateManifestOutput,
        access=AccessMode.MIXED,
        side_effect=SideEffectProfile.GENERATED_FILES,
        trust=TrustLevel.HEURISTIC,
        review=True,
    ),
    _tool(
        "TOOL-019",
        "Generate All Missing Manifests",
        "Generate candidates for all eligible unmatched inventory records.",
        GenerateAllManifestsInput,
        GenerationBatchResult,
        access=AccessMode.MIXED,
        side_effect=SideEffectProfile.GENERATED_FILES,
        trust=TrustLevel.HEURISTIC,
        review=True,
    ),
    _tool(
        "TOOL-020",
        "Validate OSDU Schemas",
        "Validate a manifest document and records against exact pinned OSDU schemas.",
        ValidateSchemasInput,
        ValidationReport,
        trust=TrustLevel.AUTHORITATIVE,
    ),
    _tool(
        "TOOL-021",
        "Refresh or Import Schema Catalog",
        "Refresh or import a pinned schema catalog under explicit network or local policy.",
        RefreshSchemaCatalogInput,
        RefreshSchemaCatalogOutput,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.VERSIONED_CACHE,
        trust=TrustLevel.AUTHORITATIVE,
        network=NetworkAccessProfile.CONDITIONAL_APPROVAL,
    ),
    _tool(
        "TOOL-022",
        "Persist Inventory",
        "Create or update versioned inventory and file records transactionally.",
        PersistInventoryInput,
        InventorySnapshotRef,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.INVENTORY_STATE,
    ),
    _tool(
        "TOOL-023",
        "Persist Manifest Associations",
        "Persist manifest associations and human review decisions.",
        PersistAssociationsInput,
        AssociationSnapshotRef,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.ASSOCIATION_STATE,
    ),
    _tool(
        "TOOL-024",
        "Persist Learning Models",
        (
            "Persist, version, activate, deactivate, or clear learning models without "
            "deleting audit history."
        ),
        PersistLearningModelInput,
        LearningModelVersionOutput,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.LEARNING_STATE,
        trust=TrustLevel.DERIVED,
    ),
    _tool(
        "TOOL-025",
        "Track Job Execution",
        "Create and execute a persisted job with typed steps and bounded concurrency.",
        TrackJobInput,
        TrackJobOutput,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.JOB_STATE,
    ),
    _tool(
        "TOOL-026",
        "Query or Cancel Job",
        "Request cancellation and query persisted job snapshots and ordered events.",
        QueryOrCancelJobInput,
        JobControlOutput,
        access=AccessMode.MIXED,
        side_effect=SideEffectProfile.JOB_STATE,
    ),
    _tool(
        "TOOL-027",
        "Build Inventory Review View",
        "Build a redacted inventory review projection with filtering, sorting, and summaries.",
        BuildInventoryReviewInput,
        InventoryReviewView,
        trust=None,
    ),
    _tool(
        "TOOL-028",
        "Build Manifest Review View",
        (
            "Build a manifest detail view with validation, provenance, association, and "
            "generation diff."
        ),
        BuildManifestReviewInput,
        ManifestReviewView,
        trust=None,
    ),
    _tool(
        "TOOL-029",
        "Record Review Decision",
        "Record an approve, reject, or needs-changes decision for a versioned review target.",
        RecordReviewDecisionInput,
        RecordReviewDecisionOutput,
        access=AccessMode.TRANSACTIONAL_WRITE,
        side_effect=SideEffectProfile.REVIEW_STATE,
    ),
    _tool(
        "TOOL-030",
        "Export Approved Results",
        "Export machine-readable reports and approved candidate manifests.",
        ExportToolInput,
        ExportToolOutput,
        access=AccessMode.ATOMIC_FILE_WRITE,
        side_effect=SideEffectProfile.EXPORT_FILES,
        trust=None,
    ),
)

TOOL_REGISTRY: Mapping[str, ToolDefinition] = MappingProxyType(
    {definition.tool_id: definition for definition in _TOOLS}
)


def validate_tool_registry(registry: Mapping[str, ToolDefinition]) -> None:
    """Fail closed when catalog metadata or generic schemas are incomplete."""

    expected_ids = [f"TOOL-{number:03d}" for number in range(1, 31)]
    if list(registry) != expected_ids:
        raise ValueError("tool registry must contain ordered TOOL-001 through TOOL-030")
    input_models: set[type[BaseModel]] = set()
    output_models: set[type[BaseModel]] = set()
    for key, definition in registry.items():
        if definition.tool_id != key:
            raise ValueError("tool registry key does not match definition")
        if not re.fullmatch(
            r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)",
            definition.version,
        ):
            raise ValueError(f"{key} has an invalid semantic version")
        if not definition.name or not definition.purpose:
            raise ValueError(f"{key} is missing name or purpose")
        if not issubclass(definition.input_model, BaseModel) or not issubclass(
            definition.output_model, BaseModel
        ):
            raise ValueError(f"{key} schemas must be Pydantic models")
        if not definition.input_model.model_fields or not definition.output_model.model_fields:
            raise ValueError(f"{key} schemas must be concrete object models")
        definition.input_model.model_json_schema()
        definition.output_model.model_json_schema()
        ToolRequest[definition.input_model].model_json_schema()  # type: ignore[name-defined]
        ToolResult[definition.output_model].model_json_schema()  # type: ignore[name-defined]
        input_models.add(definition.input_model)
        output_models.add(definition.output_model)
    if len(input_models) != 30 or len(output_models) != 30:
        raise ValueError("every tool must own unique concrete input and output models")


validate_tool_registry(TOOL_REGISTRY)

__all__ = [
    "TOOL_REGISTRY",
    "RegisterWorkspaceInput",
    "ToolDefinition",
    "ToolRequest",
    "ToolResult",
    "ToolResultStatus",
    "validate_tool_registry",
]
