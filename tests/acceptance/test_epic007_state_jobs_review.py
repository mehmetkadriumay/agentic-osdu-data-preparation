from __future__ import annotations

import ast
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import JsonValue
from sqlalchemy import inspect, select

from agentic_osdu.domain.models import (
    ActorRef,
    DataCategory,
    FileAssetRef,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    GenerationStatus,
    JobEventType,
    JobStatus,
    LearningModelStatus,
    ManifestAssociation,
    ManifestJsonDocument,
    ProvenanceRecord,
    ReviewDecision,
    ReviewDecisionValue,
    ReviewStatus,
    ReviewTargetType,
    TrustLevel,
    ValidationStatus,
    WorkspaceRelativePath,
)
from agentic_osdu.jobs.service import JobError, JobExecutionContext, JobService
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.state.database import StateDatabase, create_sqlite_state
from agentic_osdu.state.models import (
    ALL_ENTITY_TABLE_NAMES,
    AuditEventEntity,
    Base,
    FileAssetEntity,
    GeneratedCandidateEntity,
    InventoryEntity,
    JobEntity,
    LearningExampleEntity,
    LearningModelVersionEntity,
    ManifestAssociationEntity,
    ReviewDecisionEntity,
    StateCounterEntity,
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
    LearningModelContract,
    LearningModelMutation,
    LearningMutationAction,
    PersistAssociationsInput,
    PersistInventoryInput,
    PersistLearningModelInput,
    QueryOrCancelJobInput,
    RecordReviewDecisionInput,
    ReviewQuery,
    TrackJobInput,
)
from agentic_osdu.tools.review import ReviewError, ReviewService

NOW = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
ACTOR = ActorRef(actor_id="reviewer")
ZERO_HASH = "0" * 64


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDatabase]:
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    try:
        yield database
    finally:
        database.dispose()


def _file(
    inventory_id: UUID,
    *,
    file_id: UUID | None = None,
    path: str = "data/a.las",
) -> FileAssetRef:
    return FileAssetRef(
        file_id=file_id or uuid4(),
        workspace_id=inventory_id,
        relative_path=WorkspaceRelativePath(path),
        size_bytes=12,
        modified_at=NOW,
        sha256=ZERO_HASH,
        discovery_version=1,
    )


def _candidate(file_id: UUID, model_id: UUID | None = None) -> GeneratedManifestCandidate:
    content: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {"name": "candidate"},
    }
    encoded = (
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    digest = sha256(encoded).hexdigest()
    return GeneratedManifestCandidate(
        reference=GeneratedCandidateRef(
            candidate_id=uuid4(),
            source_file_id=file_id,
            source_sha256=ZERO_HASH,
            learning_model_id=model_id or uuid4(),
            model_sha256="1" * 64,
            candidate_sha256=digest,
            proposed_path=WorkspaceRelativePath("generated/candidate.json"),
            generation_status=GenerationStatus.PROPOSED,
            validation_status=ValidationStatus.VALID,
            review_status=ReviewStatus.PROPOSED,
            trust_level=TrustLevel.HEURISTIC,
            review_required=True,
        ),
        document=ManifestJsonDocument(sha256=digest, content=content),
    )


def test_item_033_initial_alembic_upgrade_and_downgrade_cover_every_entity(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    database_path = tmp_path / "migration.db"
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    database = create_sqlite_state(f"sqlite:///{database_path}")
    assert set(inspect(database.engine).get_table_names()) >= ALL_ENTITY_TABLE_NAMES
    database.dispose()

    command.downgrade(config, "base")

    database = create_sqlite_state(f"sqlite:///{database_path}")
    assert not (ALL_ENTITY_TABLE_NAMES & set(inspect(database.engine).get_table_names()))
    database.dispose()


def test_item_033_initial_migration_is_explicit_and_immutable() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations" / "versions" / "20260909_0001_initial_state.py"
    )
    source = migration.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "Base" not in imported_names
    assert "create_all" not in source
    assert "drop_all" not in source
    assert source.count("op.create_table(") == len(Base.metadata.tables)
    assert source.count("op.drop_table(") == len(Base.metadata.tables)


def test_tool_022_inventory_is_transactional_versioned_idempotent_and_audited(
    state: StateDatabase,
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    request_id = uuid4()
    file = _file(inventory_id)
    mutation = PersistInventoryInput(
        mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
        expected_state_version=0,
    )

    first = repository.persist_inventory(request_id=request_id, actor=ACTOR, request=mutation)
    replay = repository.persist_inventory(request_id=request_id, actor=ACTOR, request=mutation)

    assert first == replay
    assert first.state_version == 1
    assert first.file_count == 1
    with pytest.raises(StateConflictError, match="STATE_VERSION_CONFLICT"):
        repository.persist_inventory(
            request_id=uuid4(),
            actor=ACTOR,
            request=mutation,
        )
    with state.session_factory() as session:
        assert (
            session.scalar(
                select(AuditEventEntity).where(AuditEventEntity.request_id == str(request_id))
            )
            is not None
        )


def test_tool_022_rejects_cross_inventory_delete_and_reassignment(
    state: StateDatabase,
) -> None:
    repository = StateRepository(state.session_factory)
    first_inventory = uuid4()
    second_inventory = uuid4()
    file = _file(first_inventory)
    for inventory_id, files in ((first_inventory, (file,)), (second_inventory, ())):
        repository.persist_inventory(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id, files=files),
                expected_state_version=0,
            ),
        )

    with pytest.raises(StateConflictError, match="STATE_VERSION_CONFLICT"):
        repository.persist_inventory(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistInventoryInput(
                mutation=InventoryMutation(
                    inventory_id=second_inventory,
                    remove_file_ids=(file.file_id,),
                ),
                expected_state_version=1,
            ),
        )
    reassigned = file.model_copy(update={"workspace_id": second_inventory})
    with pytest.raises(StateConflictError, match="STATE_VERSION_CONFLICT"):
        repository.persist_inventory(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistInventoryInput(
                mutation=InventoryMutation(
                    inventory_id=second_inventory,
                    files=(reassigned,),
                ),
                expected_state_version=1,
            ),
        )

    with state.session_factory() as session:
        row = session.get(FileAssetEntity, str(file.file_id))
        inventory = session.get(InventoryEntity, str(second_inventory))
        assert row is not None
        assert row.inventory_id == str(first_inventory)
        assert inventory is not None
        assert inventory.state_version == 1


def test_tool_022_rolls_back_the_domain_write_and_audit_together(
    state: StateDatabase,
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    request_id = uuid4()
    request = PersistInventoryInput(
        mutation=InventoryMutation(
            inventory_id=inventory_id,
            files=(
                _file(inventory_id, path="duplicate.las"),
                _file(inventory_id, path="duplicate.las"),
            ),
        ),
        expected_state_version=0,
    )

    with pytest.raises(StateConflictError, match="DB_WRITE_FAILED"):
        repository.persist_inventory(request_id=request_id, actor=ACTOR, request=request)

    with state.session_factory() as session:
        assert session.scalar(select(FileAssetEntity)) is None
        assert (
            session.scalar(
                select(AuditEventEntity).where(AuditEventEntity.request_id == str(request_id))
            )
            is None
        )


def test_tool_023_associations_preserve_review_history_and_reject_stale_version(
    state: StateDatabase,
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.9,
        method="exact_filename",
        evidence_ids=(),
        review_status=ReviewStatus.PROPOSED,
        trust_level=TrustLevel.HEURISTIC,
        target_version="v1",
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

    result = repository.persist_associations(request_id=uuid4(), actor=ACTOR, request=request)

    assert result.state_version == 1
    replay = repository.persist_associations(request_id=uuid4(), actor=ACTOR, request=request)
    assert replay.state_version == 1
    changed = request.model_copy(
        update={
            "mutations": (
                AssociationMutation(
                    association=association.model_copy(update={"score": 0.8}),
                    review_status=ReviewStatus.PROPOSED,
                ),
            )
        }
    )
    with pytest.raises(StateConflictError, match="ASSOCIATION_CONFLICT"):
        repository.persist_associations(request_id=uuid4(), actor=ACTOR, request=changed)


def test_tool_023_update_refreshes_projection_and_current_review_version(
    state: StateDatabase, tmp_path: Path
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.5,
        method="normalized_identifier",
        evidence_ids=(uuid4(),),
        target_version="content-v1",
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
    updated = association.model_copy(
        update={
            "score": 0.95,
            "method": "exact_filename",
            "evidence_ids": (uuid4(), uuid4()),
            "target_version": "content-v2",
        }
    )
    repository.persist_associations(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistAssociationsInput(
            mutations=(
                AssociationMutation(
                    association=updated,
                    review_status=ReviewStatus.PROPOSED,
                ),
            ),
            expected_state_version=1,
        ),
    )

    output = tmp_path / "output"
    source = tmp_path / "source"
    output.mkdir()
    source.mkdir()
    review = ReviewService(
        repository,
        WorkspaceAccessPolicy(
            workspace_id=inventory_id,
            source_root=str(source),
            output_roots={"reports": str(output)},
            path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        ),
    )
    item = review.build_inventory_review(
        BuildInventoryReviewInput(inventory_id=inventory_id)
    ).items[0]
    assert item.association == updated
    with pytest.raises(StateConflictError, match="STALE_REVIEW_TARGET"):
        repository.record_review_decision(
            request_id=uuid4(),
            decision=ReviewDecision(
                decision_id=uuid4(),
                actor=ACTOR,
                target_type=ReviewTargetType.MANIFEST_ASSOCIATION,
                target_id=association.association_id,
                target_version="content-v1",
                decision=ReviewDecisionValue.APPROVE,
                reason="The old content must no longer be reviewable.",
                decided_at=NOW,
            ),
        )


def test_tool_024_versions_models_and_allows_only_one_active_model_per_category(
    state: StateDatabase,
) -> None:
    repository = StateRepository(state.session_factory)
    model_id = uuid4()
    document = ManifestJsonDocument(sha256=ZERO_HASH, content={})
    model = LearningModelContract(
        learning_model_id=model_id,
        category=DataCategory.WELL_LOG,
        version=1,
        model_sha256=ZERO_HASH,
        example_ids=(),
        example_identities=(),
        prototype=document,
        constants=(),
        prototype_source_path=WorkspaceRelativePath("manifest.json"),
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )
    created = repository.persist_learning_model(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistLearningModelInput(
            mutation=LearningModelMutation(
                action=LearningMutationAction.CREATE,
                model=model,
                expected_version=0,
            )
        ),
    )
    active = repository.persist_learning_model(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistLearningModelInput(
            mutation=LearningModelMutation(
                action=LearningMutationAction.ACTIVATE,
                learning_model_id=model_id,
                expected_version=created.version,
            )
        ),
    )

    assert created.status == LearningModelStatus.DRAFT
    assert active.status == LearningModelStatus.ACTIVE
    assert active.version == 2
    cleared = repository.persist_learning_model(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistLearningModelInput(
            mutation=LearningModelMutation(action=LearningMutationAction.CLEAR)
        ),
    )
    assert cleared.status == LearningModelStatus.INACTIVE
    assert repository.list_learning_model_versions(model_id) == (1, 2, 3)
    with state.session_factory() as session:
        latest = session.scalar(
            select(LearningModelVersionEntity)
            .where(LearningModelVersionEntity.learning_model_id == str(model_id))
            .order_by(LearningModelVersionEntity.version.desc())
        )
        assert latest is not None
        assert latest.status == LearningModelStatus.INACTIVE.value


def _job_input(key: str, *, concurrency: int = 2) -> TrackJobInput:
    return TrackJobInput(
        definition=JobDefinition(
            job_type="acceptance",
            steps=tuple(
                JobStepDefinition(sequence=index, tool_id="TOOL-003", input_ref=f"item-{index}")
                for index in range(1, 5)
            ),
            max_concurrency=concurrency,
            continue_on_error=False,
        ),
        deduplication_key=key,
    )


def test_tool_025_enforces_fsm_deduplication_leases_and_bounded_workers(
    state: StateDatabase,
) -> None:
    service = JobService(state.session_factory, worker_limit=4)
    created = service.create_job(request_id=uuid4(), actor=ACTOR, request=_job_input("same"))
    replay = service.create_job(request_id=uuid4(), actor=ACTOR, request=_job_input("same"))
    assert replay.descriptor.job.job_id == created.descriptor.job.job_id
    assert service.acquire_lease(created.descriptor.job.job_id, "worker-a", lease_seconds=10)
    assert not service.acquire_lease(created.descriptor.job.job_id, "worker-b", lease_seconds=10)

    active = 0
    peak = 0
    lock = threading.Lock()

    def execute(_step: JobStepDefinition, context: JobExecutionContext) -> None:
        nonlocal active, peak
        context.checkpoint()
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    snapshot = service.execute(created.descriptor.job.job_id, execute)

    assert snapshot.job.status is JobStatus.SUCCEEDED
    assert peak == 2
    events = service.query(
        QueryOrCancelJobInput(
            action=JobControlAction.QUERY,
            job_id=snapshot.job.job_id,
        )
    ).events
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type is JobEventType.CREATED
    assert events[-1].event_type is JobEventType.SUCCEEDED
    with pytest.raises(JobError, match="INVALID_JOB_TRANSITION"):
        service.transition(snapshot.job.job_id, JobStatus.RUNNING)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.QUEUED, JobStatus.RUNNING),
        (JobStatus.QUEUED, JobStatus.CANCELLED),
        (JobStatus.RUNNING, JobStatus.CANCELLING),
        (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.PARTIALLY_SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.CANCELLING, JobStatus.CANCELLED),
        (JobStatus.INTERRUPTED, JobStatus.QUEUED),
        (JobStatus.INTERRUPTED, JobStatus.CANCELLED),
    ],
)
def test_tool_025_allows_every_documented_transition(
    state: StateDatabase, start: JobStatus, target: JobStatus
) -> None:
    service = JobService(state.session_factory)
    descriptor = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input(str(uuid4()))
    ).descriptor
    if start in {JobStatus.RUNNING, JobStatus.CANCELLING}:
        service.transition(descriptor.job.job_id, JobStatus.RUNNING)
    if start is JobStatus.CANCELLING:
        service.transition(descriptor.job.job_id, JobStatus.CANCELLING)
    if start is JobStatus.INTERRUPTED:
        with state.session_factory.begin() as session:
            row = session.get(JobEntity, str(descriptor.job.job_id))
            assert row is not None
            row.status = JobStatus.INTERRUPTED.value

    snapshot = service.transition(descriptor.job.job_id, target)

    assert snapshot.job.status is target


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.QUEUED, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.CANCELLED),
        (JobStatus.CANCELLING, JobStatus.FAILED),
        (JobStatus.SUCCEEDED, JobStatus.QUEUED),
        (JobStatus.PARTIALLY_SUCCEEDED, JobStatus.QUEUED),
        (JobStatus.CANCELLED, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.QUEUED),
    ],
)
def test_tool_025_rejects_every_prohibited_transition_family(
    state: StateDatabase, start: JobStatus, target: JobStatus
) -> None:
    service = JobService(state.session_factory)
    descriptor = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input(str(uuid4()))
    ).descriptor
    with state.session_factory.begin() as session:
        row = session.get(JobEntity, str(descriptor.job.job_id))
        assert row is not None
        row.status = start.value

    with pytest.raises(JobError, match="INVALID_JOB_TRANSITION"):
        service.transition(descriptor.job.job_id, target)


@pytest.mark.parametrize(
    ("continue_on_error", "terminal"),
    [(False, JobStatus.FAILED), (True, JobStatus.PARTIALLY_SUCCEEDED)],
)
def test_tool_025_maps_step_failures_to_valid_terminal_states(
    state: StateDatabase, continue_on_error: bool, terminal: JobStatus
) -> None:
    service = JobService(state.session_factory, worker_limit=1)
    request = _job_input(str(uuid4()), concurrency=1)
    request = request.model_copy(
        update={
            "definition": request.definition.model_copy(
                update={"continue_on_error": continue_on_error}
            )
        }
    )
    descriptor = service.create_job(request_id=uuid4(), actor=ACTOR, request=request).descriptor

    def fail_first(step: JobStepDefinition, _context: JobExecutionContext) -> None:
        if step.sequence == 1:
            raise ValueError("bounded test failure")

    snapshot = service.execute(descriptor.job.job_id, fail_first)

    assert snapshot.job.status is terminal


def test_tool_026_paginates_sse_cancels_cooperatively_and_recovers_interruption(
    state: StateDatabase,
) -> None:
    service = JobService(state.session_factory, worker_limit=1)
    descriptor = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("cancel", concurrency=1)
    ).descriptor
    receipt = service.query(
        QueryOrCancelJobInput(
            action=JobControlAction.CANCEL,
            job_id=descriptor.job.job_id,
        ),
        actor=ACTOR,
    )
    assert receipt.cancellation is not None
    assert receipt.cancellation.accepted
    cancelled = service.execute(
        descriptor.job.job_id,
        lambda _step, context: context.checkpoint(),
    )
    assert cancelled.job.status is JobStatus.CANCELLED

    page = service.query(
        QueryOrCancelJobInput(
            action=JobControlAction.QUERY,
            job_id=descriptor.job.job_id,
            after_event_sequence=1,
        ),
        event_limit=2,
    )
    assert len(page.events) == 2
    assert all(event.sequence > 1 for event in page.events)
    assert "event: job." in "".join(service.sse_events(descriptor.job.job_id))

    interrupted = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("interrupted")
    ).descriptor
    service.transition(interrupted.job.job_id, JobStatus.RUNNING)
    assert service.recover_interrupted() == 1
    with state.session_factory() as session:
        row = session.get(JobEntity, str(interrupted.job.job_id))
        assert row is not None
        assert row.status == JobStatus.INTERRUPTED.value


def test_tool_025_recovery_skips_active_leases_and_recovers_expired_leases(
    state: StateDatabase,
) -> None:
    service = JobService(state.session_factory)
    active = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("active-lease")
    ).descriptor
    active_cancelling = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("active-cancelling-lease")
    ).descriptor
    expired = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("expired-lease")
    ).descriptor
    unleased = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input("unleased")
    ).descriptor
    for descriptor in (active, active_cancelling, expired, unleased):
        service.transition(descriptor.job.job_id, JobStatus.RUNNING)
    assert service.acquire_lease(active.job.job_id, "worker-active", lease_seconds=60)
    assert service.acquire_lease(
        active_cancelling.job.job_id, "worker-cancelling", lease_seconds=60
    )
    service.transition(active_cancelling.job.job_id, JobStatus.CANCELLING)
    assert service.acquire_lease(expired.job.job_id, "worker-expired", lease_seconds=60)
    with state.session_factory.begin() as session:
        row = session.get(JobEntity, str(expired.job.job_id))
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert service.recover_interrupted() == 2

    with state.session_factory() as session:
        active_row = session.get(JobEntity, str(active.job.job_id))
        active_cancelling_row = session.get(JobEntity, str(active_cancelling.job.job_id))
        expired_row = session.get(JobEntity, str(expired.job.job_id))
        unleased_row = session.get(JobEntity, str(unleased.job.job_id))
        assert active_row is not None
        assert active_row.status == JobStatus.RUNNING.value
        assert active_row.lease_owner == "worker-active"
        assert active_cancelling_row is not None
        assert active_cancelling_row.status == JobStatus.CANCELLING.value
        assert active_cancelling_row.lease_owner == "worker-cancelling"
        assert expired_row is not None
        assert expired_row.status == JobStatus.INTERRUPTED.value
        assert expired_row.lease_owner is None
        assert unleased_row is not None
        assert unleased_row.status == JobStatus.INTERRUPTED.value


def test_tool_026_cancels_an_active_worker_at_its_checkpoint(
    state: StateDatabase,
) -> None:
    service = JobService(state.session_factory, worker_limit=1)
    descriptor = service.create_job(
        request_id=uuid4(), actor=ACTOR, request=_job_input(str(uuid4()), concurrency=1)
    ).descriptor
    started = threading.Event()
    release = threading.Event()
    result: list[JobStatus] = []

    def work(_step: JobStepDefinition, context: JobExecutionContext) -> None:
        started.set()
        release.wait(timeout=2)
        context.checkpoint()

    thread = threading.Thread(
        target=lambda: result.append(service.execute(descriptor.job.job_id, work).job.status)
    )
    thread.start()
    assert started.wait(timeout=2)
    service.query(
        QueryOrCancelJobInput(
            action=JobControlAction.CANCEL,
            job_id=descriptor.job.job_id,
        ),
        actor=ACTOR,
    )
    release.set()
    thread.join(timeout=3)

    assert result == [JobStatus.CANCELLED]


def test_tools_027_to_030_project_decide_and_export_only_approved_candidates(
    state: StateDatabase, tmp_path: Path
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id, path="secret-segment/a.las")
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    candidate = _candidate(file.file_id)
    provenance = ProvenanceRecord(
        provenance_id=uuid4(),
        source_type="generated_candidate",
        source_ref="data/a.las",
        tool_id="TOOL-018",
        tool_version="1.0.0",
        recorded_at=NOW,
        source_sha256=ZERO_HASH,
    )
    source_payload: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {"name": "before", "removed": True},
    }
    repository.persist_generated_candidate(
        candidate,
        provenance=(provenance,),
        source_content=ManifestJsonDocument(
            sha256="2" * 64,
            content=source_payload,
        ),
    )
    output = tmp_path / "exports"
    output.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    policy = WorkspaceAccessPolicy(
        workspace_id=inventory_id,
        source_root=str(source),
        output_roots={"approved": str(output)},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )
    review = ReviewService(repository, policy)

    inventory = review.build_inventory_review(
        BuildInventoryReviewInput(
            inventory_id=inventory_id,
            query=ReviewQuery(search="a.las", limit=10),
        )
    )
    detail = review.build_manifest_review(BuildManifestReviewInput(manifest=candidate))
    assert inventory.total_count == 1
    assert inventory.items[0].file.relative_path.root == "secret-segment/a.las"
    assert detail.content.sha256 == candidate.document.sha256
    assert isinstance(detail.manifest, GeneratedManifestCandidate)
    assert detail.manifest.reference.trust_level is TrustLevel.HEURISTIC
    assert detail.provenance == (provenance,)
    assert detail.generation_diff is not None
    assert detail.generation_diff.changed_pointers == ("/Data/name",)
    assert detail.generation_diff.added_pointers == ()
    assert detail.generation_diff.removed_pointers == ("/Data/removed",)

    export = ExportToolInput(
        request=ExportRequest(
            export_kind=ExportKind.APPROVED_MANIFEST,
            target_id=candidate.reference.candidate_id,
            output_root_id="approved",
            relative_path=WorkspaceRelativePath("candidate.json"),
            expected_target_version=candidate.reference.candidate_sha256,
        )
    )
    with pytest.raises(ReviewError, match="UNAPPROVED_EXPORT"):
        review.export(export)

    decision = ReviewDecision(
        decision_id=uuid4(),
        actor=ACTOR,
        target_type=ReviewTargetType.GENERATED_CANDIDATE,
        target_id=candidate.reference.candidate_id,
        target_version=candidate.reference.candidate_sha256,
        decision=ReviewDecisionValue.APPROVE,
        reason="Validated by the local reviewer.",
        decided_at=NOW,
    )
    first = review.record_decision(
        request_id=uuid4(), request=RecordReviewDecisionInput(decision=decision)
    )
    replay = review.record_decision(
        request_id=uuid4(), request=RecordReviewDecisionInput(decision=decision)
    )
    assert first == replay
    assert first.receipt.review_status is ReviewStatus.APPROVED

    receipt = review.export(export)
    assert len(receipt.receipt.files) == 1
    assert (output / "candidate.json").read_text(encoding="utf-8").endswith("\n")
    assert review.export(export).receipt.export_id == receipt.receipt.export_id
    (output / "occupied.json").write_text("different", encoding="utf-8")
    with pytest.raises(ReviewError, match="OUTPUT_EXISTS"):
        review.export(
            ExportToolInput(
                request=export.request.model_copy(
                    update={"relative_path": WorkspaceRelativePath("occupied.json")}
                )
            )
        )
    with state.session_factory() as session:
        decisions = session.scalars(select(ReviewDecisionEntity)).all()
        assert len(decisions) == 1


def test_tool_028_rejects_spoofed_or_stale_generated_candidate(
    state: StateDatabase, tmp_path: Path
) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    candidate = _candidate(file.file_id)
    repository.persist_generated_candidate(candidate)
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    review = ReviewService(
        repository,
        WorkspaceAccessPolicy(
            workspace_id=inventory_id,
            source_root=str(source),
            output_roots={"reports": str(output)},
            path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        ),
    )

    spoofed = candidate.model_copy(
        update={
            "document": candidate.document.model_copy(update={"content": {"caller": "controlled"}})
        }
    )
    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        review.build_manifest_review(BuildManifestReviewInput(manifest=spoofed))

    stale_content: dict[str, JsonValue] = {"kind": "stale"}
    stale_encoded = (
        json.dumps(stale_content, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    stale_hash = sha256(stale_encoded).hexdigest()
    stale = candidate.model_copy(
        update={
            "reference": candidate.reference.model_copy(update={"candidate_sha256": stale_hash}),
            "document": ManifestJsonDocument(sha256=stale_hash, content=stale_content),
        }
    )
    with pytest.raises(ReviewError, match="STALE_REVIEW_TARGET"):
        review.build_manifest_review(BuildManifestReviewInput(manifest=stale))


def test_tool_022_cancels_before_commit(state: StateDatabase) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    with pytest.raises(StateConflictError, match="CANCELLED"):
        repository.persist_inventory(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
                expected_state_version=0,
            ),
            cancellation=lambda: True,
        )
    with state.session_factory() as session:
        assert session.get(InventoryEntity, str(inventory_id)) is None


def test_tool_023_cancels_before_commit(state: StateDatabase) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=file.file_id,
        manifest_id=uuid4(),
        score=0.8,
        method="exact_filename",
        evidence_ids=(),
        target_version="content",
    )
    with pytest.raises(StateConflictError, match="CANCELLED"):
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
            cancellation=lambda: True,
        )
    with state.session_factory() as session:
        assert session.get(ManifestAssociationEntity, str(association.association_id)) is None
        assert session.get(StateCounterEntity, "associations") is None


def test_tool_024_cancels_before_commit(state: StateDatabase) -> None:
    repository = StateRepository(state.session_factory)
    model = LearningModelContract(
        learning_model_id=uuid4(),
        category=DataCategory.WELL_LOG,
        version=1,
        model_sha256=ZERO_HASH,
        example_ids=(),
        example_identities=(),
        prototype=ManifestJsonDocument(sha256=ZERO_HASH, content={}),
        constants=(),
        prototype_source_path=WorkspaceRelativePath("manifest.json"),
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )
    with pytest.raises(StateConflictError, match="CANCELLED"):
        repository.persist_learning_model(
            request_id=uuid4(),
            actor=ACTOR,
            request=PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.CREATE,
                    model=model,
                    expected_version=0,
                )
            ),
            cancellation=lambda: True,
        )
    with state.session_factory() as session:
        assert session.scalar(select(LearningModelVersionEntity)) is None
        assert session.scalar(select(LearningExampleEntity)) is None


def test_tool_029_cancels_before_commit(state: StateDatabase, tmp_path: Path) -> None:
    repository = StateRepository(state.session_factory)
    inventory_id = uuid4()
    file = _file(inventory_id)
    repository.persist_inventory(
        request_id=uuid4(),
        actor=ACTOR,
        request=PersistInventoryInput(
            mutation=InventoryMutation(inventory_id=inventory_id, files=(file,)),
            expected_state_version=0,
        ),
    )
    candidate = _candidate(file.file_id)
    repository.persist_generated_candidate(candidate)
    source = tmp_path / "cancel-source"
    output = tmp_path / "cancel-output"
    source.mkdir()
    output.mkdir()
    review = ReviewService(
        repository,
        WorkspaceAccessPolicy(
            workspace_id=inventory_id,
            source_root=str(source),
            output_roots={"reports": str(output)},
            path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        ),
    )
    decision = ReviewDecision(
        decision_id=uuid4(),
        actor=ACTOR,
        target_type=ReviewTargetType.GENERATED_CANDIDATE,
        target_id=candidate.reference.candidate_id,
        target_version=candidate.reference.candidate_sha256,
        decision=ReviewDecisionValue.APPROVE,
        reason="Cancellation must win before commit.",
        decided_at=NOW,
    )
    with pytest.raises(ReviewError, match="CANCELLED"):
        review.record_decision(
            request_id=uuid4(),
            request=RecordReviewDecisionInput(decision=decision),
            cancellation=lambda: True,
        )
    with state.session_factory() as session:
        assert session.get(ReviewDecisionEntity, str(decision.decision_id)) is None
        row = session.get(GeneratedCandidateEntity, str(candidate.reference.candidate_id))
        assert row is not None
        assert row.review_status == ReviewStatus.PROPOSED.value
