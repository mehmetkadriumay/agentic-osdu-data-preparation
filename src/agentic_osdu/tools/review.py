"""TOOL-027..030 deterministic review projections, decisions, and exports."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from agentic_osdu.domain.models import (
    ActorRef,
    ClassificationRecord,
    FileAssetRef,
    GeneratedManifestCandidate,
    ManifestAssociation,
    ReviewStatus,
    WorkspaceRelativePath,
)
from agentic_osdu.policy import OutputPolicy, PolicyViolation
from agentic_osdu.state.models import (
    ClassificationEntity,
    FileAssetEntity,
    ManifestAssociationEntity,
)
from agentic_osdu.state.repositories import StateConflictError, StateRepository
from agentic_osdu.tools.contracts import (
    BuildInventoryReviewInput,
    BuildManifestReviewInput,
    ExportedFileRef,
    ExportKind,
    ExportReceipt,
    ExportToolInput,
    ExportToolOutput,
    GenerationDiff,
    InventoryReviewItem,
    InventoryReviewView,
    ManifestReviewView,
    RecordReviewDecisionInput,
    RecordReviewDecisionOutput,
    ReviewSummaryCount,
)

CancellationCheck = Callable[[], bool]


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ReviewService:
    def __init__(self, repository: StateRepository, output_policy: OutputPolicy) -> None:
        self._repository = repository
        self._output_policy = output_policy

    def build_inventory_review(
        self,
        request: BuildInventoryReviewInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> InventoryReviewView:
        if self._repository.get_inventory_version(request.inventory_id) is None:
            raise ReviewError("INVENTORY_NOT_FOUND", "The inventory was not found.")
        with self._repository.session_factory() as session:
            rows = session.scalars(
                select(FileAssetEntity)
                .where(FileAssetEntity.inventory_id == str(request.inventory_id))
                .order_by(FileAssetEntity.relative_path)
            ).all()
            items: list[InventoryReviewItem] = []
            for row in rows:
                self._checkpoint(cancellation)
                file = self._file_from_payload(row.payload)
                classification_row = session.scalar(
                    select(ClassificationEntity)
                    .where(
                        ClassificationEntity.file_id == row.id,
                        ClassificationEntity.active.is_(True),
                    )
                    .order_by(ClassificationEntity.created_at.desc())
                    .limit(1)
                )
                association_row = session.scalar(
                    select(ManifestAssociationEntity)
                    .where(ManifestAssociationEntity.file_id == row.id)
                    .order_by(
                        ManifestAssociationEntity.score.desc(),
                        ManifestAssociationEntity.id,
                    )
                    .limit(1)
                )
                classification = (
                    None
                    if classification_row is None
                    else ClassificationRecord.model_validate_json(
                        json.dumps(classification_row.payload)
                    )
                )

                association = (
                    None
                    if association_row is None
                    else ManifestAssociation.model_validate_json(
                        json.dumps(association_row.payload)
                    ).model_copy(
                        update={"review_status": ReviewStatus(association_row.review_status)}
                    )
                )
                item = InventoryReviewItem(
                    file=file,
                    classification=classification,
                    association=association,
                )
                if self._matches(item, request):
                    items.append(item)
            items = self._sort(items, request)
            total = len(items)
            page = items[request.query.offset : request.query.offset + request.query.limit]
            return InventoryReviewView(
                inventory_id=request.inventory_id,
                total_count=total,
                category_summaries=self._summaries(
                    item.classification.category.value
                    if item.classification is not None
                    else "unknown"
                    for item in items
                ),
                format_summaries=self._summaries(
                    item.classification.format_id.value
                    if item.classification is not None and item.classification.format_id is not None
                    else "unknown"
                    for item in items
                ),
                review_status_summaries=self._summaries(
                    item.association.review_status.value
                    if item.association is not None
                    else ReviewStatus.PROPOSED.value
                    for item in items
                ),
                items=tuple(page),
            )

    def build_manifest_review(
        self,
        request: BuildManifestReviewInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> ManifestReviewView:
        self._checkpoint(cancellation)
        if isinstance(request.manifest, GeneratedManifestCandidate):
            persisted = self._repository.get_generated_candidate(
                request.manifest.reference.candidate_id
            )
            if (
                persisted is None
                or persisted.reference.candidate_sha256
                != request.manifest.reference.candidate_sha256
                or persisted.document.sha256 != request.manifest.document.sha256
                or persisted.document.content != request.manifest.document.content
                or self._document_sha256(persisted.document.content)
                != persisted.reference.candidate_sha256
                or self._document_sha256(request.manifest.document.content)
                != request.manifest.reference.candidate_sha256
            ):
                raise ReviewError(
                    "STALE_REVIEW_TARGET",
                    "The generated candidate does not match persisted content.",
                )
            try:
                validation, provenance, source = self._repository.get_candidate_review_context(
                    request.manifest.reference.candidate_id
                )
            except StateConflictError:
                validation, provenance, source = None, (), None
            return ManifestReviewView(
                manifest=persisted,
                content=persisted.document,
                validation=validation,
                provenance=provenance,
                association=self._association_for(request.source_file_id),
                generation_diff=(
                    None
                    if source is None
                    else self._diff(
                        source,
                        persisted.document.model_dump(mode="json")["content"],
                    )
                ),
            )
        content = self._repository.get_manifest_content(request.manifest.manifest_id)
        if content is None:
            raise ReviewError("MANIFEST_NOT_FOUND", "The manifest content was not found.")
        try:
            validation, provenance = self._repository.get_manifest_review_context(
                request.manifest.manifest_id
            )
        except StateConflictError:
            validation, provenance = None, ()
        self._checkpoint(cancellation)
        return ManifestReviewView(
            manifest=request.manifest,
            content=content,
            validation=validation,
            provenance=provenance,
            association=self._association_for(request.source_file_id),
            generation_diff=None,
        )

    def record_decision(
        self,
        *,
        request_id: UUID,
        request: RecordReviewDecisionInput,
        cancellation: CancellationCheck | None = None,
    ) -> RecordReviewDecisionOutput:
        try:
            receipt = self._repository.record_review_decision(
                request_id=request_id,
                decision=request.decision,
                cancellation=cancellation,
            )
        except StateConflictError as error:
            raise ReviewError(error.code, "The review decision could not be recorded.") from error
        return RecordReviewDecisionOutput(receipt=receipt)

    def export(
        self,
        request: ExportToolInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> ExportToolOutput:
        export_request = request.request
        if export_request.export_kind is ExportKind.APPROVED_MANIFEST:
            candidate = self._repository.get_generated_candidate(export_request.target_id)
            if candidate is None:
                raise ReviewError("MANIFEST_NOT_FOUND", "The export target was not found.")
            content_sha256 = self._document_sha256(candidate.document.content)
            if (
                content_sha256 != candidate.reference.candidate_sha256
                or content_sha256 != candidate.document.sha256
            ):
                raise ReviewError(
                    "STALE_REVIEW_TARGET",
                    "The persisted candidate content does not match its declared hashes.",
                )
            if (
                candidate.reference.review_status is not ReviewStatus.APPROVED
                or candidate.reference.candidate_sha256 != export_request.expected_target_version
            ):
                raise ReviewError(
                    "UNAPPROVED_EXPORT",
                    "Only the exact approved candidate version may be exported.",
                )
            encoded = (
                json.dumps(
                    candidate.document.content,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode()
        else:
            inventory_version = self._repository.get_inventory_version(export_request.target_id)
            if inventory_version is None:
                raise ReviewError("INVENTORY_NOT_FOUND", "The inventory was not found.")
            view = self.build_inventory_review(
                BuildInventoryReviewInput(inventory_id=export_request.target_id)
            )
            version = str(inventory_version)
            if export_request.expected_target_version != version:
                raise ReviewError("STALE_REVIEW_TARGET", "The report target version is stale.")
            encoded = (
                json.dumps(view.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        self._checkpoint(cancellation)
        digest = sha256(encoded).hexdigest()
        try:
            authorized = self._output_policy.authorize_output(
                export_request.output_root_id,
                export_request.relative_path.root,
                follow_links=False,
            )
        except PolicyViolation as error:
            raise ReviewError("OUTPUT_PATH_DENIED", "The export path is not approved.") from error
        target = Path(authorized.canonical_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                if sha256(target.read_bytes()).hexdigest() == digest:
                    return self._export_receipt(export_request.relative_path, digest, len(encoded))
            except OSError as error:
                raise ReviewError(
                    "OUTPUT_PATH_DENIED", "The export target is unreadable."
                ) from error
            raise ReviewError("OUTPUT_EXISTS", "The export target already exists.")
        temporary = target.with_name(f".{target.name}.{digest[:16]}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._checkpoint(cancellation)
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise ReviewError("OUTPUT_EXISTS", "The export target already exists.") from None
        except ReviewError:
            raise
        except OSError as error:
            raise ReviewError("OUTPUT_PATH_DENIED", "The export could not be written.") from error
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        result = self._export_receipt(export_request.relative_path, digest, len(encoded))
        self._repository.record_audit(
            request_id=result.receipt.export_id,
            actor=ActorRef(actor_id="local-export"),
            tool_id="TOOL-030",
            payload={
                "target_id": str(export_request.target_id),
                "sha256": digest,
                "relative_path": export_request.relative_path.root,
            },
        )
        return result

    def _association_for(self, file_id: UUID | None) -> ManifestAssociation | None:
        if file_id is None:
            return None
        with self._repository.session_factory() as session:
            row = session.scalar(
                select(ManifestAssociationEntity)
                .where(ManifestAssociationEntity.file_id == str(file_id))
                .order_by(ManifestAssociationEntity.score.desc())
                .limit(1)
            )
            if row is None:
                return None
            return ManifestAssociation.model_validate_json(json.dumps(row.payload)).model_copy(
                update={"review_status": ReviewStatus(row.review_status)}
            )

    @staticmethod
    def _file_from_payload(payload: dict[str, object]) -> FileAssetRef:
        return FileAssetRef.model_validate_json(json.dumps(payload))

    @staticmethod
    def _matches(item: InventoryReviewItem, request: BuildInventoryReviewInput) -> bool:
        query = request.query
        if query.search and query.search.casefold() not in item.file.relative_path.root.casefold():
            return False
        if query.categories and (
            item.classification is None or item.classification.category not in query.categories
        ):
            return False
        status = (
            item.association.review_status
            if item.association is not None
            else ReviewStatus.PROPOSED
        )
        return not query.review_statuses or status in query.review_statuses

    @staticmethod
    def _sort(
        items: list[InventoryReviewItem], request: BuildInventoryReviewInput
    ) -> list[InventoryReviewItem]:
        sort_by = request.query.sort_by

        def key(item: InventoryReviewItem) -> str:
            if sort_by == "format":
                return (
                    item.classification.format_id.value
                    if item.classification and item.classification.format_id
                    else ""
                )
            if sort_by == "status":
                return (
                    item.association.review_status.value
                    if item.association
                    else ReviewStatus.PROPOSED.value
                )
            return item.file.relative_path.root.casefold()

        return sorted(items, key=key, reverse=request.query.descending)

    @staticmethod
    def _summaries(values: Iterable[str]) -> tuple[ReviewSummaryCount, ...]:
        counts = Counter(values)
        return tuple(
            ReviewSummaryCount(value=value, count=count) for value, count in sorted(counts.items())
        )

    @staticmethod
    def _checkpoint(cancellation: CancellationCheck | None) -> None:
        if cancellation is not None and cancellation():
            raise ReviewError("CANCELLED", "The review operation was cancelled.")

    @staticmethod
    def _diff(source: dict[str, object], target: dict[str, object]) -> GenerationDiff:
        source_paths = ReviewService._flatten(source)
        target_paths = ReviewService._flatten(target)
        return GenerationDiff(
            changed_pointers=tuple(
                sorted(
                    path
                    for path in source_paths.keys() & target_paths.keys()
                    if source_paths[path] != target_paths[path]
                )
            ),
            added_pointers=tuple(sorted(target_paths.keys() - source_paths.keys())),
            removed_pointers=tuple(sorted(source_paths.keys() - target_paths.keys())),
        )

    @staticmethod
    def _flatten(value: object, pointer: str = "") -> dict[str, object]:
        if isinstance(value, dict):
            output: dict[str, object] = {}
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                output.update(ReviewService._flatten(child, f"{pointer}/{escaped}"))
            return output
        if isinstance(value, list):
            output = {}
            for index, child in enumerate(value):
                output.update(ReviewService._flatten(child, f"{pointer}/{index}"))
            return output
        return {pointer: value}

    @staticmethod
    def _document_sha256(content: Mapping[str, object]) -> str:
        encoded = (
            json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode()
        return sha256(encoded).hexdigest()

    @staticmethod
    def _export_receipt(
        relative_path: WorkspaceRelativePath, digest: str, size: int
    ) -> ExportToolOutput:
        from datetime import UTC, datetime

        export_id = uuid5(NAMESPACE_URL, f"export:{relative_path.root}:{digest}")
        return ExportToolOutput(
            receipt=ExportReceipt(
                export_id=export_id,
                files=(
                    ExportedFileRef(
                        relative_path=relative_path,
                        sha256=digest,
                        size_bytes=size,
                    ),
                ),
                exported_at=datetime.now(UTC),
            )
        )


__all__ = ["ReviewError", "ReviewService"]
