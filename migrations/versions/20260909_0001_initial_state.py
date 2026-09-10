"""Create the complete EPIC-007 state model.

Revision ID: 20260909_0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("canonical_root", sa.Text(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("allowed_output_subpaths", sa.JSON(), nullable=False),
        sa.Column("policy_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_actor", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "inventory",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "manifest_document",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("document_kind", sa.String(length=512), nullable=True),
        sa.Column("normalized_content_ref", sa.Text(), nullable=True),
        sa.Column("normalized_content", sa.JSON(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("generated", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path", "sha256"),
    )
    op.create_table(
        "learning_example",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("manifest_id", sa.String(length=36), nullable=False),
        sa.Column("association_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("generated_manifest", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "learning_model_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("learning_model_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learning_model_id", "version"),
    )
    op.create_index(
        op.f("ix_learning_model_version_category"),
        "learning_model_version",
        ["category"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_model_version_learning_model_id"),
        "learning_model_version",
        ["learning_model_id"],
        unique=False,
    )
    op.create_table(
        "generated_candidate",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("learning_model_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposed_path", sa.Text(), nullable=False),
        sa.Column("generation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("trust_level", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("source_content", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_generated_candidate_source_file_id"),
        "generated_candidate",
        ["source_file_id"],
        unique=False,
    )
    op.create_table(
        "schema_catalog",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_policy_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.String(length=256), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("checksums", sa.JSON(), nullable=False),
        sa.Column("catalog_sha256", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_schema_catalog_workspace_policy_id"),
        "schema_catalog",
        ["workspace_policy_id"],
        unique=False,
    )
    op.create_table(
        "job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("deduplication_key", sa.String(length=256), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=128), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(op.f("ix_job_status"), "job", ["status"], unique=False)
    op.create_table(
        "review_decision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("target_version", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        op.f("ix_review_decision_target_id"),
        "review_decision",
        ["target_id"],
        unique=False,
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("tool_id", sa.String(length=16), nullable=False),
        sa.Column("tool_version", sa.String(length=128), nullable=False),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.Column("side_effects", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_audit_event_request_id"),
        "audit_event",
        ["request_id"],
        unique=True,
    )
    op.create_table(
        "idempotency_record",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=16), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "state_counter",
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("namespace"),
    )
    op.create_table(
        "file_asset",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("inventory_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("discovery_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["inventory_id"], ["inventory.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inventory_id", "relative_path"),
    )
    op.create_index(
        op.f("ix_file_asset_workspace_id"),
        "file_asset",
        ["workspace_id"],
        unique=False,
    )
    op.create_table(
        "manifest_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manifest_id", sa.String(length=36), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=512), nullable=False),
        sa.Column("json_pointer", sa.Text(), nullable=False),
        sa.Column("surrogate_ids", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["manifest_id"], ["manifest_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_id", "record_id", "json_pointer"),
    )
    op.create_table(
        "dataset_reference",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manifest_id", sa.String(length=36), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("json_pointer", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["manifest_id"], ["manifest_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "job_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("current_item", sa.String(length=256), nullable=True),
        sa.Column("safe_payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "sequence"),
    )
    op.create_table(
        "file_sample",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("encoding", sa.String(length=64), nullable=True),
        sa.Column("bounded_reference", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["file_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "format_detection",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("detector_version", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["file_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "metadata_extraction",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("format_id", sa.String(length=32), nullable=False),
        sa.Column("parser_version", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["file_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "classification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("detection_id", sa.String(length=36), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["file_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "manifest_association",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("manifest_id", sa.String(length=36), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("method", sa.String(length=128), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("trust_level", sa.String(length=32), nullable=False),
        sa.Column("target_version", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["file_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_manifest_association_manifest_id"),
        "manifest_association",
        ["manifest_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_manifest_association_manifest_id"), table_name="manifest_association")
    op.drop_table("manifest_association")
    op.drop_table("classification")
    op.drop_table("metadata_extraction")
    op.drop_table("format_detection")
    op.drop_table("file_sample")
    op.drop_table("job_event")
    op.drop_table("dataset_reference")
    op.drop_table("manifest_record")
    op.drop_index(op.f("ix_file_asset_workspace_id"), table_name="file_asset")
    op.drop_table("file_asset")
    op.drop_table("state_counter")
    op.drop_table("idempotency_record")
    op.drop_index(op.f("ix_audit_event_request_id"), table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index(op.f("ix_review_decision_target_id"), table_name="review_decision")
    op.drop_table("review_decision")
    op.drop_index(op.f("ix_job_status"), table_name="job")
    op.drop_table("job")
    op.drop_index(
        op.f("ix_schema_catalog_workspace_policy_id"),
        table_name="schema_catalog",
    )
    op.drop_table("schema_catalog")
    op.drop_index(
        op.f("ix_generated_candidate_source_file_id"),
        table_name="generated_candidate",
    )
    op.drop_table("generated_candidate")
    op.drop_index(
        op.f("ix_learning_model_version_learning_model_id"),
        table_name="learning_model_version",
    )
    op.drop_index(
        op.f("ix_learning_model_version_category"),
        table_name="learning_model_version",
    )
    op.drop_table("learning_model_version")
    op.drop_table("learning_example")
    op.drop_table("manifest_document")
    op.drop_table("inventory")
    op.drop_table("workspace")
