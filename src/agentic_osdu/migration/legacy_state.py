"""Versioned, read-only import of copied Volve JSON state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatCandidate,
    FormatDetectionResult,
    FormatId,
    OSDUKind,
    ProcessingLevel,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import (
    AuditEventEntity,
    Base,
    ClassificationEntity,
    FileAssetEntity,
    FormatDetectionEntity,
    InventoryEntity,
    LearningExampleEntity,
    LearningModelVersionEntity,
    ManifestAssociationEntity,
    WorkspaceEntity,
)

ADAPTER_VERSION = "1.0.0"
_KNOWN_INVENTORY = {
    "schemaVersion",
    "generatedAt",
    "count",
    "totalBytes",
    "categories",
    "formats",
    "files",
    "sources",
    "sourceCount",
    "manifestSummary",
    "selectedRoot",
    "selectedDataDirectory",
    "selectedManifestDirectory",
    "root",
}
_KNOWN_FILE = {
    "path",
    "filename",
    "extension",
    "sizeBytes",
    "modified",
    "format",
    "category",
    "subtype",
    "dimensions",
    "stack",
    "domain",
    "processing",
    "survey",
    "well",
    "details",
    "osduKind",
    "confidence",
    "evidence",
    "manifests",
    "sourceId",
    "recordId",
    "baseDirectory",
    "dataDirectory",
    "manifestDirectory",
}
_KNOWN_MODEL = {
    "category",
    "exampleCount",
    "sourceDataFiles",
    "manifestKind",
    "workProductKind",
    "componentKind",
    "datasetKind",
    "fileSourcePrefix",
    "workProductEnvelope",
    "componentEnvelope",
    "datasetEnvelope",
    "workProductConstants",
    "componentConstants",
    "prototypeManifest",
    "osduKind",
    "examples",
}
_KNOWN_LEARNING = {"trainedAt", "roots", "categories"}


class LegacyMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class MigrationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: int = 0
    transformed: int = 0
    skipped: int = 0
    failed: int = 0


class SourceHash(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    sha256: str
    size_bytes: int


class FieldOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    path: str
    field: str
    status: Literal["transformed", "retained", "skipped", "failed"]
    reason: str


class LegacyMigrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    import_id: UUID
    adapter_version: str
    inventory_id: UUID
    source_hashes: tuple[SourceHash, ...]
    source_extensions: dict[str, Any]
    counts: MigrationCounts
    field_outcomes: tuple[FieldOutcome, ...]
    source_unchanged: bool


class LegacyStateMigrator:
    """Import immutable JSON snapshots into normalized, inactive target state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        legacy_root: Path,
    ) -> None:
        _assert_output_outside_legacy_root(
            legacy_root,
            _bound_database_path(session_factory),
        )
        self.session_factory = session_factory
        self.legacy_root = legacy_root.resolve()

    def import_files(self, inventory_path: Path, learning_path: Path) -> LegacyMigrationReport:
        sources = (inventory_path.resolve(), learning_path.resolve())
        if any(
            path != self.legacy_root and self.legacy_root not in path.parents for path in sources
        ):
            raise LegacyMigrationError(
                "LEGACY_SOURCE_DENIED",
                "Copied legacy state must be located under the trusted legacy root.",
            )
        before = tuple(_source_hash(path) for path in sources)
        inventory = _read_object(sources[0], "inventory")
        learning = _read_object(sources[1], "learning")
        if inventory.get("schemaVersion") != 1:
            raise LegacyMigrationError(
                "LEGACY_SCHEMA_UNSUPPORTED", "Only legacy inventory schema version 1 is supported."
            )
        if not isinstance(inventory.get("files"), list):
            raise LegacyMigrationError("LEGACY_STATE_INVALID", "Inventory files must be a list.")
        if not isinstance(learning.get("categories"), dict):
            raise LegacyMigrationError(
                "LEGACY_STATE_INVALID", "Learning categories must be an object."
            )
        source_key = ":".join(item.sha256 for item in before)
        import_id = uuid5(NAMESPACE_URL, f"legacy-state:{ADAPTER_VERSION}:{source_key}")
        inventory_id = uuid5(import_id, "inventory")
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(AuditEventEntity).where(AuditEventEntity.request_id == str(import_id))
            )
            if existing is not None:
                return LegacyMigrationReport.model_validate(existing.payload["report"])
            report = self._import(session, inventory, learning, before, import_id, inventory_id)
            if report.counts.failed:
                raise LegacyMigrationError(
                    "LEGACY_RECORD_FAILED",
                    f"{report.counts.failed} legacy records could not be imported.",
                )
            after = tuple(_source_hash(path) for path in sources)
            if before != after:
                raise LegacyMigrationError(
                    "LEGACY_SOURCE_CHANGED",
                    "A legacy source changed during the read-only import.",
                )
            session.add(
                AuditEventEntity(
                    id=str(uuid5(import_id, "audit")),
                    request_id=str(import_id),
                    actor={"actor_id": "legacy-state-migrator", "display_name": None},
                    tool_id="TOOL-022",
                    tool_version=ADAPTER_VERSION,
                    result_status="succeeded",
                    side_effects=[{"kind": "state_mutation", "target": str(inventory_id)}],
                    payload={"report": report.model_dump(mode="json")},
                    occurred_at=datetime.now(UTC),
                )
            )
        return report.model_copy(update={"source_unchanged": True})

    def _import(
        self,
        session: Session,
        inventory: dict[str, Any],
        learning: dict[str, Any],
        source_hashes: tuple[SourceHash, ...],
        import_id: UUID,
        inventory_id: UUID,
    ) -> LegacyMigrationReport:
        outcomes: list[FieldOutcome] = []
        accepted = transformed = skipped = failed = 0
        root = str(inventory.get("selectedRoot") or inventory.get("root") or r"C:\legacy")
        workspace_id = uuid5(import_id, f"workspace:{root.casefold()}")
        policy_hash = sha256(f"{root}|read-only".encode()).hexdigest()
        session.add(
            WorkspaceEntity(
                id=str(workspace_id),
                canonical_root=root,
                read_only=True,
                allowed_output_subpaths=[],
                policy_fingerprint=policy_hash,
                version=1,
                created_actor={"actor_id": "legacy-state-migrator", "display_name": None},
                created_at=datetime.now(UTC),
            )
        )
        now = _legacy_datetime(inventory.get("generatedAt"))
        session.add(
            InventoryEntity(
                id=str(inventory_id),
                state_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        source_extensions = {
            "inventory": {key: inventory[key] for key in sorted(set(inventory) - _KNOWN_INVENTORY)},
            "learning": {key: learning[key] for key in sorted(set(learning) - _KNOWN_LEARNING)},
        }
        for source, extensions in source_extensions.items():
            for key in extensions:
                outcomes.append(_outcome(source, "/", key, "retained", "Stored in extensions."))
        file_ids: dict[str, UUID] = {}
        manifest_ids: dict[str, UUID] = {}
        for index, raw in enumerate(inventory["files"]):
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                failed += 1
                outcomes.append(
                    _outcome("inventory", f"/files/{index}", "*", "failed", "Invalid file record.")
                )
                continue
            relative = raw["path"].replace("\\", "/").lstrip("/")
            try:
                path = WorkspaceRelativePath(relative)
            except ValueError:
                failed += 1
                outcomes.append(
                    _outcome(
                        "inventory",
                        f"/files/{index}",
                        "path",
                        "failed",
                        "Unsafe workspace-relative path.",
                    )
                )
                continue
            file_id = uuid5(import_id, f"file:{relative.casefold()}")
            file_ids[relative.casefold()] = file_id
            record_hash = _json_hash(raw)
            file = FileAssetRef(
                file_id=file_id,
                workspace_id=workspace_id,
                relative_path=path,
                size_bytes=max(0, int(raw.get("sizeBytes") or 0)),
                modified_at=_legacy_datetime(raw.get("modified")),
                sha256=None,
                discovery_version=1,
            )
            extensions = {key: raw[key] for key in sorted(set(raw) - _KNOWN_FILE)}
            session.add(
                FileAssetEntity(
                    id=str(file_id),
                    inventory_id=str(inventory_id),
                    workspace_id=str(workspace_id),
                    relative_path=relative,
                    size_bytes=file.size_bytes,
                    modified_at=file.modified_at,
                    sha256=None,
                    discovery_version=1,
                    payload={
                        **file.model_dump(mode="json"),
                        "legacy_record_sha256": record_hash,
                        "legacy_details": raw.get("details", {}),
                        "legacy_extensions": extensions,
                    },
                )
            )
            session.flush()
            detection, classification = _classification(import_id, file, raw)
            session.add(
                FormatDetectionEntity(
                    id=str(detection.detection_id),
                    file_id=str(file_id),
                    detector_version=detection.detector_version,
                    active=True,
                    payload=detection.model_dump(mode="json"),
                    created_at=now,
                )
            )
            session.add(
                ClassificationEntity(
                    id=str(classification.classification_id),
                    file_id=str(file_id),
                    detection_id=str(detection.detection_id),
                    active=True,
                    payload={
                        **classification.model_dump(mode="json"),
                        "_legacy": {
                            "format": raw.get("format"),
                            "category": raw.get("category"),
                            "subtype": raw.get("subtype"),
                            "dimensions": raw.get("dimensions"),
                            "evidence": raw.get("evidence", []),
                            "extensions": extensions,
                        },
                    },
                    created_at=now,
                )
            )
            accepted += 1
            transformed += 1
            outcomes.append(
                _outcome(
                    "inventory",
                    f"/files/{index}",
                    "classification",
                    "transformed",
                    "Mapped legacy labels to typed target enums.",
                )
            )
            for key in extensions:
                outcomes.append(
                    _outcome(
                        "inventory",
                        f"/files/{index}",
                        key,
                        "retained",
                        "Stored in file legacy_extensions.",
                    )
                )
            for match_index, match in enumerate(raw.get("manifests") or []):
                if not isinstance(match, dict) or not isinstance(match.get("path"), str):
                    skipped += 1
                    outcomes.append(
                        _outcome(
                            "inventory",
                            f"/files/{index}/manifests/{match_index}",
                            "*",
                            "skipped",
                            "Invalid association summary.",
                        )
                    )
                    continue
                manifest_path = match["path"].replace("\\", "/").lstrip("/")
                manifest_id = manifest_ids.setdefault(
                    manifest_path.casefold(),
                    uuid5(import_id, f"manifest:{manifest_path.casefold()}"),
                )
                association_id = uuid5(
                    import_id, f"association:{relative.casefold()}:{manifest_path.casefold()}"
                )
                session.add(
                    ManifestAssociationEntity(
                        id=str(association_id),
                        state_version=1,
                        file_id=str(file_id),
                        manifest_id=str(manifest_id),
                        score=min(1.0, max(0.0, float(match.get("score") or 0) / 100.0)),
                        method=_method(match.get("matchMethod")),
                        evidence_ids=[],
                        review_status="proposed",
                        trust_level="heuristic",
                        target_version=None,
                        payload={"legacy": match},
                    )
                )
                accepted += 1
        categories = learning["categories"]
        for category_name, raw_model in sorted(categories.items()):
            if not isinstance(raw_model, dict):
                failed += 1
                outcomes.append(
                    _outcome(
                        "learning",
                        f"/categories/{category_name}",
                        "*",
                        "failed",
                        "Invalid model.",
                    )
                )
                continue
            model_id = uuid5(import_id, f"learning-model:{str(category_name).casefold()}")
            model_extensions = {
                key: raw_model[key] for key in sorted(set(raw_model) - _KNOWN_MODEL)
            }
            imported_examples = 0
            for example_index, example in enumerate(raw_model.get("examples") or []):
                if not isinstance(example, dict):
                    skipped += 1
                    continue
                data_path = str(example.get("dataPath") or "").replace("\\", "/").lstrip("/")
                manifest_path = (
                    str(example.get("manifestPath") or "").replace("\\", "/").lstrip("/")
                )
                generated = manifest_path.casefold().startswith("generated/") or bool(
                    isinstance(example.get("document"), dict)
                    and example["document"].get("x-agentic-osdu-generation")
                )
                pointer = f"/categories/{category_name}/examples/{example_index}"
                if generated:
                    skipped += 1
                    outcomes.append(
                        _outcome(
                            "learning",
                            pointer,
                            "example",
                            "skipped",
                            "Generated manifests are ineligible learning examples.",
                        )
                    )
                    continue
                source_file_id = file_ids.get(
                    data_path.casefold(), uuid5(import_id, f"file:{data_path.casefold()}")
                )
                manifest_id = manifest_ids.get(
                    manifest_path.casefold(),
                    uuid5(import_id, f"manifest:{manifest_path.casefold()}"),
                )
                association_id = uuid5(
                    import_id, f"association:{data_path.casefold()}:{manifest_path.casefold()}"
                )
                session.add(
                    LearningExampleEntity(
                        id=str(uuid5(import_id, f"example:{category_name}:{example_index}")),
                        source_file_id=str(source_file_id),
                        manifest_id=str(manifest_id),
                        association_id=str(association_id),
                        source_sha256=_json_hash({"path": data_path}),
                        manifest_sha256=_json_hash(
                            example.get("document") or {"path": manifest_path}
                        ),
                        review_status="proposed",
                        generated_manifest=False,
                    )
                )
                imported_examples += 1
                accepted += 1
            session.add(
                LearningModelVersionEntity(
                    learning_model_id=str(model_id),
                    category=_category(category_name).value,
                    version=1,
                    status="inactive",
                    model_sha256=_json_hash(raw_model),
                    payload={
                        "legacy_adapter_version": ADAPTER_VERSION,
                        "legacy_model": raw_model,
                        "legacy_extensions": model_extensions,
                        "imported_example_count": imported_examples,
                        "activation_required": True,
                    },
                    recorded_at=_legacy_datetime(learning.get("trainedAt")),
                )
            )
            accepted += 1
            if model_extensions:
                for key in model_extensions:
                    outcomes.append(
                        _outcome(
                            "learning",
                            f"/categories/{category_name}",
                            key,
                            "retained",
                            "Stored in model legacy_extensions.",
                        )
                    )
        counts = MigrationCounts(
            accepted=accepted,
            transformed=transformed,
            skipped=skipped,
            failed=failed,
        )
        return LegacyMigrationReport(
            import_id=import_id,
            adapter_version=ADAPTER_VERSION,
            inventory_id=inventory_id,
            source_hashes=source_hashes,
            source_extensions=source_extensions,
            counts=counts,
            field_outcomes=tuple(outcomes),
            source_unchanged=True,
        )


def _source_hash(path: Path) -> SourceHash:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise LegacyMigrationError(
            "LEGACY_STATE_UNAVAILABLE", "Legacy state could not be read."
        ) from error
    return SourceHash(name=path.name, sha256=sha256(payload).hexdigest(), size_bytes=len(payload))


def migrate_to_sqlite(
    inventory_path: Path,
    learning_path: Path,
    database_path: Path,
    *,
    legacy_root: Path,
) -> LegacyMigrationReport:
    """Create or reuse a target database and import copied legacy state."""

    resolved_database = database_path.resolve()
    _assert_output_outside_legacy_root(legacy_root, resolved_database)
    resolved_database.parent.mkdir(parents=True, exist_ok=True)
    database = create_sqlite_state(f"sqlite:///{resolved_database}")
    try:
        Base.metadata.create_all(database.engine)
        return LegacyStateMigrator(
            database.session_factory,
            legacy_root=legacy_root,
        ).import_files(
            inventory_path,
            learning_path,
        )
    finally:
        database.dispose()


def _assert_output_outside_legacy_root(legacy_root: Path, database_path: Path) -> None:
    protected_root = legacy_root.resolve()
    resolved_database = database_path.resolve()
    if resolved_database == protected_root or protected_root in resolved_database.parents:
        raise LegacyMigrationError(
            "MIGRATION_OUTPUT_DENIED",
            "The target database must not be created inside the trusted legacy root.",
        )


def _bound_database_path(session_factory: sessionmaker[Session]) -> Path:
    bind = session_factory.kw.get("bind")
    database = getattr(getattr(bind, "url", None), "database", None)
    if not isinstance(database, str) or not database or database == ":memory:":
        raise LegacyMigrationError(
            "MIGRATION_DATABASE_INVALID",
            "Migration requires a file-backed SQLite session factory.",
        )
    return Path(database)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LegacyMigrationError(
            "LEGACY_STATE_INVALID", f"The copied legacy {label} is not valid JSON."
        ) from error
    if not isinstance(value, dict):
        raise LegacyMigrationError(
            "LEGACY_STATE_INVALID", f"The copied legacy {label} must be an object."
        )
    return value


def _json_hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _legacy_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=UTC)


def _outcome(
    source: str,
    path: str,
    field: str,
    status: Literal["transformed", "retained", "skipped", "failed"],
    reason: str,
) -> FieldOutcome:
    return FieldOutcome(source=source, path=path, field=field, status=status, reason=reason)


def _format(raw: dict[str, Any]) -> FormatId | None:
    label = str(raw.get("format") or "").casefold()
    extension = str(raw.get("extension") or "").casefold()
    mapping = (
        ("json well log", FormatId.JSON_WELL_LOG),
        ("seg-y", FormatId.SEGY),
        ("las", FormatId.LAS),
        ("dlis", FormatId.DLIS),
        ("lis", FormatId.LIS_LTI),
        ("csv", FormatId.CSV),
        ("p1/90", FormatId.P190),
        ("sgp", FormatId.SGP),
        ("fault", FormatId.DAT),
        ("horizon", FormatId.DAT),
        ("pdf", FormatId.PDF),
        ("text", FormatId.TEXT),
        ("ascii", FormatId.TEXT),
    )
    for token, format_id in mapping:
        if token in label:
            return format_id
    return {
        ".segy": FormatId.SEGY,
        ".sgy": FormatId.SEGY,
        ".las": FormatId.LAS,
        ".json": FormatId.JSON_WELL_LOG,
        ".dlis": FormatId.DLIS,
        ".lis": FormatId.LIS_LTI,
        ".lti": FormatId.LIS_LTI,
        ".csv": FormatId.CSV,
        ".p190": FormatId.P190,
        ".sgp": FormatId.SGP,
        ".dat": FormatId.DAT,
        ".txt": FormatId.TEXT,
        ".asc": FormatId.TEXT,
        ".pdf": FormatId.PDF,
    }.get(extension)


def _category(value: object) -> DataCategory:
    normalized = str(value or "").casefold()
    if "seismic trace" in normalized or "velocity" in normalized:
        return DataCategory.SEISMIC
    if normalized in {"well log", "checkshot", "well trajectory", "well marker"}:
        return DataCategory.WELL_LOG
    if "navigation" in normalized:
        return DataCategory.NAVIGATION
    if "grid" in normalized:
        return DataCategory.GRID
    if "horizon" in normalized or "fault" in normalized:
        return DataCategory.INTERPRETATION
    return DataCategory.SUPPORTING_DOCUMENT


def _classification(
    namespace: UUID,
    file: FileAssetRef,
    raw: dict[str, Any],
) -> tuple[FormatDetectionResult, ClassificationRecord]:
    format_id = _format(raw)
    confidence = {"high": 1.0, "medium": 0.6, "low": 0.25}.get(
        str(raw.get("confidence") or "").casefold(), 0.5
    )
    detection_id = uuid5(namespace, f"detection:{file.relative_path.root.casefold()}")
    detection = FormatDetectionResult(
        detection_id=detection_id,
        file_id=file.file_id,
        candidates=(
            ()
            if format_id is None
            else (FormatCandidate(format_id=format_id, confidence=confidence, evidence_ids=()),)
        ),
        detector_version=ADAPTER_VERSION,
        detected_at=_legacy_datetime(raw.get("modified")),
    )
    subtype_by_format = {
        FormatId.SEGY: DataSubtype.SEGY,
        FormatId.LAS: DataSubtype.LAS,
        FormatId.JSON_WELL_LOG: DataSubtype.JSON_WELL_LOG,
        FormatId.DLIS: DataSubtype.DLIS,
        FormatId.LIS_LTI: DataSubtype.LIS_LTI,
        FormatId.CSV: DataSubtype.CSV,
        FormatId.P190: DataSubtype.P190,
        FormatId.SGP: DataSubtype.SGP,
        FormatId.DAT: (
            DataSubtype.FAULT
            if "fault" in str(raw.get("category") or "").casefold()
            else DataSubtype.HORIZON
        ),
        FormatId.TEXT: DataSubtype.TEXT,
        FormatId.PDF: DataSubtype.PDF,
    }
    subtype = (
        DataSubtype.UNKNOWN
        if format_id is None
        else subtype_by_format.get(format_id, DataSubtype.UNKNOWN)
    )
    category = _category(raw.get("category"))
    osdu_kind = raw.get("osduKind")
    if isinstance(osdu_kind, str) and osdu_kind.count(":") != 3:
        osdu_kind = f"osdu:wks:{osdu_kind}:1.0.0"
    classification = ClassificationRecord(
        classification_id=uuid5(namespace, f"classification:{file.relative_path.root.casefold()}"),
        file_id=file.file_id,
        format_id=format_id,
        category=category,
        subtype=subtype,
        dimensions=ClassificationDimensions(),
        stack=_enum_value(StackType, raw.get("stack"), StackType.UNKNOWN),
        domain=_enum_value(DataDomain, raw.get("domain"), DataDomain.UNKNOWN),
        processing=_enum_value(ProcessingLevel, raw.get("processing"), ProcessingLevel.UNKNOWN),
        survey=_enum_value(SurveyType, raw.get("survey"), SurveyType.UNKNOWN),
        well=WellDataType.LOG if category is DataCategory.WELL_LOG else WellDataType.NOT_APPLICABLE,
        osdu_kind=OSDUKind(osdu_kind) if isinstance(osdu_kind, str) else None,
        confidence=confidence,
        detection_id=detection_id,
        extraction_ids=(),
        evidence_ids=(),
        trust_level=TrustLevel.DERIVED,
    )
    return detection, classification


def _enum_value(enum_type: Any, value: object, default: Any) -> Any:
    normalized = str(value or "").casefold().replace("-", "_").replace(" ", "_")
    aliases = {"migrated": "processed", "measured_depth": "depth"}
    normalized = aliases.get(normalized, normalized)
    try:
        return enum_type(normalized)
    except ValueError:
        return default


def _method(value: object) -> str:
    normalized = str(value or "legacy_match").casefold()
    mapping = {
        "exact path in data.datasets": "exact_dataset_path",
        "exact file path in manifest": "exact_path",
        "exact filename in data.datasets": "exact_dataset_filename",
        "exact filename in manifest": "exact_filename",
        "normalized identifier in manifest filename": "normalized_identifier",
        "generated from learned pattern": "generated_legacy",
    }
    return mapping.get(normalized, "legacy_match")
