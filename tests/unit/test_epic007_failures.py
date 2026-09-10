from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

import agentic_osdu.jobs.service as job_service_module
from agentic_osdu.domain.models import (
    ActorRef,
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatCandidate,
    FormatDetectionResult,
    FormatId,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    GenerationStatus,
    JobStatus,
    ManifestAssociation,
    ManifestDocumentRef,
    ManifestJsonDocument,
    MetadataExtractionRef,
    OSDUKind,
    ProcessingLevel,
    ReviewDecision,
    ReviewDecisionValue,
    ReviewStatus,
    ReviewTargetType,
    StackType,
    SurveyType,
    TrustLevel,
    ValidationStatus,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.jobs.service import JobError, JobExecutionContext, JobService
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import (
    Base,
    GeneratedCandidateEntity,
    JobEntity,
    ManifestAssociationEntity,
    ReviewDecisionEntity,
)
from agentic_osdu.state.repositories import StateConflictError, StateRepository
from agentic_osdu.tools.contracts import (
    AssociationMutation,
    BuildInventoryReviewInput,
    BuildManifestReviewInput,
    ExportKind,
    ExportRequest,
    ExportToolInput,
    InventoryMutation,
    JobControlAction,
    JobDefinition,
    JobStepDefinition,
    PersistAssociationsInput,
    PersistInventoryInput,
    QueryOrCancelJobInput,
    RecordReviewDecisionInput,
    TrackJobInput,
)
from agentic_osdu.tools.review import ReviewError, ReviewService

NOW = datetime(2026, 9, 9, 22, 0, tzinfo=UTC)
ACTOR = ActorRef(actor_id="tester")
ZERO_HASH = "0" * 64


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[StateRepository]:
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    try:
        yield StateRepository(database.session_factory)
    finally:
        database.dispose()


def _persist_file(repository: StateRepository) -> tuple[UUID, FileAssetRef]:
    inventory_id = uuid4()
    file = FileAssetRef(
        file_id=uuid4(),
        workspace_id=inventory_id,
        relative_path=WorkspaceRelativePath("data/file.las"),
        size_bytes=1,
        modified_at=NOW,
        sha256=ZERO_HASH,
        discovery_version=1,
    )
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    return inventory_id, file


def _candidate(
    file_id: UUID, *, candidate_id: UUID | None = None, name: str = "one"
) -> GeneratedManifestCandidate:
    content: dict[str, JsonValue] = {"name": name}
    encoded = (
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    digest = sha256(encoded).hexdigest()
    return GeneratedManifestCandidate(
        reference=GeneratedCandidateRef(
            candidate_id=candidate_id or uuid4(),
            source_file_id=file_id,
            source_sha256=ZERO_HASH,
            learning_model_id=uuid4(),
            model_sha256="1" * 64,
            candidate_sha256=digest,
            proposed_path=WorkspaceRelativePath("generated/file.json"),
            generation_status=GenerationStatus.PROPOSED,
            validation_status=ValidationStatus.NOT_RUN,
        ),
        document=ManifestJsonDocument(sha256=digest, content=content),
    )


def _review_service(repository: StateRepository, tmp_path: Path) -> ReviewService:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    return ReviewService(
        repository,
        WorkspaceAccessPolicy(
            workspace_id=uuid4(),
            source_root=str(source),
            output_roots={"reports": str(output)},
            path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        ),
    )


def _job_request(key: str, sequences: tuple[int, ...] = (1,)) -> TrackJobInput:
    return TrackJobInput(
        definition=JobDefinition(
            job_type="unit",
            steps=tuple(
                JobStepDefinition(sequence=value, tool_id="TOOL-003", input_ref=str(value))
                for value in sequences
            ),
        ),
        deduplication_key=key,
    )


def test_database_rejects_non_sqlite_url() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        create_sqlite_state("postgresql://unused")


def test_inventory_replay_mismatch_update_and_remove(repository: StateRepository) -> None:
    inventory_id, file = _persist_file(repository)
    request_id = uuid4()
    request = PersistInventoryInput(
        mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
        expected_state_version=1,
    )
    repository.persist_inventory(request_id=request_id, actor=ACTOR, request=request)
    changed = request.model_copy(update={"expected_state_version": 2})
    with pytest.raises(StateConflictError, match="IDEMPOTENCY_KEY_REUSED"):
        repository.persist_inventory(request_id=request_id, actor=ACTOR, request=changed)

    result = repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(
                inventory_id=inventory_id,
                remove_file_ids=(file.file_id,),
            ),
            expected_state_version=2,
        ),
    )
    assert result.file_count == 0


def test_inventory_persists_and_updates_detection_extraction_and_classification(
    repository: StateRepository,
) -> None:
    inventory_id = uuid4()
    file = FileAssetRef(
        file_id=uuid4(),
        workspace_id=inventory_id,
        relative_path=WorkspaceRelativePath("data/file.las"),
        size_bytes=1,
        modified_at=NOW,
        sha256=ZERO_HASH,
        discovery_version=1,
    )
    detection = FormatDetectionResult(
        detection_id=uuid4(),
        file_id=file.file_id,
        candidates=(FormatCandidate(format_id=FormatId.LAS, confidence=1.0, evidence_ids=()),),
        detector_version="1.0.0",
        detected_at=NOW,
    )
    extraction = MetadataExtractionRef(
        extraction_id=uuid4(),
        file_id=file.file_id,
        file_sha256=ZERO_HASH,
        format_id=FormatId.LAS,
        parser_version="1.0.0",
        extracted_at=NOW,
        trust_level=TrustLevel.VERIFIED,
    )
    classification = ClassificationRecord(
        classification_id=uuid4(),
        file_id=file.file_id,
        format_id=FormatId.LAS,
        category=DataCategory.WELL_LOG,
        subtype=DataSubtype.LAS,
        dimensions=ClassificationDimensions(),
        stack=StackType.NOT_APPLICABLE,
        domain=DataDomain.DEPTH,
        processing=ProcessingLevel.RAW,
        survey=SurveyType.NOT_APPLICABLE,
        well=WellDataType.LOG,
        confidence=1.0,
        detection_id=detection.detection_id,
        extraction_ids=(extraction.extraction_id,),
        evidence_ids=(),
        trust_level=TrustLevel.VERIFIED,
    )
    request = PersistInventoryInput(
        mutation=InventoryMutation(
            inventory_id=inventory_id,
            files=(file,),
            detections=(detection,),
            extractions=(extraction,),
            classifications=(classification,),
        ),
        expected_state_version=0,
    )
    repository.persist_inventory(request_id=uuid4(), actor=ACTOR, request=request)
    replay_as_update = request.model_copy(update={"expected_state_version": 1})
    result = repository.persist_inventory(request_id=uuid4(), actor=ACTOR, request=replay_as_update)
    assert result.state_version == 2


def test_association_replay_update_and_identity_conflict(repository: StateRepository) -> None:
    _, file = _persist_file(repository)
    association_id = uuid4()
    association = ManifestAssociation(
        association_id=association_id,
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.5,
        method="normalized_identifier",
        evidence_ids=(),
        target_version="one",
    )
    request = PersistAssociationsInput(
        mutations=(
            AssociationMutation(
                association=association,
                review_status=ReviewStatus.PROPOSED,
            ),
        ),
        expected_state_version=0,
    )
    request_id = uuid4()
    first = repository.persist_associations(request_id=request_id, actor=ACTOR, request=request)
    assert (
        repository.persist_associations(request_id=request_id, actor=ACTOR, request=request)
        == first
    )
    updated = request.model_copy(update={"expected_state_version": 1})
    assert (
        repository.persist_associations(
            request_id=uuid4(), actor=ACTOR, request=updated
        ).state_version
        == 2
    )
    reassigned = association.model_copy(update={"manifest_id": uuid4()})
    with pytest.raises(StateConflictError, match="ASSOCIATION_CONFLICT"):
        repository.persist_associations(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistAssociationsInput(
                mutations=(
                    AssociationMutation(
                        association=reassigned,
                        review_status=ReviewStatus.PROPOSED,
                    ),
                ),
                expected_state_version=2,
            ),
        )
    receipt = repository.record_review_decision(
        request_id=uuid4(),
        decision=ReviewDecision(
            decision_id=uuid4(),
            actor=ACTOR,
            target_type=ReviewTargetType.MANIFEST_ASSOCIATION,
            target_id=association_id,
            target_version="one",
            decision=ReviewDecisionValue.APPROVE,
            reason="Exact match confirmed.",
            decided_at=NOW,
        ),
    )
    assert receipt.review_status is ReviewStatus.APPROVED
    with repository.session_factory() as session:
        row = session.get(ManifestAssociationEntity, str(association_id))
        assert row is not None
        assert row.trust_level == TrustLevel.VERIFIED.value


def test_association_persistence_rejects_stale_review_decision(
    repository: StateRepository,
) -> None:
    _, file = _persist_file(repository)
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.5,
        method="normalized_identifier",
        evidence_ids=(),
        target_version="v1",
    )
    repository.persist_associations(
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
    decision_id = uuid4()
    repository.record_review_decision(
        request_id=uuid4(),
        decision=ReviewDecision(
            decision_id=decision_id,
            actor=ACTOR,
            target_type=ReviewTargetType.MANIFEST_ASSOCIATION,
            target_id=association.association_id,
            target_version="v1",
            decision=ReviewDecisionValue.APPROVE,
            reason="Version one is correct.",
            decided_at=NOW,
        ),
    )

    with pytest.raises(StateConflictError, match="INVALID_REVIEW_TRANSITION"):
        repository.persist_associations(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistAssociationsInput(
                mutations=(
                    AssociationMutation(
                        association=association.model_copy(update={"target_version": "v2"}),
                        review_status=ReviewStatus.APPROVED,
                        reviewer_decision_id=decision_id,
                    ),
                ),
                expected_state_version=1,
            ),
        )


def test_association_persistence_rejects_wrong_review_target_type(
    repository: StateRepository,
) -> None:
    _, file = _persist_file(repository)
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.5,
        method="normalized_identifier",
        evidence_ids=(),
        target_version="v1",
    )
    repository.persist_associations(
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
    decision_id = uuid4()
    with repository.session_factory.begin() as session:
        session.add(
            ReviewDecisionEntity(
                id=str(decision_id),
                idempotency_key="f" * 64,
                actor=ACTOR.model_dump(mode="json"),
                target_type=ReviewTargetType.GENERATED_CANDIDATE.value,
                target_id=str(association.association_id),
                target_version=association.target_version,
                decision=ReviewDecisionValue.APPROVE.value,
                reason="A decision for a different target type.",
                decided_at=NOW,
                state_version=1,
            )
        )

    with pytest.raises(StateConflictError, match="INVALID_REVIEW_TRANSITION"):
        repository.persist_associations(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistAssociationsInput(
                mutations=(
                    AssociationMutation(
                        association=association,
                        review_status=ReviewStatus.APPROVED,
                        reviewer_decision_id=decision_id,
                    ),
                ),
                expected_state_version=1,
            ),
        )


def test_manifest_and_candidate_identity_conflicts_and_context(
    repository: StateRepository,
) -> None:
    manifest_id = uuid4()
    manifest = ManifestDocumentRef(
        manifest_id=manifest_id,
        path=WorkspaceRelativePath("manifests/a.json"),
        sha256=ZERO_HASH,
        document_kind=OSDUKind("osdu:wks:Manifest:1.0.0"),
    )
    content = ManifestJsonDocument(sha256=ZERO_HASH, content={})
    repository.persist_manifest(manifest, content)
    repository.persist_manifest(manifest, content)
    assert repository.get_manifest_content(manifest_id) == content
    assert repository.get_manifest_review_context(manifest_id) == (None, ())
    assert repository.get_manifest_content(uuid4()) is None
    with pytest.raises(StateConflictError, match="STATE_VERSION_CONFLICT"):
        repository.persist_manifest(
            manifest.model_copy(update={"sha256": "2" * 64}),
            ManifestJsonDocument(sha256="2" * 64, content={}),
        )

    _, file = _persist_file(repository)
    candidate_id = uuid4()
    candidate = _candidate(file.file_id, candidate_id=candidate_id)
    repository.persist_generated_candidate(candidate)
    repository.persist_generated_candidate(candidate)
    assert repository.get_candidate_review_context(candidate_id) == (None, (), None)
    assert repository.get_generated_candidate(uuid4()) is None
    with pytest.raises(StateConflictError, match="STATE_VERSION_CONFLICT"):
        repository.persist_generated_candidate(
            _candidate(file.file_id, candidate_id=candidate_id, name="changed")
        )


def test_review_rejects_missing_stale_changed_and_unknown_targets(
    repository: StateRepository, tmp_path: Path
) -> None:
    _, file = _persist_file(repository)
    candidate = _candidate(file.file_id)
    repository.persist_generated_candidate(candidate)
    service = _review_service(repository, tmp_path)

    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        service.record_decision(
            request_id=uuid4(),
            request=RecordReviewDecisionInput(
                decision=ReviewDecision(
                    decision_id=uuid4(),
                    actor=ACTOR,
                    target_type=ReviewTargetType.GENERATED_CANDIDATE,
                    target_id=candidate.reference.candidate_id,
                    target_version="stale",
                    decision=ReviewDecisionValue.APPROVE,
                    reason="Wrong version.",
                    decided_at=NOW,
                )
            ),
        )
    missing = ReviewDecision(
        decision_id=uuid4(),
        actor=ACTOR,
        target_type=ReviewTargetType.GENERATED_CANDIDATE,
        target_id=uuid4(),
        target_version=ZERO_HASH,
        decision=ReviewDecisionValue.REJECT,
        reason="Not present.",
        decided_at=NOW,
    )
    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        service.record_decision(
            request_id=uuid4(), request=RecordReviewDecisionInput(decision=missing)
        )

    valid = ReviewDecision(
        decision_id=uuid4(),
        actor=ACTOR,
        target_type=ReviewTargetType.GENERATED_CANDIDATE,
        target_id=candidate.reference.candidate_id,
        target_version=candidate.reference.candidate_sha256,
        decision=ReviewDecisionValue.NEEDS_CHANGES,
        reason="Revise it.",
        decided_at=NOW,
    )
    service.record_decision(request_id=uuid4(), request=RecordReviewDecisionInput(decision=valid))
    with pytest.raises(ReviewError, match="INVALID_DECISION"):
        service.record_decision(
            request_id=uuid4(),
            request=RecordReviewDecisionInput(
                decision=valid.model_copy(update={"decision": ReviewDecisionValue.REJECT})
            ),
        )


def test_review_manifest_projection_report_export_and_errors(
    repository: StateRepository, tmp_path: Path
) -> None:
    service = _review_service(repository, tmp_path)
    with pytest.raises(ReviewError, match="INVENTORY_NOT_FOUND"):
        service.build_inventory_review(BuildInventoryReviewInput(inventory_id=uuid4()))
    with pytest.raises(ReviewError, match="MANIFEST_NOT_FOUND"):
        service.build_manifest_review(
            BuildManifestReviewInput(
                manifest=ManifestDocumentRef(
                    manifest_id=uuid4(),
                    path=WorkspaceRelativePath("missing.json"),
                    sha256=ZERO_HASH,
                )
            )
        )

    inventory_id, _ = _persist_file(repository)
    with pytest.raises(ReviewError, match="CANCELLED"):
        service.build_inventory_review(
            BuildInventoryReviewInput(inventory_id=inventory_id),
            cancellation=lambda: True,
        )
    report = ExportToolInput(
        request=ExportRequest(
            export_kind=ExportKind.REPORT,
            target_id=inventory_id,
            output_root_id="reports",
            relative_path=WorkspaceRelativePath("report.json"),
            expected_target_version="1",
        )
    )
    assert service.export(report).receipt.files[0].size_bytes > 0
    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        service.export(
            ExportToolInput(
                request=report.request.model_copy(update={"expected_target_version": "2"})
            )
        )
    with pytest.raises(ReviewError, match="OUTPUT_PATH_DENIED"):
        service.export(
            ExportToolInput(
                request=report.request.model_copy(
                    update={
                        "output_root_id": "unknown",
                        "relative_path": WorkspaceRelativePath("other.json"),
                    }
                )
            )
        )


def test_job_input_query_lease_and_terminal_cancel_errors(repository: StateRepository) -> None:
    with pytest.raises(ValueError, match="worker_limit"):
        JobService(repository.session_factory, worker_limit=0)
    service = JobService(repository.session_factory)
    with pytest.raises(JobError, match="JOB_DEFINITION_INVALID"):
        service.create_job(
            request_id=uuid4(),
            actor=ACTOR,
            request=_job_request(str(uuid4()), sequences=(2,)),
        )
    descriptor = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_request(str(uuid4()))
    ).descriptor
    with pytest.raises(JobError, match="JOB_ALREADY_RUNNING"):
        service.create_job(
            request_id=uuid4(),
            actor=ACTOR,
            request=TrackJobInput(
                definition=JobDefinition(job_type="different", steps=()),
                deduplication_key=descriptor.deduplication_key,
            ),
        )
    with pytest.raises(JobError, match="INVALID_JOB_TRANSITION"):
        service.change_stage(descriptor.job.job_id, "premature")
    with pytest.raises(ValueError, match="lease owner"):
        service.acquire_lease(descriptor.job.job_id, "", lease_seconds=0)
    assert service.acquire_lease(descriptor.job.job_id, "old", lease_seconds=1)
    with repository.session_factory.begin() as session:
        row = session.get(JobEntity, str(descriptor.job.job_id))
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert service.acquire_lease(descriptor.job.job_id, "new", lease_seconds=1)
    with pytest.raises(JobError, match="QUERY_INVALID"):
        service.query(
            QueryOrCancelJobInput(
                action=JobControlAction.QUERY,
                job_id=descriptor.job.job_id,
            ),
            event_limit=0,
        )
    with pytest.raises(JobError, match="JOB_NOT_FOUND"):
        service.query(QueryOrCancelJobInput(action=JobControlAction.QUERY, job_id=uuid4()))
    service.transition(descriptor.job.job_id, JobStatus.RUNNING)
    changed = service.change_stage(descriptor.job.job_id, "extracting")
    assert changed.job.stage == "extracting"
    assert changed.last_event_sequence >= 3
    with pytest.raises(JobError, match="JOB_DEFINITION_INVALID"):
        service.change_stage(descriptor.job.job_id, "")
    service.transition(descriptor.job.job_id, JobStatus.SUCCEEDED)
    result = service.query(
        QueryOrCancelJobInput(
            action=JobControlAction.CANCEL,
            job_id=descriptor.job.job_id,
        )
    )
    assert result.cancellation is not None
    assert not result.cancellation.accepted


def test_execute_holds_a_live_lease_during_active_work(
    repository: StateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_service_module, "_EXECUTION_LEASE_SECONDS", 1)
    monkeypatch.setattr(job_service_module, "_EXECUTION_LEASE_RENEWAL_SECONDS", 0.05)
    service = JobService(repository.session_factory, worker_limit=1)
    descriptor = service.create_job(
        request_id=uuid4(),
        actor=ACTOR,
        request=_job_request(str(uuid4())),
    ).descriptor
    started = threading.Event()
    release = threading.Event()
    results: list[JobStatus] = []

    def work(_step: JobStepDefinition, _context: JobExecutionContext) -> None:
        started.set()
        release.wait(timeout=3)

    thread = threading.Thread(
        target=lambda: results.append(service.execute(descriptor.job.job_id, work).job.status)
    )
    thread.start()
    assert started.wait(timeout=2)
    with repository.session_factory() as session:
        row = session.get(JobEntity, str(descriptor.job.job_id))
        assert row is not None
        assert row.lease_owner is not None
        assert row.lease_expires_at is not None
        first_expiry = row.lease_expires_at

    deadline = time.monotonic() + 1
    renewed_expiry = first_expiry
    while renewed_expiry <= first_expiry and time.monotonic() < deadline:
        time.sleep(0.02)
        with repository.session_factory() as session:
            row = session.get(JobEntity, str(descriptor.job.job_id))
            assert row is not None
            assert row.lease_expires_at is not None
            renewed_expiry = row.lease_expires_at

    assert renewed_expiry > first_expiry
    assert service.recover_interrupted() == 0
    release.set()
    thread.join(timeout=3)
    assert results == [JobStatus.SUCCEEDED]


def test_export_rejects_candidate_with_corrupt_persisted_content(
    repository: StateRepository, tmp_path: Path
) -> None:
    _, file = _persist_file(repository)
    candidate = _candidate(file.file_id)
    repository.persist_generated_candidate(candidate)
    service = _review_service(repository, tmp_path)
    service.record_decision(
        request_id=uuid4(),
        request=RecordReviewDecisionInput(
            decision=ReviewDecision(
                decision_id=uuid4(),
                actor=ACTOR,
                target_type=ReviewTargetType.GENERATED_CANDIDATE,
                target_id=candidate.reference.candidate_id,
                target_version=candidate.reference.candidate_sha256,
                decision=ReviewDecisionValue.APPROVE,
                reason="The persisted candidate is valid.",
                decided_at=NOW,
            )
        ),
    )
    with repository.session_factory.begin() as session:
        row = session.get(GeneratedCandidateEntity, str(candidate.reference.candidate_id))
        assert row is not None
        payload = dict(row.payload)
        document = dict(payload["document"])
        document["content"] = {"name": "corrupt"}
        payload["document"] = document
        row.payload = payload
    request = ExportToolInput(
        request=ExportRequest(
            export_kind=ExportKind.APPROVED_MANIFEST,
            target_id=candidate.reference.candidate_id,
            output_root_id="reports",
            relative_path=WorkspaceRelativePath("candidate.json"),
            expected_target_version=candidate.reference.candidate_sha256,
        )
    )

    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        service.export(request)
    assert not (tmp_path / "output" / "candidate.json").exists()


def test_generation_diff_uses_escaped_json_pointers() -> None:
    from agentic_osdu.tools.review import ReviewService

    diff = ReviewService._diff(
        {"a/b": 1, "gone": True},
        {"a/b": 2, "new~key": [1]},
    )
    assert diff.changed_pointers == ("/a~1b",)
    assert diff.added_pointers == ("/new~0key/0",)
    assert diff.removed_pointers == ("/gone",)
