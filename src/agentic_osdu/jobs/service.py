"""TOOL-025/026 persisted jobs, bounded workers, leases, events, and cancellation."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentic_osdu.domain.models import (
    ActorRef,
    JobEventRef,
    JobEventType,
    JobRef,
    JobStatus,
)
from agentic_osdu.state.models import AuditEventEntity, JobEntity, JobEventEntity
from agentic_osdu.tools.contracts import (
    CancellationReceipt,
    JobControlAction,
    JobControlOutput,
    JobDefinition,
    JobDescriptor,
    JobSnapshot,
    JobStepDefinition,
    QueryOrCancelJobInput,
    TrackJobInput,
    TrackJobOutput,
)

StepExecutor = Callable[[JobStepDefinition, "JobExecutionContext"], None]
_EXECUTION_LEASE_SECONDS = 30
_EXECUTION_LEASE_RENEWAL_SECONDS = 10

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.CANCELLING,
            JobStatus.SUCCEEDED,
            JobStatus.PARTIALLY_SUCCEEDED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
    JobStatus.INTERRUPTED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.PARTIALLY_SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.FAILED: frozenset(),
}
_EVENT_FOR_STATUS = {
    JobStatus.RUNNING: JobEventType.STARTED,
    JobStatus.SUCCEEDED: JobEventType.SUCCEEDED,
    JobStatus.PARTIALLY_SUCCEEDED: JobEventType.PARTIALLY_SUCCEEDED,
    JobStatus.CANCELLED: JobEventType.CANCELLED,
    JobStatus.FAILED: JobEventType.FAILED,
    JobStatus.INTERRUPTED: JobEventType.INTERRUPTED,
}


class JobError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class JobCancelled(JobError):
    def __init__(self) -> None:
        super().__init__("CANCELLED", "The job cancellation checkpoint was reached.")


@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    job_id: UUID
    _cancelled: Callable[[], bool]
    _progress: Callable[[dict[str, int], str | None], None]

    def checkpoint(self) -> None:
        if self._cancelled():
            raise JobCancelled()

    def progress(self, counts: dict[str, int], current_item: str | None = None) -> None:
        self.checkpoint()
        self._progress(counts, current_item)


class JobService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_limit: int = 4,
    ) -> None:
        if not 1 <= worker_limit <= 64:
            raise ValueError("worker_limit must be between 1 and 64")
        self._sessions = session_factory
        self._worker_limit = worker_limit
        self._write_lock = threading.RLock()

    def create_job(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        request: TrackJobInput,
    ) -> TrackJobOutput:
        with self._write_lock, self._sessions.begin() as session:
            existing = session.scalar(
                select(JobEntity).where(JobEntity.deduplication_key == request.deduplication_key)
            )
            if existing is not None:
                if existing.definition != request.definition.model_dump(mode="json"):
                    raise JobError(
                        "JOB_ALREADY_RUNNING",
                        "The deduplication key belongs to a different job definition.",
                    )
                return TrackJobOutput(descriptor=self._descriptor(existing))
            sequences = [step.sequence for step in request.definition.steps]
            if sequences != list(range(1, len(sequences) + 1)):
                raise JobError(
                    "JOB_DEFINITION_INVALID",
                    "Job step sequences must be contiguous and ordered from one.",
                )
            now = datetime.now(UTC)
            row = JobEntity(
                id=str(uuid4()),
                job_type=request.definition.job_type,
                deduplication_key=request.deduplication_key,
                definition=request.definition.model_dump(mode="json"),
                status=JobStatus.QUEUED.value,
                stage=None,
                counts={},
                cancellation_requested=False,
                state_version=1,
                lease_owner=None,
                lease_expires_at=None,
                error_summary=None,
                result_summary=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            self._append_event(session, row, JobEventType.CREATED)
            self._audit(session, request_id, actor, "TOOL-025", row.id)
            return TrackJobOutput(descriptor=self._descriptor(row))

    def acquire_lease(
        self,
        job_id: UUID,
        owner: str,
        *,
        lease_seconds: int,
    ) -> bool:
        if not owner or lease_seconds < 1:
            raise ValueError("lease owner and a positive duration are required")
        lease_actor = ActorRef(actor_id=owner)
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            now = datetime.now(UTC)
            expires = row.lease_expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if (
                row.lease_owner is not None
                and row.lease_owner != owner
                and expires is not None
                and expires > now
            ):
                return False
            row.lease_owner = owner
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.state_version += 1
            row.updated_at = now
            self._audit(
                session,
                uuid4(),
                lease_actor,
                "TOOL-025",
                row.id,
            )
            return True

    def transition(
        self,
        job_id: UUID,
        target: JobStatus,
        *,
        stage: str | None = None,
        safe_payload: dict[str, JsonValue] | None = None,
        lease_owner: str | None = None,
    ) -> JobSnapshot:
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            if lease_owner is not None and row.lease_owner != lease_owner:
                raise JobError("JOB_LEASE_LOST", "The execution lease is no longer owned.")
            current = JobStatus(row.status)
            if target not in _ALLOWED[current]:
                raise JobError(
                    "INVALID_JOB_TRANSITION",
                    f"Transition from {current.value} to {target.value} is not allowed.",
                )
            row.status = target.value
            row.stage = stage if stage is not None else row.stage
            if target in {
                JobStatus.SUCCEEDED,
                JobStatus.PARTIALLY_SUCCEEDED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                row.lease_owner = None
                row.lease_expires_at = None
            row.state_version += 1
            row.updated_at = datetime.now(UTC)
            event_type = _EVENT_FOR_STATUS.get(target)
            if event_type is not None:
                self._append_event(
                    session,
                    row,
                    event_type,
                    safe_payload=safe_payload,
                )
            self._audit(
                session,
                uuid4(),
                ActorRef(actor_id="job-supervisor"),
                "TOOL-025",
                row.id,
            )
            return self._snapshot(session, row)

    def execute(self, job_id: UUID, executor: StepExecutor) -> JobSnapshot:
        with self._sessions() as session:
            initial = self._job(session, job_id)
            if initial.status == JobStatus.CANCELLED.value:
                return self._snapshot(session, initial)
        definition, lease_owner = self._start_execution(job_id)
        lease_stop = threading.Event()
        lease_lost = threading.Event()

        def maintain_lease() -> None:
            while not lease_stop.wait(_EXECUTION_LEASE_RENEWAL_SECONDS):
                if not self._renew_execution_lease(job_id, lease_owner):
                    lease_lost.set()
                    return

        lease_thread = threading.Thread(
            target=maintain_lease,
            name=f"agentic-osdu-lease-{job_id}",
            daemon=True,
        )
        lease_thread.start()
        failures = 0
        cancelled = False
        stop_after_failure = threading.Event()
        max_workers = min(self._worker_limit, definition.max_concurrency)
        context = JobExecutionContext(
            job_id=job_id,
            _cancelled=lambda: self._is_cancelled(job_id),
            _progress=lambda counts, item: self._record_progress(job_id, counts, item),
        )

        def run_step(step: JobStepDefinition) -> None:
            if stop_after_failure.is_set():
                return
            context.checkpoint()
            self._record_event(
                job_id,
                JobEventType.ITEM_STARTED,
                current_item=step.input_ref,
                safe_payload={"tool_id": step.tool_id, "step": step.sequence},
            )
            executor(step, context)
            context.checkpoint()
            self._increment_event(
                job_id,
                JobEventType.ITEM_SUCCEEDED,
                "succeeded",
                current_item=step.input_ref,
                safe_payload={"tool_id": step.tool_id, "step": step.sequence},
            )

        try:
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="agentic-osdu"
            ) as pool:
                futures = {pool.submit(run_step, step): step for step in definition.steps}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except JobCancelled:
                        cancelled = True
                    except Exception as error:
                        failures += 1
                        self._increment_event(
                            job_id,
                            JobEventType.ITEM_FAILED,
                            "failed",
                            current_item=futures[future].input_ref,
                            safe_payload={"error_type": type(error).__name__},
                        )
                        if not definition.continue_on_error:
                            stop_after_failure.set()
        finally:
            lease_stop.set()
            lease_thread.join()

        if lease_lost.is_set():
            raise JobError("JOB_LEASE_LOST", "The execution lease could not be maintained.")
        if cancelled or self._is_cancelled(job_id):
            with self._sessions() as session:
                row = self._job(session, job_id)
                status = JobStatus(row.status)
            if status is JobStatus.RUNNING:
                self.transition(
                    job_id,
                    JobStatus.CANCELLING,
                    lease_owner=lease_owner,
                )
            return self.transition(
                job_id,
                JobStatus.CANCELLED,
                lease_owner=lease_owner,
            )
        if failures:
            terminal = (
                JobStatus.PARTIALLY_SUCCEEDED if definition.continue_on_error else JobStatus.FAILED
            )
            return self.transition(job_id, terminal, lease_owner=lease_owner)
        return self.transition(job_id, JobStatus.SUCCEEDED, lease_owner=lease_owner)

    def _start_execution(self, job_id: UUID) -> tuple[JobDefinition, str]:
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            if JobStatus(row.status) is not JobStatus.QUEUED:
                raise JobError(
                    "INVALID_JOB_TRANSITION",
                    f"Transition from {row.status} to running is not allowed.",
                )
            now = datetime.now(UTC)
            current_expiry = row.lease_expires_at
            normalized_expiry = current_expiry
            if normalized_expiry is not None and normalized_expiry.tzinfo is None:
                normalized_expiry = normalized_expiry.replace(tzinfo=UTC)
            owner = (
                row.lease_owner
                if row.lease_owner is not None
                and normalized_expiry is not None
                and normalized_expiry > now
                else f"job-executor-{uuid4()}"
            )
            ownership = (
                JobEntity.lease_owner.is_(None)
                if row.lease_owner is None
                else JobEntity.lease_owner == row.lease_owner
            )
            expiry = (
                JobEntity.lease_expires_at.is_(None)
                if current_expiry is None
                else JobEntity.lease_expires_at == current_expiry
            )
            changed = session.connection().execute(
                update(JobEntity)
                .where(
                    JobEntity.id == row.id,
                    JobEntity.status == JobStatus.QUEUED.value,
                    JobEntity.state_version == row.state_version,
                    ownership,
                    expiry,
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    lease_owner=owner,
                    lease_expires_at=now + timedelta(seconds=_EXECUTION_LEASE_SECONDS),
                    state_version=row.state_version + 1,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise JobError("JOB_LEASE_UNAVAILABLE", "The execution lease was not acquired.")
            session.refresh(row)
            self._append_event(session, row, JobEventType.STARTED)
            self._audit(
                session,
                uuid4(),
                ActorRef(actor_id="job-supervisor"),
                "TOOL-025",
                row.id,
            )
            definition = JobDefinition.model_validate_json(json.dumps(row.definition))
            return definition, owner

    def _renew_execution_lease(self, job_id: UUID, owner: str) -> bool:
        with self._write_lock, self._sessions.begin() as session:
            now = datetime.now(UTC)
            changed = session.connection().execute(
                update(JobEntity)
                .where(
                    JobEntity.id == str(job_id),
                    JobEntity.status.in_([JobStatus.RUNNING.value, JobStatus.CANCELLING.value]),
                    JobEntity.lease_owner == owner,
                    JobEntity.lease_expires_at.is_not(None),
                    JobEntity.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=_EXECUTION_LEASE_SECONDS),
                    state_version=JobEntity.state_version + 1,
                    updated_at=now,
                )
            )
            return changed.rowcount == 1

    def query(
        self,
        request: QueryOrCancelJobInput,
        *,
        actor: ActorRef | None = None,
        event_limit: int = 1000,
    ) -> JobControlOutput:
        if not 1 <= event_limit <= 10_000:
            raise JobError("QUERY_INVALID", "Event limit is outside the supported range.")
        cancellation: CancellationReceipt | None = None
        if request.action is JobControlAction.CANCEL:
            cancellation = self._cancel(request.job_id, actor or ActorRef(actor_id="local-user"))
        with self._sessions() as session:
            row = self._job(session, request.job_id)
            after = request.after_event_sequence or 0
            events = session.scalars(
                select(JobEventEntity)
                .where(
                    JobEventEntity.job_id == str(request.job_id),
                    JobEventEntity.sequence > after,
                )
                .order_by(JobEventEntity.sequence)
                .limit(event_limit)
            ).all()
            return JobControlOutput(
                snapshot=self._snapshot(session, row),
                events=tuple(self._event_ref(item) for item in events),
                cancellation=cancellation,
            )

    def change_stage(self, job_id: UUID, stage: str) -> JobSnapshot:
        if not stage or len(stage) > 128:
            raise JobError("JOB_DEFINITION_INVALID", "The job stage is invalid.")
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            if JobStatus(row.status) is not JobStatus.RUNNING:
                raise JobError("INVALID_JOB_TRANSITION", "Only a running job may change stage.")
            row.stage = stage
            row.state_version += 1
            row.updated_at = datetime.now(UTC)
            self._append_event(
                session,
                row,
                JobEventType.STAGE_CHANGED,
                safe_payload={"stage": stage},
            )
            return self._snapshot(session, row)

    def sse_events(
        self,
        job_id: UUID,
        *,
        after_event_sequence: int = 0,
        limit: int = 1000,
    ) -> Iterator[str]:
        output = self.query(
            QueryOrCancelJobInput(
                action=JobControlAction.QUERY,
                job_id=job_id,
                after_event_sequence=after_event_sequence,
            ),
            event_limit=limit,
        )
        for event in output.events:
            yield (
                f"id: {event.sequence}\n"
                f"event: {event.event_type.value}\n"
                f"data: {event.model_dump_json()}\n\n"
            )

    def recover_interrupted(self) -> int:
        recovered = 0
        with self._write_lock, self._sessions.begin() as session:
            now = datetime.now(UTC)
            rows = session.scalars(
                select(JobEntity).where(
                    JobEntity.status.in_([JobStatus.RUNNING.value, JobStatus.CANCELLING.value]),
                    or_(
                        JobEntity.lease_owner.is_(None),
                        JobEntity.lease_expires_at.is_(None),
                        JobEntity.lease_expires_at <= now,
                    ),
                )
            ).all()
            for row in rows:
                ownership = (
                    JobEntity.lease_owner.is_(None)
                    if row.lease_owner is None
                    else JobEntity.lease_owner == row.lease_owner
                )
                expiry = (
                    JobEntity.lease_expires_at.is_(None)
                    if row.lease_expires_at is None
                    else JobEntity.lease_expires_at == row.lease_expires_at
                )
                changed = session.connection().execute(
                    update(JobEntity)
                    .where(
                        JobEntity.id == row.id,
                        JobEntity.status == row.status,
                        JobEntity.state_version == row.state_version,
                        ownership,
                        expiry,
                        or_(
                            JobEntity.lease_owner.is_(None),
                            JobEntity.lease_expires_at.is_(None),
                            JobEntity.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        status=JobStatus.INTERRUPTED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        state_version=row.state_version + 1,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    continue
                session.refresh(row)
                self._append_event(session, row, JobEventType.INTERRUPTED)
                self._audit(
                    session,
                    uuid4(),
                    ActorRef(actor_id="job-supervisor"),
                    "TOOL-025",
                    row.id,
                )
                recovered += 1
        return recovered

    def _cancel(self, job_id: UUID, actor: ActorRef) -> CancellationReceipt:
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            status = JobStatus(row.status)
            now = datetime.now(UTC)
            if status in {
                JobStatus.SUCCEEDED,
                JobStatus.PARTIALLY_SUCCEEDED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                event_time = session.scalar(
                    select(JobEventEntity.timestamp)
                    .where(
                        JobEventEntity.job_id == row.id,
                        JobEventEntity.event_type == JobEventType.CANCELLATION_REQUESTED.value,
                    )
                    .order_by(JobEventEntity.sequence.desc())
                    .limit(1)
                )
                if event_time is not None and event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=UTC)
                return CancellationReceipt(
                    job_id=job_id,
                    accepted=status is JobStatus.CANCELLED,
                    requested_at=event_time if status is JobStatus.CANCELLED else None,
                )
            if not row.cancellation_requested:
                row.cancellation_requested = True
                row.state_version += 1
                row.updated_at = now
                self._append_event(session, row, JobEventType.CANCELLATION_REQUESTED)
                self._audit(session, uuid4(), actor, "TOOL-026", row.id)
            if status is JobStatus.QUEUED:
                row.status = JobStatus.CANCELLED.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.state_version += 1
                self._append_event(session, row, JobEventType.CANCELLED)
            elif status is JobStatus.RUNNING:
                row.status = JobStatus.CANCELLING.value
                row.state_version += 1
            elif status is JobStatus.INTERRUPTED:
                row.status = JobStatus.CANCELLED.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.state_version += 1
                self._append_event(session, row, JobEventType.CANCELLED)
            return CancellationReceipt(job_id=job_id, accepted=True, requested_at=now)

    def _is_cancelled(self, job_id: UUID) -> bool:
        with self._sessions() as session:
            row = self._job(session, job_id)
            return row.cancellation_requested

    def _record_progress(
        self, job_id: UUID, counts: dict[str, int], current_item: str | None
    ) -> None:
        self._record_event(
            job_id,
            JobEventType.PROGRESSED,
            counts=counts,
            current_item=current_item,
        )

    def _record_event(
        self,
        job_id: UUID,
        event_type: JobEventType,
        *,
        counts: dict[str, int] | None = None,
        current_item: str | None = None,
        safe_payload: dict[str, JsonValue] | None = None,
    ) -> None:
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            self._append_event(
                session,
                row,
                event_type,
                counts=counts,
                current_item=current_item,
                safe_payload=safe_payload,
            )

    def _increment_event(
        self,
        job_id: UUID,
        event_type: JobEventType,
        counter: str,
        *,
        current_item: str | None = None,
        safe_payload: dict[str, JsonValue] | None = None,
    ) -> None:
        with self._write_lock, self._sessions.begin() as session:
            row = self._job(session, job_id)
            counts = dict(row.counts)
            counts[counter] = counts.get(counter, 0) + 1
            row.counts = counts
            self._append_event(
                session,
                row,
                event_type,
                counts=counts,
                current_item=current_item,
                safe_payload=safe_payload,
            )

    @staticmethod
    def _job(session: Session, job_id: UUID) -> JobEntity:
        row = session.get(JobEntity, str(job_id))
        if row is None:
            raise JobError("JOB_NOT_FOUND", "The requested job was not found.")
        return row

    @staticmethod
    def _append_event(
        session: Session,
        row: JobEntity,
        event_type: JobEventType,
        *,
        counts: dict[str, int] | None = None,
        current_item: str | None = None,
        safe_payload: dict[str, JsonValue] | None = None,
    ) -> None:
        sequence = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(JobEventEntity.sequence), 0)).where(
                        JobEventEntity.job_id == row.id
                    )
                )
                or 0
            )
            + 1
        )
        if counts is not None:
            row.counts = counts
        event = JobEventRef(
            job_id=UUID(row.id),
            sequence=sequence,
            event_type=event_type,
            timestamp=datetime.now(UTC),
            counts=counts or row.counts,
            current_item=current_item,
            safe_payload=safe_payload or {},
        )
        session.add(
            JobEventEntity(
                job_id=row.id,
                sequence=event.sequence,
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                counts=event.counts,
                current_item=event.current_item,
                safe_payload=event.safe_payload,
            )
        )
        session.flush()

    @staticmethod
    def _audit(
        session: Session,
        request_id: UUID,
        actor: ActorRef,
        tool_id: str,
        job_id: str,
    ) -> None:
        session.add(
            AuditEventEntity(
                id=str(uuid4()),
                request_id=str(request_id),
                actor=actor.model_dump(mode="json"),
                tool_id=tool_id,
                tool_version="1.0.0",
                result_status="succeeded",
                side_effects=[],
                payload={"job_id": job_id},
                occurred_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _descriptor(row: JobEntity) -> JobDescriptor:
        return JobDescriptor(
            job=JobService._job_ref(row),
            deduplication_key=row.deduplication_key,
            state_version=row.state_version,
        )

    @staticmethod
    def _job_ref(row: JobEntity) -> JobRef:
        created = row.created_at
        updated = row.updated_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return JobRef(
            job_id=UUID(row.id),
            job_type=row.job_type,
            status=JobStatus(row.status),
            stage=row.stage,
            cancellation_requested=row.cancellation_requested,
            created_at=created,
            updated_at=updated,
        )

    @staticmethod
    def _event_ref(row: JobEventEntity) -> JobEventRef:
        timestamp = row.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return JobEventRef(
            job_id=UUID(row.job_id),
            sequence=row.sequence,
            event_type=JobEventType(row.event_type),
            timestamp=timestamp,
            current_item=row.current_item,
            counts=row.counts,
            safe_payload=row.safe_payload,
        )

    @staticmethod
    def _snapshot(session: Session, row: JobEntity) -> JobSnapshot:
        sequence = int(
            session.scalar(
                select(func.coalesce(func.max(JobEventEntity.sequence), 0)).where(
                    JobEventEntity.job_id == row.id
                )
            )
            or 0
        )
        return JobSnapshot(
            job=JobService._job_ref(row),
            last_event_sequence=sequence,
            counts=row.counts,
        )


__all__ = ["JobError", "JobExecutionContext", "JobService"]
