"""Transactional repositories for TOOL-022 through TOOL-024 and review state."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentic_osdu.domain.models import (
    ActorRef,
    ApprovedAbsolutePath,
    ClassificationRecord,
    FileAssetRef,
    FormatDetectionResult,
    GeneratedManifestCandidate,
    LearningExampleRef,
    LearningModelStatus,
    ManifestDocumentRef,
    ManifestJsonDocument,
    OSDUKind,
    ProvenanceRecord,
    ReviewDecision,
    ReviewDecisionReceipt,
    ReviewDecisionValue,
    ReviewStatus,
    ReviewTargetType,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.state.models import (
    AuditEventEntity,
    ClassificationEntity,
    FileAssetEntity,
    FormatDetectionEntity,
    GeneratedCandidateEntity,
    IdempotencyRecordEntity,
    InventoryEntity,
    LearningExampleEntity,
    LearningModelVersionEntity,
    ManifestAssociationEntity,
    ManifestDocumentEntity,
    MetadataExtractionEntity,
    ReviewDecisionEntity,
    StateCounterEntity,
    WorkspaceEntity,
)
from agentic_osdu.tools.contracts import (
    AssociationMutation,
    AssociationSnapshotRef,
    ExtractedMetadataContract,
    FileRecordContract,
    InventoryMutation,
    InventorySnapshotRef,
    LearningModelContract,
    LearningModelVersionOutput,
    LearningMutationAction,
    PersistAssociationsInput,
    PersistInventoryInput,
    PersistLearningModelInput,
    ValidationReport,
    WorkspaceDescriptor,
)

T = TypeVar("T", bound=BaseModel)
CancellationCheck = Callable[[], bool]


class StateConflictError(RuntimeError):
    """Stable persistence failure suitable for a ToolError translation boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


def _now() -> datetime:
    return datetime.now(UTC)


def _json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _hash_request(model: BaseModel) -> str:
    encoded = json.dumps(_json(model), sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _audit(
    session: Session,
    *,
    request_id: UUID,
    actor: ActorRef,
    tool_id: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        AuditEventEntity(
            id=str(uuid4()),
            request_id=str(request_id),
            actor=_json(actor),
            tool_id=tool_id,
            tool_version="1.0.0",
            result_status="succeeded",
            side_effects=[],
            payload=payload,
            occurred_at=_now(),
        )
    )


class StateRepository:
    """Single transaction boundary for related state and its audit record."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def _replay(
        self,
        session: Session,
        request_id: UUID,
        tool_id: str,
        request: BaseModel,
        response_type: type[T],
    ) -> T | None:
        row = session.get(IdempotencyRecordEntity, str(request_id))
        if row is None:
            return None
        if row.tool_id != tool_id or row.request_hash != _hash_request(request):
            raise StateConflictError(
                "IDEMPOTENCY_KEY_REUSED",
                "The request ID was already used for different input.",
            )
        return response_type.model_validate_json(json.dumps(row.response))

    def _remember(
        self,
        session: Session,
        request_id: UUID,
        tool_id: str,
        request: BaseModel,
        response: BaseModel,
    ) -> None:
        session.add(
            IdempotencyRecordEntity(
                request_id=str(request_id),
                tool_id=tool_id,
                request_hash=_hash_request(request),
                response=_json(response),
                created_at=_now(),
            )
        )

    def persist_inventory(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        request: PersistInventoryInput,
        cancellation: CancellationCheck | None = None,
    ) -> InventorySnapshotRef:
        try:
            with self.session_factory.begin() as session:
                replay = self._replay(
                    session, request_id, "TOOL-022", request, InventorySnapshotRef
                )
                if replay is not None:
                    return replay
                inventory_id = str(request.mutation.inventory_id)
                inventory = session.get(InventoryEntity, inventory_id)
                current = 0 if inventory is None else inventory.state_version
                if current != request.expected_state_version:
                    raise StateConflictError(
                        "STATE_VERSION_CONFLICT",
                        "The inventory state version is stale.",
                        retryable=True,
                    )
                now = _now()
                archived_file_count: int | None = None
                archive_payload: dict[str, Any] | None = None
                if request.archive_before_reset:
                    if inventory is None:
                        raise StateConflictError(
                            "INVENTORY_NOT_FOUND", "The inventory was not found."
                        )
                    if any(
                        (
                            request.mutation.files,
                            request.mutation.detections,
                            request.mutation.extractions,
                            request.mutation.classifications,
                            request.mutation.remove_file_ids,
                        )
                    ):
                        raise StateConflictError(
                            "STATE_VERSION_CONFLICT",
                            "An archive/reset request cannot contain other inventory mutations.",
                        )
                    archive_payload = self._inventory_archive_snapshot(session, inventory_id)
                    archived_file_count = len(archive_payload["files"])
                if inventory is None:
                    inventory = InventoryEntity(
                        id=inventory_id,
                        state_version=0,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(inventory)
                    session.flush()
                else:
                    changed: CursorResult[Any] = session.connection().execute(
                        update(InventoryEntity)
                        .where(
                            InventoryEntity.id == inventory_id,
                            InventoryEntity.state_version == request.expected_state_version,
                        )
                        .values(state_version=current + 1, updated_at=now)
                    )
                    if changed.rowcount != 1:
                        raise StateConflictError(
                            "STATE_VERSION_CONFLICT",
                            "The inventory state version is stale.",
                            retryable=True,
                        )
                remove_file_ids = request.mutation.remove_file_ids
                if request.archive_before_reset:
                    remove_file_ids = tuple(
                        UUID(value)
                        for value in session.scalars(
                            select(FileAssetEntity.id).where(
                                FileAssetEntity.inventory_id == inventory_id
                            )
                        ).all()
                    )
                    session.execute(
                        delete(ManifestAssociationEntity).where(
                            ManifestAssociationEntity.file_id.in_(
                                tuple(str(value) for value in remove_file_ids)
                            )
                        )
                    )
                for file_id in remove_file_ids:
                    row = session.get(FileAssetEntity, str(file_id))
                    if row is not None and row.inventory_id != inventory_id:
                        raise StateConflictError(
                            "STATE_VERSION_CONFLICT",
                            "A file owned by another inventory cannot be removed.",
                        )
                    session.execute(
                        delete(FileAssetEntity).where(
                            FileAssetEntity.id == str(file_id),
                            FileAssetEntity.inventory_id == inventory_id,
                        )
                    )
                for file in request.mutation.files:
                    row = session.get(FileAssetEntity, str(file.file_id))
                    if row is not None and row.inventory_id != inventory_id:
                        raise StateConflictError(
                            "STATE_VERSION_CONFLICT",
                            "A file owned by another inventory cannot be reassigned.",
                        )
                    values = {
                        "inventory_id": inventory_id,
                        "workspace_id": str(file.workspace_id),
                        "relative_path": file.relative_path.root,
                        "size_bytes": file.size_bytes,
                        "modified_at": file.modified_at,
                        "sha256": file.sha256,
                        "discovery_version": file.discovery_version,
                        "payload": _json(file),
                    }
                    if row is None:
                        session.add(FileAssetEntity(id=str(file.file_id), **values))
                    else:
                        for name, value in values.items():
                            setattr(row, name, value)
                session.flush()
                self._persist_inventory_children(session, request)
                if current == 0:
                    inventory.state_version = 1
                    inventory.updated_at = now
                session.flush()
                file_count = session.scalar(
                    select(func.count())
                    .select_from(FileAssetEntity)
                    .where(FileAssetEntity.inventory_id == inventory_id)
                )
                result = InventorySnapshotRef(
                    inventory_id=request.mutation.inventory_id,
                    state_version=current + 1,
                    file_count=int(file_count or 0),
                    created_at=now,
                    archive_id=request_id if request.archive_before_reset else None,
                    prior_state_version=current if request.archive_before_reset else None,
                    archived_file_count=archived_file_count,
                )
                _audit(
                    session,
                    request_id=request_id,
                    actor=actor,
                    tool_id="TOOL-022",
                    payload={
                        "inventory_id": inventory_id,
                        "state_version": result.state_version,
                        **(
                            {"archive_id": str(request_id), "snapshot": archive_payload}
                            if archive_payload is not None
                            else {}
                        ),
                    },
                )
                self._remember(session, request_id, "TOOL-022", request, result)
                self._checkpoint(cancellation)
                return result
        except StateConflictError:
            raise
        except (IntegrityError, SQLAlchemyError) as error:
            raise StateConflictError(
                "DB_WRITE_FAILED", "The inventory transaction failed."
            ) from error

    def archive_and_reset_inventory(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        inventory_id: UUID,
        expected_state_version: int | None,
    ) -> InventorySnapshotRef:
        if expected_state_version is None:
            raise StateConflictError("INVENTORY_NOT_FOUND", "The inventory was not found.")
        return self.persist_inventory(
            request_id=request_id,
            actor=actor,
            request=PersistInventoryInput(
                mutation=InventoryMutation(inventory_id=inventory_id),
                expected_state_version=expected_state_version,
                archive_before_reset=True,
            ),
        )

    @staticmethod
    def _inventory_archive_snapshot(session: Session, inventory_id: str) -> dict[str, Any]:
        files = session.scalars(
            select(FileAssetEntity)
            .where(FileAssetEntity.inventory_id == inventory_id)
            .order_by(FileAssetEntity.relative_path)
        ).all()
        file_ids = [row.id for row in files]

        def payloads(entity: Any, order_column: Any) -> list[dict[str, Any]]:
            if not file_ids:
                return []
            return [
                dict(row.payload)
                for row in session.scalars(
                    select(entity).where(entity.file_id.in_(file_ids)).order_by(order_column)
                ).all()
            ]

        return {
            "inventory_id": inventory_id,
            "state_version": session.scalar(
                select(InventoryEntity.state_version).where(InventoryEntity.id == inventory_id)
            ),
            "files": [dict(row.payload) for row in files],
            "detections": payloads(FormatDetectionEntity, FormatDetectionEntity.id),
            "extractions": payloads(MetadataExtractionEntity, MetadataExtractionEntity.id),
            "classifications": payloads(ClassificationEntity, ClassificationEntity.id),
            "associations": payloads(ManifestAssociationEntity, ManifestAssociationEntity.id),
        }

    @staticmethod
    def _persist_inventory_children(session: Session, request: PersistInventoryInput) -> None:
        complete_extractions = {
            item.reference.extraction_id: item for item in request.mutation.extracted_metadata
        }
        for detection in request.mutation.detections:
            detection_row = session.get(FormatDetectionEntity, str(detection.detection_id))
            values = {
                "file_id": str(detection.file_id),
                "detector_version": detection.detector_version,
                "active": True,
                "payload": _json(detection),
                "created_at": detection.detected_at or _now(),
            }
            if detection_row is None:
                session.add(
                    FormatDetectionEntity(
                        id=str(detection.detection_id),
                        **values,
                    )
                )
            else:
                for name, value in values.items():
                    setattr(detection_row, name, value)
        session.flush()
        for extraction in request.mutation.extractions:
            complete = complete_extractions.get(extraction.extraction_id)
            extraction_row = session.get(MetadataExtractionEntity, str(extraction.extraction_id))
            values = {
                "file_id": str(extraction.file_id),
                "file_sha256": extraction.file_sha256,
                "format_id": extraction.format_id.value,
                "parser_version": extraction.parser_version,
                "payload": _json(complete or extraction),
                "errors": [],
                "provenance": [str(item) for item in extraction.provenance_ids],
                "created_at": extraction.extracted_at,
            }
            if extraction_row is None:
                session.add(
                    MetadataExtractionEntity(
                        id=str(extraction.extraction_id),
                        **values,
                    )
                )
            else:
                for name, value in values.items():
                    setattr(extraction_row, name, value)
        session.flush()
        for classification in request.mutation.classifications:
            classification_row = session.get(
                ClassificationEntity, str(classification.classification_id)
            )
            evidence_ids = set(classification.evidence_ids)
            matched_detection = next(
                (
                    item
                    for item in request.mutation.detections
                    if item.detection_id == classification.detection_id
                ),
                None,
            )
            if matched_detection is not None:
                evidence_ids.update(
                    evidence_id
                    for candidate in matched_detection.candidates
                    for evidence_id in candidate.evidence_ids
                )
            payload = _json(classification)
            payload["_evidence"] = [
                _json(item)
                for item in request.mutation.evidence
                if item.evidence_id in evidence_ids
            ]
            payload["_provenance"] = [
                _json(item)
                for item in request.mutation.provenance
                if item.source_ref == str(classification.file_id)
            ]
            values = {
                "file_id": str(classification.file_id),
                "detection_id": str(classification.detection_id),
                "active": True,
                "payload": payload,
                "created_at": _now(),
            }
            if classification_row is None:
                session.add(
                    ClassificationEntity(
                        id=str(classification.classification_id),
                        **values,
                    )
                )
            else:
                for name, value in values.items():
                    setattr(classification_row, name, value)

    def persist_associations(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        request: PersistAssociationsInput,
        cancellation: CancellationCheck | None = None,
    ) -> AssociationSnapshotRef:
        try:
            with self.session_factory.begin() as session:
                replay = self._replay(
                    session, request_id, "TOOL-023", request, AssociationSnapshotRef
                )
                if replay is not None:
                    return replay
                counter = session.get(StateCounterEntity, "associations")
                current = 0 if counter is None else counter.version
                if current == request.expected_state_version + 1 and all(
                    self._association_matches(session, mutation) for mutation in request.mutations
                ):
                    return AssociationSnapshotRef(
                        state_version=current,
                        association_ids=tuple(
                            item.association.association_id for item in request.mutations
                        ),
                        recorded_at=_now(),
                    )
                if current != request.expected_state_version:
                    raise StateConflictError(
                        "ASSOCIATION_CONFLICT",
                        "The association state version is stale.",
                        retryable=True,
                    )
                if counter is None:
                    counter = StateCounterEntity(namespace="associations", version=0)
                    session.add(counter)
                else:
                    changed = session.connection().execute(
                        update(StateCounterEntity)
                        .where(
                            StateCounterEntity.namespace == "associations",
                            StateCounterEntity.version == request.expected_state_version,
                        )
                        .values(version=current + 1)
                    )
                    if changed.rowcount != 1:
                        raise StateConflictError(
                            "ASSOCIATION_CONFLICT",
                            "The association state version is stale.",
                            retryable=True,
                        )
                ids: list[UUID] = []
                for mutation in request.mutations:
                    association = mutation.association
                    if mutation.review_status is not ReviewStatus.PROPOSED:
                        if mutation.reviewer_decision_id is None:
                            raise StateConflictError(
                                "INVALID_REVIEW_TRANSITION",
                                "A reviewed association requires a decision reference.",
                            )
                        decision = session.get(
                            ReviewDecisionEntity, str(mutation.reviewer_decision_id)
                        )
                        expected_decision = {
                            ReviewStatus.APPROVED: ReviewDecisionValue.APPROVE.value,
                            ReviewStatus.REJECTED: ReviewDecisionValue.REJECT.value,
                            ReviewStatus.NEEDS_CHANGES: ReviewDecisionValue.NEEDS_CHANGES.value,
                        }[mutation.review_status]
                        if (
                            decision is None
                            or decision.target_type != ReviewTargetType.MANIFEST_ASSOCIATION.value
                            or decision.target_id != str(association.association_id)
                            or decision.target_version != association.target_version
                            or decision.decision != expected_decision
                        ):
                            raise StateConflictError(
                                "INVALID_REVIEW_TRANSITION",
                                "The association review decision is invalid.",
                            )
                    row = session.get(ManifestAssociationEntity, str(association.association_id))
                    trust = (
                        TrustLevel.VERIFIED
                        if mutation.review_status is ReviewStatus.APPROVED
                        else association.trust_level
                    )
                    payload = _json(
                        association.model_copy(
                            update={
                                "review_status": mutation.review_status,
                                "trust_level": trust,
                            }
                        )
                    )
                    if row is None:
                        row = ManifestAssociationEntity(
                            id=str(association.association_id),
                            state_version=current + 1,
                            file_id=str(association.file_id),
                            manifest_id=str(association.manifest_id),
                            score=association.score,
                            method=association.method,
                            evidence_ids=[str(item) for item in association.evidence_ids],
                            review_status=mutation.review_status.value,
                            trust_level=trust.value,
                            target_version=association.target_version,
                            payload=payload,
                        )
                        session.add(row)
                    elif row.file_id != str(association.file_id) or row.manifest_id != str(
                        association.manifest_id
                    ):
                        raise StateConflictError(
                            "ASSOCIATION_CONFLICT", "Association identity cannot be reassigned."
                        )
                    else:
                        row.score = association.score
                        row.method = association.method
                        row.evidence_ids = [str(item) for item in association.evidence_ids]
                        row.review_status = mutation.review_status.value
                        row.trust_level = trust.value
                        row.target_version = association.target_version
                        row.state_version = current + 1
                        row.payload = payload
                    ids.append(association.association_id)
                if current == 0:
                    counter.version = 1
                result = AssociationSnapshotRef(
                    state_version=current + 1,
                    association_ids=tuple(ids),
                    recorded_at=_now(),
                )
                _audit(
                    session,
                    request_id=request_id,
                    actor=actor,
                    tool_id="TOOL-023",
                    payload={"state_version": result.state_version, "count": len(ids)},
                )
                self._remember(session, request_id, "TOOL-023", request, result)
                self._checkpoint(cancellation)
                return result
        except StateConflictError:
            raise
        except (IntegrityError, SQLAlchemyError) as error:
            raise StateConflictError(
                "DB_WRITE_FAILED", "The association transaction failed."
            ) from error

    def persist_learning_model(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        request: PersistLearningModelInput,
        cancellation: CancellationCheck | None = None,
    ) -> LearningModelVersionOutput:
        try:
            with self.session_factory.begin() as session:
                replay = self._replay(
                    session, request_id, "TOOL-024", request, LearningModelVersionOutput
                )
                if replay is not None:
                    return replay
                mutation = request.mutation
                if mutation.action is LearningMutationAction.CREATE:
                    if mutation.model is None:
                        raise StateConflictError(
                            "MODEL_VERSION_CONFLICT", "A create mutation requires a model."
                        )
                    self._resolve_learning_examples(session, mutation.model.example_identities)
                    model_id = mutation.model.learning_model_id
                    latest = self._latest_model(session, model_id)
                    current = 0 if latest is None else latest.version
                    if (
                        latest is not None
                        and mutation.expected_version == current - 1
                        and latest.payload == _json(mutation.model)
                    ):
                        return self._model_output(latest)
                    if mutation.expected_version != current:
                        raise StateConflictError(
                            "MODEL_VERSION_CONFLICT", "The learning model version is stale."
                        )
                    result = self._append_model(
                        session, mutation.model, current + 1, LearningModelStatus.DRAFT
                    )
                    for example in mutation.model.example_identities:
                        if session.get(LearningExampleEntity, str(example.example_id)) is None:
                            session.add(
                                LearningExampleEntity(
                                    id=str(example.example_id),
                                    source_file_id=str(example.source_file_id),
                                    manifest_id=str(example.manifest_id),
                                    association_id=str(example.association_id),
                                    source_sha256=example.source_sha256,
                                    manifest_sha256=example.manifest_sha256,
                                    review_status=example.review_status.value,
                                    generated_manifest=example.generated_manifest,
                                )
                            )
                elif mutation.action is LearningMutationAction.CLEAR:
                    active = self._current_active_models(session)
                    if not active:
                        latest_inactive = session.scalar(
                            select(LearningModelVersionEntity)
                            .where(
                                LearningModelVersionEntity.status
                                == LearningModelStatus.INACTIVE.value
                            )
                            .order_by(LearningModelVersionEntity.recorded_at.desc())
                            .limit(1)
                        )
                        if latest_inactive is None:
                            raise StateConflictError(
                                "MODEL_IN_USE", "No active learning model exists."
                            )
                        return self._model_output(latest_inactive)
                    result = self._append_existing_status(
                        session, active[-1], LearningModelStatus.INACTIVE
                    )
                    for row in active[:-1]:
                        self._append_existing_status(session, row, LearningModelStatus.INACTIVE)
                else:
                    if mutation.learning_model_id is None:
                        raise StateConflictError(
                            "MODEL_VERSION_CONFLICT", "A model mutation requires an ID."
                        )
                    latest = self._latest_model(session, mutation.learning_model_id)
                    if latest is None:
                        raise StateConflictError("MODEL_VERSION_CONFLICT", "Model was not found.")
                    if mutation.expected_version != latest.version:
                        raise StateConflictError(
                            "MODEL_VERSION_CONFLICT", "The learning model version is stale."
                        )
                    status = (
                        LearningModelStatus.ACTIVE
                        if mutation.action is LearningMutationAction.ACTIVATE
                        else LearningModelStatus.INACTIVE
                    )
                    if (
                        latest.status == status.value
                        and mutation.expected_version == latest.version - 1
                    ):
                        return self._model_output(latest)
                    if status is LearningModelStatus.ACTIVE:
                        for active_row in self._current_active_models(
                            session, category=latest.category
                        ):
                            if active_row.learning_model_id != latest.learning_model_id:
                                self._append_existing_status(
                                    session, active_row, LearningModelStatus.INACTIVE
                                )
                    result = self._append_existing_status(session, latest, status)
                _audit(
                    session,
                    request_id=request_id,
                    actor=actor,
                    tool_id="TOOL-024",
                    payload={
                        "learning_model_id": str(result.learning_model_id),
                        "version": result.version,
                        "status": result.status,
                    },
                )
                self._remember(session, request_id, "TOOL-024", request, result)
                self._checkpoint(cancellation)
                return result
        except StateConflictError:
            raise
        except (IntegrityError, SQLAlchemyError) as error:
            raise StateConflictError("DB_WRITE_FAILED", "The model transaction failed.") from error

    @staticmethod
    def _latest_model(session: Session, model_id: UUID | str) -> LearningModelVersionEntity | None:
        return session.scalar(
            select(LearningModelVersionEntity)
            .where(LearningModelVersionEntity.learning_model_id == str(model_id))
            .order_by(LearningModelVersionEntity.version.desc())
            .limit(1)
        )

    def _current_active_models(
        self, session: Session, *, category: str | None = None
    ) -> list[LearningModelVersionEntity]:
        ids = session.scalars(select(LearningModelVersionEntity.learning_model_id).distinct()).all()
        latest = [self._latest_model(session, item) for item in ids]
        return [
            row
            for row in latest
            if row is not None
            and row.status == LearningModelStatus.ACTIVE.value
            and (category is None or row.category == category)
        ]

    @staticmethod
    def _append_model(
        session: Session,
        model: Any,
        version: int,
        status: LearningModelStatus,
    ) -> LearningModelVersionOutput:
        now = _now()
        session.add(
            LearningModelVersionEntity(
                learning_model_id=str(model.learning_model_id),
                category=model.category.value,
                version=version,
                status=status.value,
                model_sha256=model.model_sha256,
                payload=_json(model),
                recorded_at=now,
            )
        )
        return LearningModelVersionOutput(
            learning_model_id=model.learning_model_id,
            version=version,
            status=status.value,
            recorded_at=now,
        )

    @staticmethod
    def _append_existing_status(
        session: Session,
        source: LearningModelVersionEntity,
        status: LearningModelStatus,
    ) -> LearningModelVersionOutput:
        now = _now()
        version = source.version + 1
        session.add(
            LearningModelVersionEntity(
                learning_model_id=source.learning_model_id,
                category=source.category,
                version=version,
                status=status.value,
                model_sha256=source.model_sha256,
                payload=source.payload,
                recorded_at=now,
            )
        )
        return LearningModelVersionOutput(
            learning_model_id=UUID(source.learning_model_id),
            version=version,
            status=status.value,
            recorded_at=now,
        )

    @staticmethod
    def _model_output(row: LearningModelVersionEntity) -> LearningModelVersionOutput:
        recorded_at = row.recorded_at
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        return LearningModelVersionOutput(
            learning_model_id=UUID(row.learning_model_id),
            version=row.version,
            status=row.status,
            recorded_at=recorded_at,
        )

    def list_learning_model_versions(self, model_id: UUID) -> tuple[int, ...]:
        with self.session_factory() as session:
            return tuple(
                session.scalars(
                    select(LearningModelVersionEntity.version)
                    .where(LearningModelVersionEntity.learning_model_id == str(model_id))
                    .order_by(LearningModelVersionEntity.version)
                ).all()
            )

    def persist_manifest(
        self,
        manifest: ManifestDocumentRef,
        content: ManifestJsonDocument,
        *,
        validation: ValidationReport | None = None,
        provenance: tuple[ProvenanceRecord, ...] = (),
        request_id: UUID | None = None,
        actor: ActorRef | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            row = session.get(ManifestDocumentEntity, str(manifest.manifest_id))
            values = {
                "path": manifest.path.root,
                "sha256": manifest.sha256,
                "document_kind": (
                    manifest.document_kind.root if manifest.document_kind is not None else None
                ),
                "normalized_content_ref": manifest.normalized_content_ref,
                "normalized_content": content.model_dump(mode="json")["content"],
                "validation": None if validation is None else _json(validation),
                "provenance": [_json(item) for item in provenance],
                "generated": manifest.generated,
            }
            if row is None:
                session.add(ManifestDocumentEntity(id=str(manifest.manifest_id), **values))
            else:
                if row.sha256 != manifest.sha256:
                    raise StateConflictError(
                        "STATE_VERSION_CONFLICT", "Manifest identity cannot be reassigned."
                    )
                return
            _audit(
                session,
                request_id=request_id or manifest.manifest_id,
                actor=actor or ActorRef(actor_id="state-import"),
                tool_id="TOOL-022",
                payload={"manifest_id": str(manifest.manifest_id)},
            )

    def persist_manifest_validation(
        self,
        manifest: ManifestDocumentRef,
        content: ManifestJsonDocument,
        validation: ValidationReport,
        provenance: tuple[ProvenanceRecord, ...],
        *,
        request_id: UUID,
        actor: ActorRef,
    ) -> None:
        try:
            with self.session_factory.begin() as session:
                row = session.get(ManifestDocumentEntity, str(manifest.manifest_id))
                normalized_content = content.model_dump(mode="json")["content"]
                if (
                    row is None
                    or row.path != manifest.path.root
                    or row.sha256 != manifest.sha256
                    or row.normalized_content != normalized_content
                ):
                    raise StateConflictError(
                        "STATE_VERSION_CONFLICT",
                        "Manifest validation requires the immutable persisted document.",
                    )
                row.validation = _json(validation)
                row.provenance = [_json(item) for item in provenance]
                _audit(
                    session,
                    request_id=request_id,
                    actor=actor,
                    tool_id="TOOL-020",
                    payload={
                        "manifest_id": str(manifest.manifest_id),
                        "validation_status": validation.status.value,
                    },
                )
        except StateConflictError:
            raise
        except (IntegrityError, SQLAlchemyError) as error:
            raise StateConflictError(
                "DB_WRITE_FAILED", "The manifest validation transaction failed."
            ) from error

    def persist_generated_candidate(
        self,
        candidate: GeneratedManifestCandidate,
        *,
        validation: ValidationReport | None = None,
        provenance: tuple[ProvenanceRecord, ...] = (),
        source_content: ManifestJsonDocument | None = None,
        request_id: UUID | None = None,
        actor: ActorRef | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            identifier = str(candidate.reference.candidate_id)
            row = session.get(GeneratedCandidateEntity, identifier)
            payload = _json(candidate)
            if row is None:
                session.add(
                    GeneratedCandidateEntity(
                        id=identifier,
                        source_file_id=str(candidate.reference.source_file_id),
                        learning_model_id=str(candidate.reference.learning_model_id),
                        candidate_sha256=candidate.reference.candidate_sha256,
                        proposed_path=candidate.reference.proposed_path.root,
                        generation_status=candidate.reference.generation_status.value,
                        validation_status=candidate.reference.validation_status.value,
                        review_status=candidate.reference.review_status.value,
                        trust_level=candidate.reference.trust_level.value,
                        state_version=1,
                        payload=payload,
                        validation=None if validation is None else _json(validation),
                        provenance=[_json(item) for item in provenance],
                        source_content=(
                            None
                            if source_content is None
                            else source_content.model_dump(mode="json")["content"]
                        ),
                    )
                )
            elif row.candidate_sha256 != candidate.reference.candidate_sha256:
                raise StateConflictError(
                    "STATE_VERSION_CONFLICT", "Candidate identity cannot be reassigned."
                )
            else:
                if validation is not None:
                    row.validation = _json(validation)
                    row.validation_status = candidate.reference.validation_status.value
                    row.payload = payload
                if provenance:
                    row.provenance = [_json(item) for item in provenance]
                if source_content is not None:
                    row.source_content = source_content.model_dump(mode="json")["content"]
                return
            _audit(
                session,
                request_id=request_id or candidate.reference.candidate_id,
                actor=actor or ActorRef(actor_id="generation-tool"),
                tool_id="TOOL-022",
                payload={"candidate_id": identifier},
            )

    def persist_workspace(self, descriptor: WorkspaceDescriptor, actor: ActorRef) -> None:
        with self.session_factory.begin() as session:
            row = session.get(WorkspaceEntity, str(descriptor.workspace_id))
            values = {
                "canonical_root": descriptor.canonical_root.root,
                "read_only": descriptor.read_only,
                "allowed_output_subpaths": [
                    item.root for item in descriptor.allowed_output_subpaths
                ],
                "policy_fingerprint": descriptor.policy_fingerprint,
                "version": 1,
                "created_actor": actor.model_dump(mode="json"),
                "created_at": _now(),
            }
            if row is None:
                session.add(WorkspaceEntity(id=str(descriptor.workspace_id), **values))
            elif row.policy_fingerprint != descriptor.policy_fingerprint:
                raise StateConflictError(
                    "STATE_VERSION_CONFLICT", "Workspace policy identity cannot be reassigned."
                )

    def list_workspaces(self) -> tuple[WorkspaceDescriptor, ...]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(WorkspaceEntity).order_by(WorkspaceEntity.created_at)
            ).all()
            return tuple(
                WorkspaceDescriptor(
                    workspace_id=UUID(row.id),
                    canonical_root=ApprovedAbsolutePath(row.canonical_root),
                    read_only=row.read_only,
                    allowed_output_subpaths=tuple(
                        WorkspaceRelativePath(value) for value in row.allowed_output_subpaths
                    ),
                    policy_fingerprint=row.policy_fingerprint,
                )
                for row in rows
            )

    def load_runtime_file_records(self) -> tuple[tuple[UUID, FileRecordContract], ...]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FileAssetEntity).order_by(FileAssetEntity.inventory_id, FileAssetEntity.id)
            ).all()
            loaded: list[tuple[UUID, FileRecordContract]] = []
            for row in rows:
                file = FileAssetRef.model_validate_json(json.dumps(row.payload))
                detection_row = session.scalar(
                    select(FormatDetectionEntity)
                    .where(
                        FormatDetectionEntity.file_id == row.id,
                        FormatDetectionEntity.active.is_(True),
                    )
                    .order_by(FormatDetectionEntity.created_at.desc())
                    .limit(1)
                )
                classification_row = session.scalar(
                    select(ClassificationEntity)
                    .where(
                        ClassificationEntity.file_id == row.id,
                        ClassificationEntity.active.is_(True),
                    )
                    .order_by(ClassificationEntity.created_at.desc())
                    .limit(1)
                )
                extraction_rows = session.scalars(
                    select(MetadataExtractionEntity)
                    .where(MetadataExtractionEntity.file_id == row.id)
                    .order_by(MetadataExtractionEntity.created_at)
                ).all()
                extracted = tuple(
                    ExtractedMetadataContract.model_validate_json(json.dumps(item.payload))
                    for item in extraction_rows
                    if "reference" in item.payload and "payload" in item.payload
                )
                classification_payload = (
                    {}
                    if classification_row is None
                    else {
                        key: value
                        for key, value in classification_row.payload.items()
                        if not key.startswith("_")
                    }
                )
                loaded.append(
                    (
                        UUID(row.inventory_id),
                        FileRecordContract(
                            file=file,
                            detection=(
                                None
                                if detection_row is None
                                else FormatDetectionResult.model_validate_json(
                                    json.dumps(detection_row.payload)
                                )
                            ),
                            classification=(
                                None
                                if classification_row is None
                                else ClassificationRecord.model_validate_json(
                                    json.dumps(classification_payload)
                                )
                            ),
                            metadata_extractions=extracted,
                        ),
                    )
                )
            return tuple(loaded)

    def get_generated_candidate(self, candidate_id: UUID) -> GeneratedManifestCandidate | None:
        with self.session_factory() as session:
            row = session.get(GeneratedCandidateEntity, str(candidate_id))
            if row is None:
                return None
            candidate = GeneratedManifestCandidate.model_validate_json(json.dumps(row.payload))
            return candidate.model_copy(
                update={
                    "reference": candidate.reference.model_copy(
                        update={"review_status": ReviewStatus(row.review_status)}
                    )
                }
            )

    def get_manifest_content(self, manifest_id: UUID) -> ManifestJsonDocument | None:
        with self.session_factory() as session:
            row = session.get(ManifestDocumentEntity, str(manifest_id))
            if row is None or row.normalized_content is None:
                return None
            return ManifestJsonDocument(sha256=row.sha256, content=row.normalized_content)

    def get_inventory_version(self, inventory_id: UUID) -> int | None:
        with self.session_factory() as session:
            return session.scalar(
                select(InventoryEntity.state_version).where(InventoryEntity.id == str(inventory_id))
            )

    def get_association_version(self) -> int:
        with self.session_factory() as session:
            return (
                session.scalar(
                    select(StateCounterEntity.version).where(
                        StateCounterEntity.namespace == "associations"
                    )
                )
                or 0
            )

    def resolve_learning_examples(
        self, requested: tuple[LearningExampleRef, ...]
    ) -> tuple[LearningExampleRef, ...]:
        """Resolve exact approved, human-reviewed examples from persisted state."""
        with self.session_factory() as session:
            return self._resolve_learning_examples(session, requested)

    @staticmethod
    def _resolve_learning_examples(
        session: Session, requested: tuple[LearningExampleRef, ...]
    ) -> tuple[LearningExampleRef, ...]:
        resolved: list[LearningExampleRef] = []
        for example in requested:
            association = session.get(ManifestAssociationEntity, str(example.association_id))
            file = session.get(FileAssetEntity, str(example.source_file_id))
            manifest = session.get(ManifestDocumentEntity, str(example.manifest_id))
            if association is None or file is None or manifest is None:
                raise StateConflictError(
                    "NO_ELIGIBLE_EXAMPLES",
                    "Every learning example must match persisted state.",
                )
            if file.sha256 is None:
                raise StateConflictError(
                    "NO_ELIGIBLE_EXAMPLES",
                    "Every learning example requires a persisted complete file hash.",
                )
            decision = session.scalar(
                select(ReviewDecisionEntity)
                .where(
                    ReviewDecisionEntity.target_type == ReviewTargetType.MANIFEST_ASSOCIATION.value,
                    ReviewDecisionEntity.target_id == str(example.association_id),
                    ReviewDecisionEntity.target_version == manifest.sha256,
                    ReviewDecisionEntity.decision == ReviewDecisionValue.APPROVE.value,
                )
                .order_by(ReviewDecisionEntity.decided_at.desc())
                .limit(1)
            )
            exact = (
                decision is not None
                and association.file_id == str(example.source_file_id)
                and association.manifest_id == str(example.manifest_id)
                and association.review_status == ReviewStatus.APPROVED.value
                and association.target_version == manifest.sha256
                and file.sha256 == example.source_sha256
                and manifest.sha256 == example.manifest_sha256
                and not manifest.generated
            )
            if not exact:
                raise StateConflictError(
                    "NO_ELIGIBLE_EXAMPLES",
                    ("Every learning example must exactly match a persisted approved association."),
                )
            resolved.append(
                LearningExampleRef(
                    example_id=example.example_id,
                    source_file_id=UUID(association.file_id),
                    manifest_id=UUID(association.manifest_id),
                    association_id=UUID(association.id),
                    source_sha256=file.sha256,
                    manifest_sha256=manifest.sha256,
                    review_status=ReviewStatus.APPROVED,
                    generated_manifest=False,
                )
            )
        return tuple(resolved)

    def get_active_learning_model(self) -> LearningModelVersionOutput | None:
        with self.session_factory() as session:
            active = self._current_active_models(session)
            if not active:
                return None
            latest = max(active, key=lambda row: row.recorded_at)
            return self._model_output(latest)

    def get_active_learning_models(self) -> tuple[LearningModelContract, ...]:
        """Return the persisted payload for every currently active model version."""
        with self.session_factory() as session:
            return tuple(
                LearningModelContract.model_validate_json(json.dumps(row.payload))
                for row in self._current_active_models(session)
            )

    def get_manifest_document(self, manifest_id: UUID) -> ManifestDocumentRef | None:
        with self.session_factory() as session:
            row = session.get(ManifestDocumentEntity, str(manifest_id))
            if row is None:
                return None
            document_kind = row.document_kind
            if (
                document_kind is not None
                and document_kind.startswith("root='")
                and document_kind.endswith("'")
            ):
                document_kind = document_kind[6:-1]
            return ManifestDocumentRef(
                manifest_id=UUID(row.id),
                path=WorkspaceRelativePath(row.path),
                sha256=row.sha256,
                document_kind=OSDUKind(document_kind) if document_kind else None,
                normalized_content_ref=row.normalized_content_ref,
                generated=row.generated,
            )

    def list_manifest_documents(
        self,
    ) -> tuple[tuple[ManifestDocumentRef, ManifestJsonDocument], ...]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ManifestDocumentEntity).order_by(ManifestDocumentEntity.path)
            ).all()
            documents: list[tuple[ManifestDocumentRef, ManifestJsonDocument]] = []
            for row in rows:
                if row.normalized_content is None:
                    continue
                document_kind = row.document_kind
                documents.append(
                    (
                        ManifestDocumentRef(
                            manifest_id=UUID(row.id),
                            path=WorkspaceRelativePath(row.path),
                            sha256=row.sha256,
                            document_kind=OSDUKind(document_kind) if document_kind else None,
                            normalized_content_ref=row.normalized_content_ref,
                            generated=row.generated,
                        ),
                        ManifestJsonDocument(
                            sha256=row.sha256,
                            content=row.normalized_content,
                        ),
                    )
                )
            return tuple(documents)

    def list_associated_file_ids(self) -> frozenset[UUID]:
        with self.session_factory() as session:
            return frozenset(
                UUID(value)
                for value in session.scalars(
                    select(ManifestAssociationEntity.file_id).distinct()
                ).all()
            )

    def get_candidate_review_context(
        self, candidate_id: UUID
    ) -> tuple[ValidationReport | None, tuple[ProvenanceRecord, ...], dict[str, Any] | None]:
        with self.session_factory() as session:
            row = session.get(GeneratedCandidateEntity, str(candidate_id))
            if row is None:
                raise StateConflictError("STALE_REVIEW_TARGET", "Candidate was not found.")
            validation = (
                None
                if row.validation is None
                else ValidationReport.model_validate_json(json.dumps(row.validation))
            )
            provenance = tuple(
                ProvenanceRecord.model_validate_json(json.dumps(item)) for item in row.provenance
            )
            return validation, provenance, row.source_content

    def get_manifest_review_context(
        self, manifest_id: UUID
    ) -> tuple[ValidationReport | None, tuple[ProvenanceRecord, ...]]:
        with self.session_factory() as session:
            row = session.get(ManifestDocumentEntity, str(manifest_id))
            if row is None:
                raise StateConflictError("STALE_REVIEW_TARGET", "Manifest was not found.")
            validation = (
                None
                if row.validation is None
                else ValidationReport.model_validate_json(json.dumps(row.validation))
            )
            provenance = tuple(
                ProvenanceRecord.model_validate_json(json.dumps(item)) for item in row.provenance
            )
            return validation, provenance

    def record_audit(
        self,
        *,
        request_id: UUID,
        actor: ActorRef,
        tool_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self.session_factory.begin() as session:
            if (
                session.scalar(
                    select(AuditEventEntity).where(AuditEventEntity.request_id == str(request_id))
                )
                is None
            ):
                _audit(
                    session,
                    request_id=request_id,
                    actor=actor,
                    tool_id=tool_id,
                    payload=payload,
                )

    def record_review_decision(
        self,
        *,
        request_id: UUID,
        decision: ReviewDecision,
        cancellation: CancellationCheck | None = None,
    ) -> ReviewDecisionReceipt:
        try:
            with self.session_factory.begin() as session:
                decision_key = self._decision_key(decision)
                semantic_replay = session.scalar(
                    select(ReviewDecisionEntity).where(
                        ReviewDecisionEntity.idempotency_key == decision_key
                    )
                )
                if semantic_replay is not None:
                    return self._decision_receipt(semantic_replay)
                existing = session.get(ReviewDecisionEntity, str(decision.decision_id))
                if existing is not None:
                    decided_at = existing.decided_at
                    if decided_at.tzinfo is None:
                        decided_at = decided_at.replace(tzinfo=UTC)
                    original = ReviewDecision(
                        decision_id=UUID(existing.id),
                        actor=ActorRef.model_validate(existing.actor),
                        target_type=ReviewTargetType(existing.target_type),
                        target_id=UUID(existing.target_id),
                        target_version=existing.target_version,
                        decision=ReviewDecisionValue(existing.decision),
                        reason=existing.reason,
                        decided_at=decided_at,
                    )
                    if original != decision:
                        raise StateConflictError(
                            "INVALID_DECISION", "Decision identity cannot be reused."
                        )
                    return self._decision_receipt(existing)
                target = (
                    session.get(GeneratedCandidateEntity, str(decision.target_id))
                    if decision.target_type is ReviewTargetType.GENERATED_CANDIDATE
                    else session.get(ManifestAssociationEntity, str(decision.target_id))
                )
                if target is None:
                    raise StateConflictError("STALE_REVIEW_TARGET", "Review target was not found.")
                target_version = (
                    target.candidate_sha256
                    if isinstance(target, GeneratedCandidateEntity)
                    else target.target_version or str(target.state_version)
                )
                if target_version != decision.target_version:
                    raise StateConflictError(
                        "STALE_REVIEW_TARGET", "Review target version is stale."
                    )
                status = {
                    ReviewDecisionValue.APPROVE: ReviewStatus.APPROVED,
                    ReviewDecisionValue.REJECT: ReviewStatus.REJECTED,
                    ReviewDecisionValue.NEEDS_CHANGES: ReviewStatus.NEEDS_CHANGES,
                }[decision.decision]
                target.state_version += 1
                target.review_status = status.value
                payload = dict(target.payload)
                if isinstance(target, GeneratedCandidateEntity):
                    reference = dict(payload["reference"])
                    reference["review_status"] = status.value
                    payload["reference"] = reference
                else:
                    payload["review_status"] = status.value
                    if status is ReviewStatus.APPROVED:
                        target.trust_level = "verified"
                        payload["trust_level"] = "verified"
                target.payload = payload
                row = ReviewDecisionEntity(
                    id=str(decision.decision_id),
                    idempotency_key=decision_key,
                    actor=_json(decision.actor),
                    target_type=decision.target_type.value,
                    target_id=str(decision.target_id),
                    target_version=decision.target_version,
                    decision=decision.decision.value,
                    reason=decision.reason,
                    decided_at=decision.decided_at,
                    state_version=target.state_version,
                )
                session.add(row)
                _audit(
                    session,
                    request_id=request_id,
                    actor=decision.actor,
                    tool_id="TOOL-029",
                    payload={
                        "decision_id": str(decision.decision_id),
                        "target_id": str(decision.target_id),
                    },
                )
                session.flush()
                self._checkpoint(cancellation)
                return self._decision_receipt(row)
        except StateConflictError:
            raise
        except (IntegrityError, SQLAlchemyError) as error:
            raise StateConflictError("DB_WRITE_FAILED", "The review transaction failed.") from error

    @staticmethod
    def _decision_receipt(row: ReviewDecisionEntity) -> ReviewDecisionReceipt:
        status = {
            ReviewDecisionValue.APPROVE.value: ReviewStatus.APPROVED,
            ReviewDecisionValue.REJECT.value: ReviewStatus.REJECTED,
            ReviewDecisionValue.NEEDS_CHANGES.value: ReviewStatus.NEEDS_CHANGES,
        }[row.decision]
        recorded = row.decided_at
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        return ReviewDecisionReceipt(
            decision_id=UUID(row.id),
            target_id=UUID(row.target_id),
            target_version=row.target_version,
            review_status=status,
            recorded_at=recorded,
            state_version=row.state_version,
        )

    @staticmethod
    def _decision_key(decision: ReviewDecision) -> str:
        material = {
            "actor_id": decision.actor.actor_id,
            "decision": decision.decision.value,
            "target_id": str(decision.target_id),
            "target_type": decision.target_type.value,
            "target_version": decision.target_version,
        }
        return sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _association_matches(session: Session, mutation: AssociationMutation) -> bool:
        row = session.get(ManifestAssociationEntity, str(mutation.association.association_id))
        if row is None:
            return False
        expected = _json(
            mutation.association.model_copy(
                update={
                    "review_status": mutation.review_status,
                    "trust_level": (
                        TrustLevel.VERIFIED
                        if mutation.review_status is ReviewStatus.APPROVED
                        else mutation.association.trust_level
                    ),
                }
            )
        )
        return row.payload == expected

    @staticmethod
    def _checkpoint(cancellation: CancellationCheck | None) -> None:
        if cancellation is not None and cancellation():
            raise StateConflictError("CANCELLED", "The state mutation was cancelled.")


__all__ = ["StateConflictError", "StateRepository"]
