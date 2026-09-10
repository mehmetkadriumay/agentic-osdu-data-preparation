from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from agentic_osdu.api.app import create_app
from agentic_osdu.domain.models import (
    ActorRef,
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatId,
    LearningExampleRef,
    ManifestAssociation,
    ManifestDocumentRef,
    ManifestJsonDocument,
    ProcessingLevel,
    ReviewDecision,
    ReviewDecisionValue,
    ReviewStatus,
    ReviewTargetType,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.generate import GenerationError
from agentic_osdu.runtime import create_runtime
from agentic_osdu.state.models import ReviewDecisionEntity
from agentic_osdu.state.repositories import StateConflictError
from agentic_osdu.tools.contracts import (
    AssociationMutation,
    GenerateManifestInput,
    InventoryMutation,
    LearnManifestPatternsInput,
    PersistAssociationsInput,
    PersistInventoryInput,
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
)

ACTOR = ActorRef(actor_id="local-user")


def _request(workspace_id: Any, value: Any) -> ToolRequest[Any]:
    return ToolRequest[Any](
        request_id=uuid4(),
        workspace_id=workspace_id,
        actor=ACTOR,
        input=value,
    )


def test_learning_runtime_rejects_unpersisted_or_tampered_approved_examples(
    tmp_path: Path,
) -> None:
    composition = create_runtime(tmp_path / "state.db")
    inventory_id, file_id, manifest_id, association_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    workspace_root = tmp_path / "workspace"
    (workspace_root / "data").mkdir(parents=True)
    (workspace_root / "data" / "example.sgy").write_bytes(b"x")
    registered = composition.registry.invoke(
        "TOOL-001",
        _request(
            uuid4(),
            RegisterWorkspaceInput(root_path=str(workspace_root)),
        ),
    ).output
    assert registered is not None
    workspace_id = registered.workspace_id
    file = FileAssetRef(
        file_id=file_id,
        workspace_id=workspace_id,
        relative_path=WorkspaceRelativePath("data/example.sgy"),
        size_bytes=1,
        modified_at=datetime(2026, 9, 9, tzinfo=UTC),
        sha256="a" * 64,
        discovery_version=1,
    )
    manifest = ManifestDocumentRef(
        manifest_id=manifest_id,
        path=WorkspaceRelativePath("Manifests/example.json"),
        sha256="b" * 64,
        generated=False,
    )
    composition.repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(
                inventory_id=inventory_id,
                files=(file,),
                classifications=(
                    ClassificationRecord(
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
                        confidence=1,
                        detection_id=uuid4(),
                        extraction_ids=(),
                        evidence_ids=(),
                        trust_level=TrustLevel.DERIVED,
                    ),
                ),
            ),
            expected_state_version=0,
        ),
    )
    composition.repository.persist_manifest(
        manifest,
        ManifestJsonDocument(
            sha256=manifest.sha256,
            content={"kind": "osdu:wks:Manifest:1.0.0", "Data": {}},
        ),
    )
    association = ManifestAssociation(
        association_id=association_id,
        file_id=file_id,
        manifest_id=manifest_id,
        score=1,
        method="exact_filename",
        evidence_ids=(),
        target_version=manifest.sha256,
    )
    composition.repository.persist_associations(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistAssociationsInput(
            mutations=(
                AssociationMutation(
                    association=association,
                    review_status=ReviewStatus.PROPOSED,
                ),
            ),
            expected_state_version=0,
        ),
    )
    composition.repository.record_review_decision(
        request_id=uuid4(),
        decision=ReviewDecision(
            decision_id=uuid4(),
            actor=ACTOR,
            target_type=ReviewTargetType.MANIFEST_ASSOCIATION,
            target_id=association_id,
            target_version=manifest.sha256,
            decision=ReviewDecisionValue.APPROVE,
            reason="approved source example",
            decided_at=datetime.now(UTC),
        ),
    )
    composition.hydrate()
    trusted = LearningExampleRef(
        example_id=uuid4(),
        source_file_id=file_id,
        manifest_id=manifest_id,
        association_id=association_id,
        source_sha256="a" * 64,
        manifest_sha256=manifest.sha256,
        review_status=ReviewStatus.APPROVED,
    )
    assert composition.repository.resolve_learning_examples((trusted,)) == (trusted,)
    learned = composition.registry.invoke(
        "TOOL-017",
        _request(
            workspace_id,
            LearnManifestPatternsInput(
                examples=(trusted,),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
        ),
    ).output
    assert learned is not None
    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        composition.registry.invoke(
            "TOOL-018",
            _request(
                workspace_id,
                GenerateManifestInput(
                    file_id=file_id,
                    learning_model_id=learned.model.learning_model_id,
                    generation_policy_version="1.0.0",
                ),
            ),
        )
    for tampered in (
        trusted.model_copy(update={"source_sha256": "c" * 64}),
        trusted.model_copy(update={"manifest_sha256": "c" * 64}),
        trusted.model_copy(update={"association_id": uuid4()}),
    ):
        with pytest.raises(StateConflictError, match="NO_ELIGIBLE_EXAMPLES"):
            composition.registry.invoke(
                "TOOL-017",
                _request(
                    workspace_id,
                    LearnManifestPatternsInput(
                        examples=(tampered,),
                        category=DataCategory.SEISMIC,
                        learning_policy_version="1.0.0",
                    ),
                ),
            )
    with composition.repository.session_factory.begin() as session:
        session.execute(delete(ReviewDecisionEntity))
    with pytest.raises(StateConflictError, match="NO_ELIGIBLE_EXAMPLES"):
        composition.registry.invoke(
            "TOOL-017",
            _request(
                workspace_id,
                LearnManifestPatternsInput(
                    examples=(trusted,),
                    category=DataCategory.SEISMIC,
                    learning_policy_version="1.0.0",
                ),
            ),
        )
    composition.database.dispose()


def test_generation_runtime_preserves_requested_write_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = create_runtime(tmp_path / "state.db")
    observed: list[bool] = []
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = composition.discovery.register_workspace(
        RegisterWorkspaceInput(root_path=str(root))
    )

    class GenerationSpy:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def generate_one(self, value: GenerateManifestInput) -> Any:
            observed.append(value.dry_run)
            raise GenerationError("NO_COMPATIBLE_MODEL", "stop after observing request")

    monkeypatch.setattr("agentic_osdu.runtime.GenerationService", GenerationSpy)
    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        composition._generate_one(
            _request(
                workspace.workspace_id,
                GenerateManifestInput(
                    file_id=uuid4(),
                    learning_model_id=uuid4(),
                    generation_policy_version="1.0.0",
                    dry_run=False,
                ),
            )
        )
    assert observed == [False]
    composition.database.dispose()


class _UnusedInvoker:
    def invoke(self, _tool_id: str, _request: ToolRequest[Any]) -> ToolResult[Any]:
        raise AssertionError("request must be rejected before invoking a tool")


@pytest.mark.parametrize(
    ("path", "input_value"),
    [
        (
            "/api/v1/generation/one",
            {
                "file_id": str(uuid4()),
                "learning_model_id": str(uuid4()),
                "generation_policy_version": "1.0.0",
                "dry_run": False,
            },
        ),
        (
            "/api/v1/generation/all",
            {
                "inventory_id": str(uuid4()),
                "schema_catalog_id": str(uuid4()),
                "generation_policy_version": "1.0.0",
                "dry_run": False,
            },
        ),
    ],
)
def test_direct_generation_write_requires_human_approval_boundary(
    path: str, input_value: dict[str, object]
) -> None:
    response = TestClient(create_app(_UnusedInvoker())).post(
        path,
        json={
            "request_id": str(uuid4()),
            "workspace_id": str(uuid4()),
            "actor": {"actor_id": "local-user"},
            "input": input_value,
        },
    )
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "HUMAN_APPROVAL_REQUIRED"


def test_bundled_web_assets_are_present_and_served() -> None:
    from agentic_osdu.api.app import bundled_web_directory

    assets = bundled_web_directory()
    assert (assets / "index.html").is_file()
    assert (assets / "manifest.html").is_file()

    response = TestClient(create_app(_UnusedInvoker(), web_directory=assets)).get("/")

    assert response.status_code == 200
    assert "Inventory review" in response.text
