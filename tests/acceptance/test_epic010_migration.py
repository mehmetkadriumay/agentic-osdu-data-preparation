from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

import agentic_osdu.migration.legacy_state as legacy_state
from agentic_osdu.migration.legacy_state import LegacyMigrationError, LegacyStateMigrator
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import (
    AuditEventEntity,
    Base,
    FileAssetEntity,
    InventoryEntity,
    LearningExampleEntity,
    LearningModelVersionEntity,
    ManifestAssociationEntity,
)


def _write_legacy_state(root: Path) -> tuple[Path, Path]:
    root = root / "legacy-copy"
    root.mkdir()
    inventory = root / "classification-inventory.json"
    learning = root / "manifest-learning.json"
    inventory.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAt": "2026-09-05T01:01:07+00:00",
                "count": 2,
                "totalBytes": 30,
                "selectedRoot": r"C:\legacy\Volve",
                "selectedDataDirectory": r"C:\legacy\Volve\Data",
                "selectedManifestDirectory": r"C:\legacy\Volve\Manifests",
                "futureInventoryField": {"retained": True},
                "files": [
                    {
                        "path": "well/a.las",
                        "filename": "a.las",
                        "extension": ".las",
                        "sizeBytes": 10,
                        "modified": "2026-09-01T00:00:00+00:00",
                        "format": "LAS unknown",
                        "category": "Well log",
                        "subtype": "Petrophysical/composite log",
                        "dimensions": "1D wellbore",
                        "stack": None,
                        "domain": "Measured depth",
                        "processing": None,
                        "survey": None,
                        "well": "F-1",
                        "details": {"curveCount": 2},
                        "osduKind": "work-product-component--WellLog",
                        "confidence": "high",
                        "evidence": ["LAS markers detected."],
                        "futureFileField": "preserve-me",
                        "manifests": [
                            {
                                "path": "work-products/a.json",
                                "filename": "a.json",
                                "score": 100,
                                "matchMethod": "Exact path in Data.Datasets",
                            }
                        ],
                    },
                    {
                        "path": "seismic/b.segy",
                        "filename": "b.segy",
                        "extension": ".segy",
                        "sizeBytes": 20,
                        "modified": "2026-09-01T00:00:00+00:00",
                        "format": "SEG-Y",
                        "category": "Seismic trace data",
                        "subtype": "Migrated",
                        "dimensions": "3D",
                        "stack": "Post-stack",
                        "domain": "Time",
                        "processing": "Migrated",
                        "survey": "3D",
                        "well": None,
                        "details": {},
                        "osduKind": "work-product-component--SeismicTraceData",
                        "confidence": "high",
                        "evidence": ["SEG-Y header detected."],
                        "manifests": [
                            {
                                "path": "generated/seismic/generated_b.segy.json",
                                "filename": "generated_b.segy.json",
                                "score": 100,
                                "matchMethod": "Generated from learned pattern",
                            }
                        ],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    learning.write_text(
        json.dumps(
            {
                "trainedAt": "2026-09-05T00:00:00+00:00",
                "roots": [r"C:\legacy\Volve"],
                "futureLearningField": {"retained": True},
                "categories": {
                    "Well log": {
                        "category": "Well log",
                        "exampleCount": 2,
                        "componentKind": "osdu:wks:work-product-component--WellLog:1.1.0",
                        "prototypeManifest": "work-products/a.json",
                        "futureModelField": {"retained": True},
                        "examples": [
                            {
                                "dataPath": "well/a.las",
                                "manifestPath": "work-products/a.json",
                                "document": {
                                    "kind": "osdu:wks:Manifest:1.0.0",
                                    "Data": {},
                                },
                            },
                            {
                                "dataPath": "seismic/b.segy",
                                "manifestPath": "generated/seismic/generated_b.segy.json",
                                "document": {
                                    "kind": "osdu:wks:Manifest:1.0.0",
                                    "Data": {},
                                    "x-agentic-osdu-generation": {"policy": "legacy"},
                                },
                            },
                        ],
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return inventory, learning


def test_item_048_import_is_read_only_idempotent_and_reports_field_outcomes(
    tmp_path: Path,
) -> None:
    inventory_path, learning_path = _write_legacy_state(tmp_path)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (inventory_path, learning_path)
    }
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    migrator = LegacyStateMigrator(
        database.session_factory,
        legacy_root=inventory_path.parent,
    )

    first = migrator.import_files(inventory_path, learning_path)
    replay = migrator.import_files(inventory_path, learning_path)

    assert replay == first
    assert first.adapter_version == "1.0.0"
    assert first.counts.accepted >= 4
    assert first.counts.transformed >= 2
    assert first.counts.skipped == 1
    assert first.counts.failed == 0
    assert first.source_unchanged is True
    assert {item.status for item in first.field_outcomes} >= {"transformed", "retained", "skipped"}
    assert any(item.field == "futureInventoryField" for item in first.field_outcomes)
    assert first.source_extensions["inventory"]["futureInventoryField"] == {"retained": True}
    assert first.source_extensions["learning"]["futureLearningField"] == {"retained": True}
    assert any("generated" in item.reason.casefold() for item in first.field_outcomes)
    assert all(path.read_bytes() == content for path, (content, _) in before.items())
    assert all(path.stat().st_mtime_ns == mtime for path, (_, mtime) in before.items())

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InventoryEntity)) == 1
        assert session.scalar(select(func.count()).select_from(FileAssetEntity)) == 2
        assert session.scalar(select(func.count()).select_from(ManifestAssociationEntity)) == 2
        assert session.scalar(select(func.count()).select_from(LearningExampleEntity)) == 1
        assert session.scalar(select(func.count()).select_from(LearningModelVersionEntity)) == 1
        model = session.scalar(select(LearningModelVersionEntity))
        assert model is not None
        assert model.status == "inactive"
        assert model.payload["legacy_extensions"]["futureModelField"] == {"retained": True}
        audit = session.scalar(select(AuditEventEntity))
        assert audit is not None
        assert audit.payload["report"]["source_extensions"]["inventory"][
            "futureInventoryField"
        ] == {"retained": True}
    database.dispose()


def test_item_048_rejects_database_output_inside_legacy_root(tmp_path: Path) -> None:
    from agentic_osdu.migration.legacy_state import migrate_to_sqlite

    inventory_path, learning_path = _write_legacy_state(tmp_path)
    with pytest.raises(LegacyMigrationError, match="MIGRATION_OUTPUT_DENIED"):
        migrate_to_sqlite(
            inventory_path,
            learning_path,
            inventory_path.parent / ".state" / "migrated.db",
            legacy_root=inventory_path.parent,
        )


def test_item_048_rejects_session_factory_bound_inside_legacy_root(tmp_path: Path) -> None:
    inventory_path, _ = _write_legacy_state(tmp_path)
    database = create_sqlite_state(f"sqlite:///{inventory_path.parent / 'bypass.db'}")
    try:
        with pytest.raises(LegacyMigrationError, match="MIGRATION_OUTPUT_DENIED"):
            LegacyStateMigrator(
                database.session_factory,
                legacy_root=inventory_path.parent,
            )
    finally:
        database.dispose()


def test_item_048_rejects_unsupported_inventory_without_partial_state(tmp_path: Path) -> None:
    inventory_path, learning_path = _write_legacy_state(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["schemaVersion"] = 2
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)

    with pytest.raises(LegacyMigrationError, match="LEGACY_SCHEMA_UNSUPPORTED"):
        LegacyStateMigrator(
            database.session_factory,
            legacy_root=inventory_path.parent,
        ).import_files(inventory_path, learning_path)

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InventoryEntity)) == 0
    database.dispose()


def test_item_048_source_change_rolls_back_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, learning_path = _write_legacy_state(tmp_path)
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    real_source_hash = legacy_state._source_hash
    calls = 0

    def mutate_before_verification(path: Path) -> legacy_state.SourceHash:
        nonlocal calls
        calls += 1
        if calls == 3:
            inventory_path.write_text(
                inventory_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
        return real_source_hash(path)

    monkeypatch.setattr(legacy_state, "_source_hash", mutate_before_verification)

    with pytest.raises(LegacyMigrationError, match="LEGACY_SOURCE_CHANGED"):
        LegacyStateMigrator(
            database.session_factory,
            legacy_root=inventory_path.parent,
        ).import_files(
            inventory_path,
            learning_path,
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InventoryEntity)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEventEntity)) == 0
    database.dispose()


def test_item_048_fails_closed_on_invalid_records_and_retains_learning_extensions(
    tmp_path: Path,
) -> None:
    inventory_path, learning_path = _write_legacy_state(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["files"].append({"not": "a file"})
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    learning = json.loads(learning_path.read_text(encoding="utf-8"))
    learning["futureTopLevel"] = {"retained": True}
    learning_path.write_text(json.dumps(learning), encoding="utf-8")
    database = create_sqlite_state(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(database.engine)
    migrator = LegacyStateMigrator(
        database.session_factory,
        legacy_root=inventory_path.parent,
    )

    with pytest.raises(LegacyMigrationError, match="LEGACY_RECORD_FAILED"):
        migrator.import_files(inventory_path, learning_path)

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InventoryEntity)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEventEntity)) == 0
    database.dispose()
