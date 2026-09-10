from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import sleep
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.domain.models import (
    ActorRef,
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatId,
    ManifestJsonDocument,
    OSDUKind,
    ProcessingLevel,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.jobs.service import JobError, JobExecutionContext, JobService
from agentic_osdu.runtime import create_runtime
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import Base, WorkspaceEntity
from agentic_osdu.tools.contracts import (
    BuildManifestReviewInput,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    InventoryMutation,
    JobControlAction,
    JobDefinition,
    JobStepDefinition,
    LearningModelContract,
    LearningModelMutation,
    LearningMutationAction,
    LocalSchemaCatalogImport,
    PersistInventoryInput,
    PersistLearningModelInput,
    QueryOrCancelJobInput,
    RegisterWorkspaceInput,
    SchemaCatalogSource,
    SchemaChecksum,
    ToolRequest,
    TrackJobInput,
)


def test_registered_workspace_is_persisted_and_hydrated_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    root = tmp_path / "workspace"
    root.mkdir()
    first = create_runtime(state_path)
    result = first.registry.invoke(
        "TOOL-001",
        ToolRequest(
            request_id=uuid4(),
            workspace_id=uuid4(),
            actor=ActorRef(actor_id="local-user"),
            input=RegisterWorkspaceInput(root_path=str(root)),
        ),
    )
    assert result.output is not None
    workspace_id = result.output.workspace_id
    with first.database.session_factory() as session:
        assert session.get(WorkspaceEntity, str(workspace_id)) is not None
    first.database.dispose()

    restarted = create_runtime(state_path)
    assert restarted.workspace_store.get(workspace_id) == result.output
    restarted.database.dispose()


def test_inventory_generate_handler_is_bound_once_outside_row_toggle_loop() -> None:
    source = Path("web/src/inventory.ts").read_text(encoding="utf-8")
    toggle_loop = source.index(
        'document.querySelectorAll<HTMLButtonElement>(".row-toggle").forEach'
    )
    generate_loop = source.index(
        'document.querySelectorAll<HTMLButtonElement>(".generate-one").forEach'
    )
    bind_function = source.index("function bind(): void")
    toggle_block_end = source.index("\n  });", toggle_loop)
    assert toggle_loop < toggle_block_end < generate_loop < bind_function


def test_wf005_all_failed_and_mixed_outcomes_have_distinct_terminal_states(
    tmp_path: Path,
) -> None:
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(database.engine)
    service = JobService(database.session_factory, worker_limit=1)
    actor = ActorRef(actor_id="local-user")

    def run(code: str, steps: int) -> str:
        created = service.create_job(
            request_id=uuid4(),
            actor=actor,
            request=TrackJobInput(
                workflow_id="WF-005",
                generation=GenerateAllManifestsInput(
                    inventory_id=uuid4(),
                    schema_catalog_id=uuid4(),
                    generation_policy_version="1.0.0",
                ),
                definition=JobDefinition(
                    job_type="WF-005",
                    steps=tuple(
                        JobStepDefinition(
                            sequence=index + 1,
                            tool_id="TOOL-027",
                            input_ref=f"candidate-{index}",
                        )
                        for index in range(steps)
                    ),
                    max_concurrency=1,
                    continue_on_error=True,
                ),
                deduplication_key=f"{code}:{uuid4()}",
            ),
        )

        def execute(step: JobStepDefinition, _context: JobExecutionContext) -> None:
            if step.sequence == steps:
                raise JobError(code, "Synthetic aggregate result.")

        return service.execute(created.descriptor.job.job_id, execute).job.status.value

    assert run("JOB_ALL_ITEMS_FAILED", 1) == "failed"
    assert run("JOB_PARTIAL_ITEMS_FAILED", 2) == "partially_succeeded"
    database.dispose()


def test_restart_hydrates_generation_inputs_and_persists_dry_run_candidate(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "runtime.db"
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sample.sgy").write_bytes(b"x")
    first = create_runtime(state_path)
    actor = ActorRef(actor_id="local-user")

    def request(workspace_id: UUID, value: Any) -> ToolRequest[Any]:
        return ToolRequest[Any](
            request_id=uuid4(),
            workspace_id=workspace_id,
            actor=actor,
            input=value,
        )

    workspace = first.registry.invoke(
        "TOOL-001", request(uuid4(), RegisterWorkspaceInput(root_path=str(root)))
    ).output
    assert workspace is not None
    inventory_id, file_id, detection_id = uuid4(), uuid4(), uuid4()
    asset = FileAssetRef(
        file_id=file_id,
        workspace_id=workspace.workspace_id,
        relative_path=WorkspaceRelativePath("sample.sgy"),
        size_bytes=1,
        modified_at=datetime.now(UTC),
        sha256=sha256(b"x").hexdigest(),
        discovery_version=1,
    )
    classification = ClassificationRecord(
        classification_id=uuid4(),
        file_id=file_id,
        format_id=FormatId.SEGY,
        category=DataCategory.SEISMIC,
        subtype=DataSubtype.SEGY,
        dimensions=ClassificationDimensions(),
        stack=StackType.POST_STACK,
        domain=DataDomain.TIME,
        processing=ProcessingLevel.PROCESSED,
        survey=SurveyType.THREE_D,
        well=WellDataType.NOT_APPLICABLE,
        osdu_kind=OSDUKind("osdu:wks:work-product-component--SeismicTraceData:1.0.0"),
        confidence=1.0,
        detection_id=detection_id,
        extraction_ids=(),
        evidence_ids=(),
        trust_level=TrustLevel.DERIVED,
    )
    first.registry.invoke(
        "TOOL-022",
        request(
            workspace.workspace_id,
            PersistInventoryInput(
                mutation=InventoryMutation(
                    inventory_id=inventory_id,
                    files=(asset,),
                    classifications=(classification,),
                ),
                expected_state_version=0,
            ),
        ),
    )
    content: dict[str, Any] = {"kind": "osdu:wks:Manifest:1.0.0", "Data": {}}
    model = LearningModelContract(
        learning_model_id=uuid4(),
        category=DataCategory.SEISMIC,
        version=1,
        model_sha256="d" * 64,
        example_ids=(),
        example_identities=(),
        prototype=ManifestJsonDocument(
            sha256=sha256(
                __import__("json").dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            content=content,
        ),
        constants=(),
        prototype_source_path=WorkspaceRelativePath("source.sgy"),
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )
    created = first.registry.invoke(
        "TOOL-024",
        request(
            workspace.workspace_id,
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
    first.registry.invoke(
        "TOOL-024",
        request(
            workspace.workspace_id,
            PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.ACTIVATE,
                    learning_model_id=model.learning_model_id,
                    expected_version=created.version,
                )
            ),
        ),
    )
    first.database.dispose()

    restarted = create_runtime(state_path)
    assert restarted._require_file(file_id) == asset
    with pytest.raises(OrchestrationError, match="not discovered"):
        restarted._require_file(uuid4())
    generated = restarted.registry.invoke(
        "TOOL-018",
        request(
            workspace.workspace_id,
            GenerateManifestInput(
                file_id=file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version="1.0.0",
                dry_run=True,
            ),
        ),
    ).output
    assert generated is not None
    assert generated.candidate.reference.generation_status.value == "proposed"
    assert not (root / generated.candidate.reference.proposed_path.root).exists()
    assert (
        restarted.repository.get_generated_candidate(generated.candidate.reference.candidate_id)
        is not None
    )
    reviewed = restarted.registry.invoke(
        "TOOL-028",
        request(
            workspace.workspace_id,
            BuildManifestReviewInput(
                manifest_id=generated.candidate.reference.candidate_id,
                source_file_id=file_id,
            ),
        ),
    ).output
    assert reviewed is not None
    assert reviewed.generation_diff is not None
    assert reviewed.generation_diff.added_pointers

    schema_source = tmp_path / "schema-source"
    schema_path = schema_source / "manifest" / "Manifest.1.0.0.json"
    schema_path.parent.mkdir(parents=True)
    schema_payload = json.dumps({"schema": {"type": "object"}}, sort_keys=True).encode()
    schema_path.write_bytes(schema_payload)
    catalog = restarted.schema_catalog_store.import_local(
        LocalSchemaCatalogImport(
            source=SchemaCatalogSource.LOCAL_EXPORT,
            revision="epic-009-review",
            local_root=str(schema_source),
            expected_checksums=(
                SchemaChecksum(
                    relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                    sha256=sha256(schema_payload).hexdigest(),
                ),
            ),
        )
    )
    generated_all = restarted.registry.invoke(
        "TOOL-019",
        request(
            workspace.workspace_id,
            GenerateAllManifestsInput(
                inventory_id=inventory_id,
                schema_catalog_id=catalog.schema_catalog_id,
                generation_policy_version="1.0.0",
                dry_run=True,
            ),
        ),
    ).output
    assert generated_all is not None
    assert generated_all.generated == 1
    assert generated_all.candidates[0].reference.generation_status.value == "proposed"
    assert (
        restarted.repository.get_generated_candidate(
            generated_all.candidates[0].reference.candidate_id
        )
        is not None
    )

    job = restarted.registry.invoke(
        "TOOL-025",
        request(
            workspace.workspace_id,
            TrackJobInput(
                workflow_id="WF-005",
                generation=GenerateAllManifestsInput(
                    inventory_id=inventory_id,
                    schema_catalog_id=catalog.schema_catalog_id,
                    generation_policy_version="1.0.0",
                ),
                definition=JobDefinition(
                    job_type="WF-005",
                    steps=(
                        JobStepDefinition(sequence=1, tool_id="TOOL-019", input_ref="batch"),
                        JobStepDefinition(sequence=2, tool_id="TOOL-020", input_ref="candidates"),
                        JobStepDefinition(sequence=3, tool_id="TOOL-022", input_ref="inventory"),
                        JobStepDefinition(sequence=4, tool_id="TOOL-023", input_ref="associations"),
                        JobStepDefinition(sequence=5, tool_id="TOOL-027", input_ref="review"),
                    ),
                ),
                deduplication_key=f"wf-005:{uuid4()}",
            ),
        ),
    ).output
    assert job is not None
    for _ in range(100):
        queried = restarted.registry.invoke(
            "TOOL-026",
            request(
                workspace.workspace_id,
                QueryOrCancelJobInput(
                    action=JobControlAction.QUERY,
                    job_id=job.descriptor.job.job_id,
                ),
            ),
        ).output
        assert queried is not None
        if queried.snapshot.job.status.value in {"succeeded", "failed", "partially_succeeded"}:
            break
        sleep(0.01)
    assert queried is not None
    assert queried.snapshot.job.status.value == "succeeded"
    persisted = restarted.repository.get_generated_candidate(
        generated_all.candidates[0].reference.candidate_id
    )
    assert persisted is not None
    assert persisted.reference.validation_status.value == "valid"
    restarted.database.dispose()
