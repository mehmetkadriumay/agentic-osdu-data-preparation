"""Complete SQLAlchemy 2 entity model for PRD section 3.10."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkspaceEntity(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_root: Mapped[str] = mapped_column(Text)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_output_subpaths: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_actor: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InventoryEntity(Base):
    __tablename__ = "inventory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FileAssetEntity(Base):
    __tablename__ = "file_asset"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    inventory_id: Mapped[str] = mapped_column(ForeignKey("inventory.id", ondelete="CASCADE"))
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str | None] = mapped_column(String(64))
    discovery_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("inventory_id", "relative_path"),)


class FileSampleEntity(Base):
    __tablename__ = "file_sample"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_asset.id", ondelete="CASCADE"))
    offset: Mapped[int] = mapped_column(Integer)
    length: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    encoding: Mapped[str | None] = mapped_column(String(64))
    bounded_reference: Mapped[str | None] = mapped_column(Text)


class FormatDetectionEntity(Base):
    __tablename__ = "format_detection"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_asset.id", ondelete="CASCADE"))
    detector_version: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MetadataExtractionEntity(Base):
    __tablename__ = "metadata_extraction"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_asset.id", ondelete="CASCADE"))
    file_sha256: Mapped[str] = mapped_column(String(64))
    format_id: Mapped[str] = mapped_column(String(32))
    parser_version: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provenance: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClassificationEntity(Base):
    __tablename__ = "classification"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_asset.id", ondelete="CASCADE"))
    detection_id: Mapped[str] = mapped_column(String(36))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ManifestDocumentEntity(Base):
    __tablename__ = "manifest_document"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    document_kind: Mapped[str | None] = mapped_column(String(512))
    normalized_content_ref: Mapped[str | None] = mapped_column(Text)
    normalized_content: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provenance: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    generated: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("path", "sha256"),)


class ManifestRecordEntity(Base):
    __tablename__ = "manifest_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("manifest_document.id", ondelete="CASCADE"))
    record_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(512))
    json_pointer: Mapped[str] = mapped_column(Text)
    surrogate_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("manifest_id", "record_id", "json_pointer"),)


class DatasetReferenceEntity(Base):
    __tablename__ = "dataset_reference"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("manifest_document.id", ondelete="CASCADE"))
    record_id: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)
    json_pointer: Mapped[str] = mapped_column(Text)
    relative_path: Mapped[str | None] = mapped_column(Text)


class ManifestAssociationEntity(Base):
    __tablename__ = "manifest_association"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_asset.id", ondelete="CASCADE"))
    manifest_id: Mapped[str] = mapped_column(String(36), index=True)
    score: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(128))
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(32))
    trust_level: Mapped[str] = mapped_column(String(32))
    target_version: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class LearningExampleEntity(Base):
    __tablename__ = "learning_example"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_file_id: Mapped[str] = mapped_column(String(36))
    manifest_id: Mapped[str] = mapped_column(String(36))
    association_id: Mapped[str] = mapped_column(String(36))
    source_sha256: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    review_status: Mapped[str] = mapped_column(String(32))
    generated_manifest: Mapped[bool] = mapped_column(Boolean, default=False)


class LearningModelVersionEntity(Base):
    __tablename__ = "learning_model_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    learning_model_id: Mapped[str] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    model_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("learning_model_id", "version"),)


class GeneratedCandidateEntity(Base):
    __tablename__ = "generated_candidate"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_file_id: Mapped[str] = mapped_column(String(36), index=True)
    learning_model_id: Mapped[str] = mapped_column(String(36))
    candidate_sha256: Mapped[str] = mapped_column(String(64))
    proposed_path: Mapped[str] = mapped_column(Text)
    generation_status: Mapped[str] = mapped_column(String(32))
    validation_status: Mapped[str] = mapped_column(String(32))
    review_status: Mapped[str] = mapped_column(String(32))
    trust_level: Mapped[str] = mapped_column(String(32))
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provenance: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_content: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SchemaCatalogEntity(Base):
    __tablename__ = "schema_catalog"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_policy_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(Text)
    checksums: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    catalog_sha256: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEntity(Base):
    __tablename__ = "job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(128))
    deduplication_key: Mapped[str] = mapped_column(String(256), unique=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str | None] = mapped_column(String(128))
    counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobEventEntity(Base):
    __tablename__ = "job_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    current_item: Mapped[str | None] = mapped_column(String(256))
    safe_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("job_id", "sequence"),)


class ReviewDecisionEntity(Base):
    __tablename__ = "review_decision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    actor: Mapped[dict[str, Any]] = mapped_column(JSON)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    target_version: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(Integer)


class AuditEventEntity(Base):
    __tablename__ = "audit_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    actor: Mapped[dict[str, Any]] = mapped_column(JSON)
    tool_id: Mapped[str] = mapped_column(String(16))
    tool_version: Mapped[str] = mapped_column(String(128))
    result_status: Mapped[str] = mapped_column(String(32))
    side_effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyRecordEntity(Base):
    __tablename__ = "idempotency_record"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StateCounterEntity(Base):
    __tablename__ = "state_counter"
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


ALL_ENTITY_TABLE_NAMES = frozenset(
    {
        "workspace",
        "file_asset",
        "file_sample",
        "format_detection",
        "metadata_extraction",
        "classification",
        "manifest_document",
        "manifest_record",
        "dataset_reference",
        "manifest_association",
        "learning_example",
        "learning_model_version",
        "generated_candidate",
        "schema_catalog",
        "job",
        "job_event",
        "review_decision",
        "audit_event",
    }
)

__all__ = [
    "ALL_ENTITY_TABLE_NAMES",
    "AuditEventEntity",
    "Base",
    "JobEntity",
    "ReviewDecisionEntity",
]
