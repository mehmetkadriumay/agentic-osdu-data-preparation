from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agentic_osdu.agents.orchestrator import ToolRegistryAdapter
from agentic_osdu.api import runtime
from agentic_osdu.api.app import create_app
from agentic_osdu.domain.models import (
    ActorRef,
    FileAssetRef,
    ManifestDocumentRef,
    ManifestJsonDocument,
    OSDUKind,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.generate import GenerationError
from agentic_osdu.manifests.match import MATCHING_POLICY_V1
from agentic_osdu.runtime import create_runtime
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import (
    AuditEventEntity,
    Base,
    FileAssetEntity,
    ManifestDocumentEntity,
)
from agentic_osdu.state.repositories import StateRepository
from agentic_osdu.tools.contracts import (
    BuildInventoryReviewInput,
    DiscoverFilesInput,
    ExportKind,
    ExportRequest,
    ExportToolInput,
    FileRecordContract,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    InventoryMutation,
    JobControlAction,
    JobDefinition,
    JobStepDefinition,
    ManifestIndexContract,
    MatchManifestInput,
    ParseManifestsInput,
    PersistAssociationsInput,
    PersistInventoryInput,
    QueryOrCancelJobInput,
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
    TrackJobInput,
    ValidateSchemasInput,
)


class _RejectingInvoker:
    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        raise AssertionError(f"unexpected invocation: {tool_id} {request}")


def test_shipped_runtime_registers_every_api_and_cli_tool() -> None:
    assert runtime.app.state.tool_invoker.__class__.__name__ == "ToolRegistryAdapter"
    assert {
        "TOOL-001",
        "TOOL-002",
        "TOOL-003",
        "TOOL-004",
        "TOOL-005",
        "TOOL-006",
        "TOOL-007",
        "TOOL-008",
        "TOOL-009",
        "TOOL-010",
        "TOOL-011",
        "TOOL-012",
        "TOOL-013",
        "TOOL-014",
        "TOOL-016",
        "TOOL-017",
        "TOOL-018",
        "TOOL-019",
        "TOOL-020",
        "TOOL-022",
        "TOOL-023",
        "TOOL-024",
        "TOOL-025",
        "TOOL-026",
        "TOOL-027",
        "TOOL-028",
        "TOOL-029",
        "TOOL-030",
    } <= runtime.app.state.tool_invoker.executable_tool_ids


class _RecordingInvoker:
    def __init__(self, delegate: ToolRegistryAdapter) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        self.calls.append(tool_id)
        return self.delegate.invoke(tool_id, request)


def test_runtime_executes_registered_wf001_job_and_projects_state(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "sample.txt").write_text("sample", encoding="utf-8")
    (workspace_root / "exports").mkdir()
    state_path = tmp_path / "runtime.db"
    composition = create_runtime(state_path)
    actor = ActorRef(actor_id="local-user")
    registration = ToolRequest[RegisterWorkspaceInput](
        request_id=uuid4(),
        workspace_id=uuid4(),
        actor=actor,
        input=RegisterWorkspaceInput(
            root_path=str(workspace_root),
            allowed_output_subpaths=(WorkspaceRelativePath("exports"),),
        ),
    )
    workspace = composition.registry.invoke("TOOL-001", registration).output
    assert workspace is not None
    recorder = _RecordingInvoker(composition.registry)
    composition.registry = cast(Any, recorder)
    inventory_id = uuid4()
    start = ToolRequest[TrackJobInput](
        request_id=uuid4(),
        workspace_id=workspace.workspace_id,
        actor=actor,
        input=TrackJobInput(
            workflow_id="WF-001",
            inventory_id=inventory_id,
            discovery=DiscoverFilesInput(
                workspace_id=workspace.workspace_id,
                include_paths=(),
                max_files=10,
            ),
            definition=JobDefinition(
                job_type="WF-001",
                steps=(
                    JobStepDefinition(sequence=1, tool_id="TOOL-002", input_ref="workspace"),
                    JobStepDefinition(sequence=2, tool_id="TOOL-003", input_ref="files"),
                    JobStepDefinition(sequence=3, tool_id="TOOL-004", input_ref="samples"),
                    JobStepDefinition(sequence=4, tool_id="TOOL-005", input_ref="detections"),
                    JobStepDefinition(sequence=5, tool_id="TOOL-022", input_ref="classifications"),
                ),
            ),
            deduplication_key=f"wf-001:{uuid4()}",
        ),
    )
    started = composition.registry.invoke("TOOL-025", start).output
    assert started is not None
    job_id = started.descriptor.job.job_id
    for _ in range(100):
        queried = composition.registry.invoke(
            "TOOL-026",
            ToolRequest[QueryOrCancelJobInput](
                request_id=uuid4(),
                workspace_id=workspace.workspace_id,
                actor=actor,
                input=QueryOrCancelJobInput(action=JobControlAction.QUERY, job_id=job_id),
            ),
        ).output
        assert queried is not None
        if queried.snapshot.job.status.value in {"succeeded", "failed"}:
            break
        sleep(0.01)
    assert queried is not None
    assert queried.snapshot.job.status.value == "succeeded"
    projected = composition.registry.invoke(
        "TOOL-027",
        ToolRequest[BuildInventoryReviewInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=BuildInventoryReviewInput(inventory_id=inventory_id),
        ),
    ).output
    assert projected is not None
    assert projected.state_version == 1
    assert [item.file.relative_path.root for item in projected.items] == ["sample.txt"]
    assert projected.items[0].classification is not None
    assert projected.items[0].classification.extraction_ids
    assert projected.items[0].evidence
    assert projected.items[0].provenance
    assert {
        "TOOL-002",
        "TOOL-003",
        "TOOL-004",
        "TOOL-013",
        "TOOL-005",
        "TOOL-022",
    } <= set(recorder.calls)

    manifests = workspace_root / "Manifests"
    manifests.mkdir()
    (manifests / "example.json").write_text(
        '{"kind":"osdu:wks:Manifest:1.0.0","Data":{}}',
        encoding="utf-8",
    )
    parsed = composition.registry.invoke(
        "TOOL-014",
        ToolRequest[ParseManifestsInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=ParseManifestsInput(
                workspace_id=workspace.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
            ),
        ),
    ).output
    assert parsed is not None
    assert len(parsed.manifests) == 1
    validation = composition.registry.invoke(
        "TOOL-020",
        ToolRequest[ValidateSchemasInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=ValidateSchemasInput(
                manifest=parsed.manifests[0],
                schema_catalog_id=uuid4(),
            ),
        ),
    ).output
    assert validation is not None
    assert validation.status.value == "unavailable"

    associations = composition.registry.invoke(
        "TOOL-023",
        ToolRequest[PersistAssociationsInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=PersistAssociationsInput(mutations=(), expected_state_version=0),
        ),
    ).output
    assert associations is not None
    assert associations.state_version == 1

    generated = composition.registry.invoke(
        "TOOL-019",
        ToolRequest[GenerateAllManifestsInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=GenerateAllManifestsInput(
                inventory_id=inventory_id,
                generation_policy_version="1.0.0",
                dry_run=True,
            ),
        ),
    ).output
    assert generated is not None
    assert generated.generated == 0
    assert generated.skipped == 0

    matched = composition.registry.invoke(
        "TOOL-016",
        ToolRequest[MatchManifestInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=MatchManifestInput(
                file_record=FileRecordContract(
                    file=projected.items[0].file,
                    classification=projected.items[0].classification,
                ),
                manifest_index=ManifestIndexContract(
                    manifest_index_id=uuid4(),
                    manifest_ids=(),
                    records=(),
                    dataset_references=(),
                    component_relationships=(),
                    index_sha256="b" * 64,
                    built_at=datetime.now(UTC),
                ),
                matching_policy=MATCHING_POLICY_V1,
            ),
        ),
    ).output
    assert matched is not None
    assert matched.matches == ()

    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        composition.registry.invoke(
            "TOOL-018",
            ToolRequest[GenerateManifestInput](
                request_id=uuid4(),
                workspace_id=workspace.workspace_id,
                actor=actor,
                input=GenerateManifestInput(
                    file_id=projected.items[0].file.file_id,
                    learning_model_id=uuid4(),
                    generation_policy_version="1.0.0",
                ),
            ),
        )

    exported = composition.registry.invoke(
        "TOOL-030",
        ToolRequest[ExportToolInput](
            request_id=uuid4(),
            workspace_id=workspace.workspace_id,
            actor=actor,
            input=ExportToolInput(
                request=ExportRequest(
                    export_kind=ExportKind.REPORT,
                    target_id=inventory_id,
                    output_root_id="exports",
                    relative_path=WorkspaceRelativePath("inventory.json"),
                    expected_target_version="1",
                )
            ),
        ),
    ).output
    assert exported is not None
    assert (workspace_root / "exports" / "inventory.json").is_file()
    source_file_id = projected.items[0].file.file_id
    manifest_id = parsed.manifests[0].document.manifest_id
    composition.database.dispose()
    restarted = create_runtime(state_path)
    assert restarted._file_records[source_file_id].metadata_extractions
    assert manifest_id in restarted._parsed_manifests
    restarted.database.dispose()


def test_manifest_document_kind_round_trips_through_repository(tmp_path: Path) -> None:
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(database.engine)
    repository = StateRepository(database.session_factory)
    kind = OSDUKind("osdu:wks:Manifest:1.0.0")
    manifest = ManifestDocumentRef(
        manifest_id=uuid4(),
        path=WorkspaceRelativePath("Manifests/example.json"),
        sha256="a" * 64,
        document_kind=kind,
    )
    repository.persist_manifest(
        manifest,
        ManifestJsonDocument(
            sha256=manifest.sha256,
            content={"kind": kind.root, "Data": {}},
        ),
    )

    assert repository.get_manifest_document(manifest.manifest_id) == manifest
    with database.session_factory.begin() as session:
        row = session.get(ManifestDocumentEntity, str(manifest.manifest_id))
        assert row is not None
        row.document_kind = str(kind)
    assert repository.get_manifest_document(manifest.manifest_id) == manifest
    database.dispose()


def test_fastapi_404_and_405_are_stable_tool_error_envelopes() -> None:
    client = TestClient(create_app(_RejectingInvoker()))
    missing = client.get("/api/v1/not-a-route")
    wrong_method = client.get("/api/v1/workspaces")

    assert missing.status_code == 404
    assert missing.json()["errors"][0] == {
        "code": "API_ROUTE_NOT_FOUND",
        "category": "not_found",
        "message": "The requested API route was not found.",
        "retryable": False,
        "path": None,
        "details": {},
    }
    assert wrong_method.status_code == 405
    assert wrong_method.json()["errors"][0]["code"] == "API_METHOD_NOT_ALLOWED"
    assert wrong_method.json()["errors"][0]["category"] == "validation"


def test_inventory_reset_archives_snapshot_and_uses_current_version(tmp_path: Path) -> None:
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    repository = StateRepository(database.session_factory)
    inventory_id = uuid4()
    file_id = uuid4()
    actor = ActorRef(actor_id="local-user")
    repository.persist_inventory(
        request_id=uuid4(),
        actor=actor,
        request=PersistInventoryInput(
            mutation=InventoryMutation(
                inventory_id=inventory_id,
                files=(
                    FileAssetRef(
                        file_id=file_id,
                        workspace_id=inventory_id,
                        relative_path=WorkspaceRelativePath("data/example.las"),
                        size_bytes=12,
                        modified_at=datetime(2026, 9, 9, tzinfo=UTC),
                        sha256="0" * 64,
                        discovery_version=1,
                    ),
                ),
            ),
            expected_state_version=0,
        ),
    )

    receipt = repository.archive_and_reset_inventory(
        request_id=uuid4(),
        actor=actor,
        inventory_id=inventory_id,
        expected_state_version=repository.get_inventory_version(inventory_id),
    )

    assert receipt.prior_state_version == 1
    assert receipt.state_version == 2
    assert receipt.archived_file_count == 1
    with database.session_factory() as session:
        assert session.scalars(select(FileAssetEntity)).all() == []
        audit = session.scalar(
            select(AuditEventEntity).where(AuditEventEntity.request_id == str(receipt.archive_id))
        )
        assert audit is not None
        assert audit.payload["snapshot"]["files"][0]["relative_path"] == "data/example.las"
    database.dispose()


def test_projection_loads_inventory_version_and_active_learning_metadata() -> None:
    source = Path("src/agentic_osdu/tools/review.py").read_text(encoding="utf-8")
    assert "state_version=" in source
    assert "active_learning_model=" in source


def test_manifest_review_accepts_persisted_target_identifier() -> None:
    source = Path("src/agentic_osdu/tools/contracts.py").read_text(encoding="utf-8")
    assert "manifest_id: UUID" in source[source.index("class BuildManifestReviewInput") :]
