from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentic_osdu.agents.orchestrator import OrchestrationError, ToolRegistryAdapter
from agentic_osdu.api.app import create_app
from agentic_osdu.domain.models import (
    ActorRef,
    DataCategory,
    FileAssetRef,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    GenerationStatus,
    ManifestJsonDocument,
    ReviewStatus,
    TrustLevel,
    ValidationStatus,
    WorkspaceRelativePath,
)
from agentic_osdu.formats import FormatExtractionError
from agentic_osdu.jobs.service import JobError
from agentic_osdu.manifests.generate import GenerationError
from agentic_osdu.manifests.learn import LearningError
from agentic_osdu.manifests.match import MatchingError
from agentic_osdu.manifests.parse import ManifestError
from agentic_osdu.runtime import RuntimeComposition, create_runtime
from agentic_osdu.schemas.catalog import SchemaCatalogError
from agentic_osdu.state.repositories import StateConflictError
from agentic_osdu.tools.contracts import (
    BuildInventoryReviewInput,
    BuildManifestReviewInput,
    ClassifyDataInput,
    DetectFormatInput,
    DiscoverFilesInput,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    InventoryMutation,
    JobControlAction,
    JobDefinition,
    JobStepDefinition,
    LearningModelContract,
    LearningModelMutation,
    LearningMutationAction,
    ParseManifestsInput,
    PersistInventoryInput,
    PersistLearningModelInput,
    QueryOrCancelJobInput,
    ReadFileSampleInput,
    RegisterWorkspaceInput,
    SampleMode,
    ToolRequest,
    TrackJobInput,
    ValidateSchemasInput,
)
from agentic_osdu.tools.detection import DetectionError
from agentic_osdu.tools.discovery import DiscoveryError
from agentic_osdu.tools.review import ReviewError

ACTOR = ActorRef(actor_id="local-user")


def _request(workspace_id: Any, value: Any) -> ToolRequest[Any]:
    return ToolRequest[Any](request_id=uuid4(), workspace_id=workspace_id, actor=ACTOR, input=value)


def _model() -> LearningModelContract:
    content: dict[str, Any] = {"kind": "osdu:wks:Manifest:1.0.0", "Data": {}}
    digest = (
        __import__("hashlib")
        .sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    return LearningModelContract(
        learning_model_id=uuid4(),
        category=DataCategory.SEISMIC,
        version=1,
        model_sha256="d" * 64,
        example_ids=(),
        example_identities=(),
        prototype=ManifestJsonDocument(sha256=digest, content=content),
        constants=(),
        prototype_source_path=WorkspaceRelativePath("Data/source.sgy"),
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )


class _RecordingInvoker:
    def __init__(self, delegate: ToolRegistryAdapter) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> Any:
        self.calls.append(tool_id)
        return self.delegate.invoke(tool_id, request)


def _wait_for_terminal(composition: RuntimeComposition, workspace_id: Any, job_id: Any) -> Any:
    for _ in range(200):
        result = composition.registry.invoke(
            "TOOL-026",
            _request(
                workspace_id,
                QueryOrCancelJobInput(action=JobControlAction.QUERY, job_id=job_id),
            ),
        )
        assert result.output is not None
        if result.output.snapshot.job.status.value in {
            "succeeded",
            "partially_succeeded",
            "failed",
            "cancelled",
        }:
            return result.output
        sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def test_generate_all_is_a_complete_persisted_wf005_job(tmp_path: Path) -> None:
    composition = create_runtime(tmp_path / "state.db")
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = composition.registry.invoke(
        "TOOL-001", _request(uuid4(), RegisterWorkspaceInput(root_path=str(root)))
    ).output
    assert workspace is not None
    workspace_id = workspace.workspace_id
    recorder = _RecordingInvoker(composition.registry)
    composition.registry = cast(Any, recorder)
    generation = GenerateAllManifestsInput(
        inventory_id=uuid4(),
        schema_catalog_id=uuid4(),
        generation_policy_version="1.0.0",
        dry_run=True,
    )
    start = TrackJobInput(
        workflow_id="WF-005",
        generation=generation,
        definition=JobDefinition(
            job_type="WF-005",
            steps=(
                JobStepDefinition(sequence=1, tool_id="TOOL-019", input_ref="batch"),
                JobStepDefinition(sequence=2, tool_id="TOOL-020", input_ref="candidates"),
                JobStepDefinition(sequence=3, tool_id="TOOL-022", input_ref="validated-candidates"),
                JobStepDefinition(sequence=4, tool_id="TOOL-023", input_ref="associations"),
                JobStepDefinition(sequence=5, tool_id="TOOL-027", input_ref="review"),
            ),
        ),
        deduplication_key=f"wf-005:{uuid4()}",
    )

    created = composition.registry.invoke("TOOL-025", _request(workspace_id, start)).output
    assert created is not None
    queried = _wait_for_terminal(composition, workspace_id, created.descriptor.job.job_id)

    assert queried.snapshot.job.status.value == "succeeded"
    assert {"TOOL-019", "TOOL-022", "TOOL-023", "TOOL-027"} <= set(recorder.calls)
    assert [event.event_type.value for event in queried.events][-1] == "job.succeeded"
    composition.database.dispose()


def test_generation_all_route_delegates_to_tool025(monkeypatch: pytest.MonkeyPatch) -> None:
    class Invoker:
        def __init__(self) -> None:
            self.tool_id: str | None = None
            self.request: Any = None

        def invoke(self, tool_id: str, request: Any) -> Any:
            self.tool_id = tool_id
            self.request = request
            raise StateConflictError("JOB_ALREADY_RUNNING", "Already running.", retryable=True)

    invoker = Invoker()
    monkeypatch.setattr(
        "agentic_osdu.api.routes._parse",
        lambda *_args: _request(
            uuid4(),
            GenerateAllManifestsInput(
                inventory_id=uuid4(),
                schema_catalog_id=uuid4(),
                generation_policy_version="1.0.0",
            ),
        ),
    )
    response = TestClient(create_app(invoker)).post("/api/v1/generation/all", json={})
    assert invoker.tool_id == "TOOL-025"
    assert [step.tool_id for step in invoker.request.input.definition.steps] == [
        "TOOL-019",
        "TOOL-020",
        "TOOL-022",
        "TOOL-023",
        "TOOL-027",
    ]
    assert response.status_code == 409
    assert response.json()["errors"][0] == {
        "code": "JOB_ALREADY_RUNNING",
        "category": "conflict",
        "message": "Already running.",
        "retryable": True,
        "path": None,
        "details": {},
    }


@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (StateConflictError("STATE_VERSION_CONFLICT", "Stale.", retryable=True), 409, "conflict"),
        (DiscoveryError("ROOT_NOT_FOUND", "Missing."), 404, "not_found"),
        (DetectionError("INSUFFICIENT_SAMPLE", "Missing."), 400, "validation"),
        (FormatExtractionError("LAS_ENCODING_FAILED", "Bad encoding."), 400, "parse"),
        (ManifestError("MANIFEST_JSON_INVALID", "Bad JSON."), 400, "parse"),
        (MatchingError("MATCH_INPUT_INCOMPLETE", "Missing."), 400, "validation"),
        (LearningError("NO_ELIGIBLE_EXAMPLES", "Missing."), 400, "validation"),
        (GenerationError("NO_COMPATIBLE_MODEL", "No model."), 409, "conflict"),
        (SchemaCatalogError("SCHEMA_UNAVAILABLE", "Missing."), 503, "schema_unavailable"),
        (JobError("JOB_NOT_CANCELLABLE", "Terminal."), 409, "conflict"),
        (ReviewError("MANIFEST_NOT_FOUND", "Missing."), 404, "not_found"),
        (OrchestrationError("ROOT_POLICY_DENIED", "Denied."), 403, "access_denied"),
    ],
)
def test_domain_exceptions_are_stable_tool_errors(
    error: Exception, status: int, category: str
) -> None:
    class Invoker:
        def invoke(self, _tool_id: str, _request: Any) -> Any:
            raise error

    response = TestClient(create_app(Invoker()), raise_server_exceptions=False).post(
        "/api/v1/generation/one",
        json={
            "request_id": str(uuid4()),
            "workspace_id": str(uuid4()),
            "actor": {"actor_id": "local-user"},
            "input": {
                "file_id": str(uuid4()),
                "learning_model_id": str(uuid4()),
                "generation_policy_version": "1.0.0",
            },
        },
    )
    assert response.status_code == status
    assert response.json()["errors"][0]["code"] == cast(Any, error).code
    assert response.json()["errors"][0]["category"] == category
    assert response.json()["errors"][0]["retryable"] is bool(getattr(error, "retryable", False))


def test_learning_cache_tracks_only_persisted_active_versions(tmp_path: Path) -> None:
    composition = create_runtime(tmp_path / "state.db")
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = composition.registry.invoke(
        "TOOL-001", _request(uuid4(), RegisterWorkspaceInput(root_path=str(root)))
    ).output
    assert workspace is not None
    workspace_id = workspace.workspace_id
    model = _model()
    created = composition.registry.invoke(
        "TOOL-024",
        _request(
            workspace_id,
            PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.CREATE,
                    model=model,
                    expected_version=0,
                )
            ),
        ),
    ).output
    assert created is not None
    assert model.learning_model_id not in composition._learning_models
    active = composition.registry.invoke(
        "TOOL-024",
        _request(
            workspace_id,
            PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.ACTIVATE,
                    learning_model_id=model.learning_model_id,
                    expected_version=created.version,
                )
            ),
        ),
    ).output
    assert active is not None
    assert model.learning_model_id in composition._learning_models
    composition.registry.invoke(
        "TOOL-024",
        _request(
            workspace_id,
            PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.DEACTIVATE,
                    learning_model_id=model.learning_model_id,
                    expected_version=active.version,
                )
            ),
        ),
    )
    assert composition._learning_models == {}
    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        composition.registry.invoke(
            "TOOL-018",
            _request(
                workspace_id,
                GenerateManifestInput(
                    file_id=uuid4(),
                    learning_model_id=model.learning_model_id,
                    generation_policy_version="1.0.0",
                ),
            ),
        )
    composition.database.dispose()


def test_archive_reset_evicts_runtime_inventory_and_file_caches(tmp_path: Path) -> None:
    composition = create_runtime(tmp_path / "state.db")
    inventory_id, workspace_id, file_id = uuid4(), uuid4(), uuid4()
    file = FileAssetRef(
        file_id=file_id,
        workspace_id=workspace_id,
        relative_path=WorkspaceRelativePath("data/example.las"),
        size_bytes=1,
        modified_at=datetime(2026, 9, 9, tzinfo=UTC),
        sha256="a" * 64,
        discovery_version=1,
    )
    composition.registry.invoke(
        "TOOL-022",
        _request(
            workspace_id,
            PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
                expected_state_version=0,
            ),
        ),
    )
    composition._matched_files.add(file_id)
    composition.registry.invoke(
        "TOOL-022",
        _request(
            workspace_id,
            PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id),
                expected_state_version=1,
                archive_before_reset=True,
            ),
        ),
    )
    assert composition._inventory_files[inventory_id] == set()
    assert file_id not in composition._files
    assert file_id not in composition._file_records
    assert file_id not in composition._matched_files
    composition.database.dispose()


def test_detection_evidence_is_returned_persisted_and_projected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sample.las").write_text("~Version\nVERS. 2.0\n~Curve\nDEPT.M\n", encoding="ascii")
    composition = create_runtime(tmp_path / "state.db")
    registered = composition.registry.invoke(
        "TOOL-001",
        _request(
            uuid4(),
            RegisterWorkspaceInput(root_path=str(root), allowed_output_subpaths=()),
        ),
    ).output
    assert registered is not None
    discovered = composition.registry.invoke(
        "TOOL-002",
        _request(
            registered.workspace_id,
            DiscoverFilesInput(workspace_id=registered.workspace_id, include_paths=()),
        ),
    ).output
    assert discovered is not None
    file = discovered.files[0]
    sample = composition.registry.invoke(
        "TOOL-003",
        _request(
            registered.workspace_id,
            ReadFileSampleInput(file_id=file.file_id, mode=SampleMode.PREFIX, max_bytes=4096),
        ),
    ).output
    assert sample is not None
    detection_result = composition.registry.invoke(
        "TOOL-004",
        _request(
            registered.workspace_id,
            DetectFormatInput(file_id=file.file_id, sample_refs=(sample.sample.sample_id,)),
        ),
    )
    assert detection_result.output is not None
    detection = detection_result.output.detection
    assert set(detection.candidates[0].evidence_ids) <= {
        evidence.evidence_id for evidence in detection_result.evidence
    }
    classification_result = composition.registry.invoke(
        "TOOL-005",
        _request(
            registered.workspace_id,
            ClassifyDataInput(file_id=file.file_id, detection=detection),
        ),
    )
    assert classification_result.output is not None
    inventory_id = uuid4()
    composition.registry.invoke(
        "TOOL-022",
        _request(
            registered.workspace_id,
            PersistInventoryInput(
                mutation=InventoryMutation(
                    inventory_id=inventory_id,
                    files=(file,),
                    detections=(detection,),
                    classifications=(classification_result.output.classification,),
                    evidence=detection_result.evidence + classification_result.evidence,
                    provenance=detection_result.provenance + classification_result.provenance,
                ),
                expected_state_version=0,
            ),
        ),
    )
    view = composition.registry.invoke(
        "TOOL-027",
        _request(
            registered.workspace_id,
            BuildInventoryReviewInput(inventory_id=inventory_id),
        ),
    ).output
    assert view is not None
    assert view.items[0].evidence
    assert any("LAS" in evidence.summary for evidence in view.items[0].evidence)
    assert view.items[0].provenance
    composition.database.dispose()


def test_wf001_cancellation_reaches_discovery_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    composition = create_runtime(tmp_path / "state.db")
    registered = composition.registry.invoke(
        "TOOL-001",
        _request(uuid4(), RegisterWorkspaceInput(root_path=str(root))),
    ).output
    assert registered is not None
    saw_cancellation_callback = False

    def slow_discovery(value: Any, *, cancellation: Any = None, on_progress: Any = None) -> Any:
        nonlocal saw_cancellation_callback
        del value, on_progress
        saw_cancellation_callback = cancellation is not None
        for _ in range(500):
            if cancellation is not None and cancellation.is_cancelled:
                from agentic_osdu.tools.discovery import DiscoveryError

                raise DiscoveryError("CANCELLED", "The operation was cancelled.")
            sleep(0.002)
        raise AssertionError("discovery did not observe cancellation")

    monkeypatch.setattr(composition.discovery, "discover_files", slow_discovery)
    start = TrackJobInput(
        workflow_id="WF-001",
        inventory_id=uuid4(),
        discovery=DiscoverFilesInput(
            workspace_id=registered.workspace_id,
            include_paths=(),
        ),
        definition=JobDefinition(
            job_type="WF-001",
            steps=(JobStepDefinition(sequence=1, tool_id="TOOL-002", input_ref="workspace"),),
        ),
        deduplication_key=f"wf-001:{uuid4()}",
    )
    created = composition.registry.invoke(
        "TOOL-025", _request(registered.workspace_id, start)
    ).output
    assert created is not None
    for _ in range(100):
        running = composition.registry.invoke(
            "TOOL-026",
            _request(
                registered.workspace_id,
                QueryOrCancelJobInput(
                    action=JobControlAction.QUERY,
                    job_id=created.descriptor.job.job_id,
                ),
            ),
        ).output
        assert running is not None
        if running.snapshot.job.status.value == "running":
            break
        sleep(0.005)
    assert running is not None
    assert running.snapshot.job.status.value == "running"
    composition.registry.invoke(
        "TOOL-026",
        _request(
            registered.workspace_id,
            QueryOrCancelJobInput(
                action=JobControlAction.CANCEL,
                job_id=created.descriptor.job.job_id,
            ),
        ),
    )
    result = _wait_for_terminal(composition, registered.workspace_id, created.descriptor.job.job_id)
    assert saw_cancellation_callback
    assert result.snapshot.job.status.value == "cancelled"
    composition.database.dispose()


def test_inventory_projects_current_candidate_for_source_file(tmp_path: Path) -> None:
    composition = create_runtime(tmp_path / "state.db")
    inventory_id, workspace_id, file_id = uuid4(), uuid4(), uuid4()
    file = FileAssetRef(
        file_id=file_id,
        workspace_id=workspace_id,
        relative_path=WorkspaceRelativePath("data/example.sgy"),
        size_bytes=1,
        modified_at=datetime(2026, 9, 9, tzinfo=UTC),
        sha256="a" * 64,
        discovery_version=1,
    )
    composition.registry.invoke(
        "TOOL-022",
        _request(
            workspace_id,
            PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
                expected_state_version=0,
            ),
        ),
    )
    candidate = GeneratedManifestCandidate(
        reference=GeneratedCandidateRef(
            candidate_id=uuid4(),
            source_file_id=file_id,
            source_sha256="a" * 64,
            learning_model_id=uuid4(),
            model_sha256="b" * 64,
            candidate_sha256="c" * 64,
            proposed_path=WorkspaceRelativePath("generated/example.json"),
            generation_status=GenerationStatus.PROPOSED,
            validation_status=ValidationStatus.VALID,
            review_status=ReviewStatus.NEEDS_CHANGES,
            trust_level=TrustLevel.HEURISTIC,
        ),
        document=ManifestJsonDocument(sha256="c" * 64, content={}),
    )
    composition.repository.persist_generated_candidate(candidate)

    view = composition.registry.invoke(
        "TOOL-027",
        _request(workspace_id, BuildInventoryReviewInput(inventory_id=inventory_id)),
    ).output

    assert view is not None
    assert view.items[0].candidate is not None
    assert view.items[0].candidate.candidate_id == candidate.reference.candidate_id
    assert view.items[0].candidate.review_status is ReviewStatus.NEEDS_CHANGES
    assert view.items[0].candidate.candidate_sha256 == candidate.reference.candidate_sha256
    composition.database.dispose()


def test_tool020_persists_parsed_manifest_validation_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    manifests = root / "Manifests"
    manifests.mkdir(parents=True)
    (manifests / "example.json").write_text(
        '{"kind":"osdu:wks:Manifest:1.0.0","Data":{}}',
        encoding="utf-8",
    )
    composition = create_runtime(tmp_path / "state.db")
    workspace = composition.registry.invoke(
        "TOOL-001",
        _request(uuid4(), RegisterWorkspaceInput(root_path=str(root))),
    ).output
    assert workspace is not None
    parsed = composition.registry.invoke(
        "TOOL-014",
        _request(
            workspace.workspace_id,
            ParseManifestsInput(
                workspace_id=workspace.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
            ),
        ),
    ).output
    assert parsed is not None
    manifest = parsed.manifests[0]

    validated = composition.registry.invoke(
        "TOOL-020",
        _request(
            workspace.workspace_id,
            ValidateSchemasInput(manifest=manifest, schema_catalog_id=uuid4()),
        ),
    )
    review = composition.registry.invoke(
        "TOOL-028",
        _request(
            workspace.workspace_id,
            BuildManifestReviewInput(manifest_id=manifest.document.manifest_id),
        ),
    ).output

    assert validated.output is not None
    assert review is not None
    assert review.content == manifest.content
    assert review.validation == validated.output
    assert [item.tool_id for item in review.provenance] == ["TOOL-020"]
    composition.database.dispose()
