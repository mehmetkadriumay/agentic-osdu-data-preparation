from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from agentic_osdu.domain.models import (
    ActorRef,
    DataCategory,
    LearningModelStatus,
    ManifestJsonDocument,
    WorkspaceRelativePath,
)
from agentic_osdu.operations import create_database_backup, restore_database_backup
from agentic_osdu.schemas.catalog import SchemaCatalogStore
from agentic_osdu.state.database import create_sqlite_state
from agentic_osdu.state.models import LearningModelVersionEntity
from agentic_osdu.state.repositories import StateRepository
from agentic_osdu.tools.contracts import (
    LearningModelContract,
    LearningModelMutation,
    LearningMutationAction,
    LocalSchemaCatalogImport,
    PersistLearningModelInput,
    SchemaCatalogSource,
    SchemaChecksum,
)

ROOT = Path(__file__).parents[3]
ZERO_HASH = "0" * 64


def test_database_backup_restore_rehearsal(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    backup = tmp_path / "backups" / "state.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('usable-before')")
        connection.commit()

    receipt = create_database_backup(database, backup)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE marker SET value = 'broken-after'")
        connection.commit()

    restore_database_backup(backup, database)
    with closing(sqlite3.connect(database)) as connection:
        value = connection.execute("SELECT value FROM marker").fetchone()

    assert value == ("usable-before",)
    assert receipt.sha256 == sha256(backup.read_bytes()).hexdigest()
    assert receipt.integrity_check == "ok"


def test_database_migration_downgrade_restore_rehearsal(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    backup = tmp_path / "state.backup.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    create_database_backup(database, backup)

    command.downgrade(config, "base")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='workspace'"
        ).fetchone() == (0,)

    restore_database_backup(backup, database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='workspace'"
        ).fetchone() == (1,)


def _catalog_request(source: Path, revision: str, required: bool) -> LocalSchemaCatalogImport:
    relative = "manifest/Manifest.1.0.0.json"
    payload = json.dumps(
        {"schema": {"type": "object", **({"required": ["kind"]} if required else {})}}
    ).encode()
    target = source / Path(relative)
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    return LocalSchemaCatalogImport(
        source=SchemaCatalogSource.LOCAL_EXPORT,
        revision=revision,
        local_root=str(source),
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath(relative),
                sha256=sha256(payload).hexdigest(),
            ),
        ),
    )


def test_schema_catalog_activation_rollback_rehearsal(tmp_path: Path) -> None:
    store = SchemaCatalogStore(tmp_path / "cache")
    prior = store.import_local(_catalog_request(tmp_path / "prior", "prior", False))
    current = store.import_local(_catalog_request(tmp_path / "current", "current", True))

    assert store.active_catalog().schema_catalog_id == current.schema_catalog_id
    restored = store.activate(prior.schema_catalog_id)

    assert restored.schema_catalog_id == prior.schema_catalog_id
    assert store.active_catalog().schema_catalog_id == prior.schema_catalog_id


def _learning_model(category: DataCategory) -> LearningModelContract:
    return LearningModelContract(
        learning_model_id=uuid4(),
        category=category,
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


def test_learning_model_activation_rollback_preserves_history(tmp_path: Path) -> None:
    state = create_sqlite_state(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    from agentic_osdu.state.models import Base

    Base.metadata.create_all(state.engine)
    repository = StateRepository(state.session_factory)
    actor = ActorRef(actor_id="rollback-rehearsal")
    prior = _learning_model(DataCategory.WELL_LOG)
    current = _learning_model(DataCategory.WELL_LOG)

    for model in (prior, current):
        created = repository.persist_learning_model(
            request_id=uuid4(),
            actor=actor,
            request=PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.CREATE,
                    model=model,
                    expected_version=0,
                )
            ),
        )
        repository.persist_learning_model(
            request_id=uuid4(),
            actor=actor,
            request=PersistLearningModelInput(
                mutation=LearningModelMutation(
                    action=LearningMutationAction.ACTIVATE,
                    learning_model_id=model.learning_model_id,
                    expected_version=created.version,
                )
            ),
        )

    latest_prior = repository.list_learning_model_versions(prior.learning_model_id)[-1]
    restored = repository.persist_learning_model(
        request_id=uuid4(),
        actor=actor,
        request=PersistLearningModelInput(
            mutation=LearningModelMutation(
                action=LearningMutationAction.ACTIVATE,
                learning_model_id=prior.learning_model_id,
                expected_version=latest_prior,
            )
        ),
    )

    assert restored.status == LearningModelStatus.ACTIVE
    with state.session_factory() as session:
        rows = session.scalars(
            select(LearningModelVersionEntity).order_by(LearningModelVersionEntity.id)
        ).all()
        assert len(rows) >= 6
        assert rows[-1].learning_model_id == str(prior.learning_model_id)
        assert rows[-1].status == LearningModelStatus.ACTIVE.value
    state.dispose()
