from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from agentic_osdu.runtime import create_runtime
from agentic_osdu.state import create_state
from agentic_osdu.state.models import FileAssetEntity, FileSampleEntity


def test_postgresql_uses_the_explicit_driver_without_sqlite_pragmas() -> None:
    engine = MagicMock()
    with patch("agentic_osdu.state.database.create_engine", return_value=engine) as create:
        state = create_state("postgresql+psycopg://user@localhost/inventory")
        create.assert_called_once_with(
            "postgresql+psycopg://user@localhost/inventory", pool_pre_ping=True
        )
        assert state.engine is engine
        state.dispose()
        engine.dispose.assert_called_once()


def test_postgresql_supports_subsurface_file_sizes_and_offsets_over_two_gib() -> None:
    for column in (FileAssetEntity.__table__.c.size_bytes, FileSampleEntity.__table__.c.offset):
        assert isinstance(column.type, BigInteger)
    ddl = str(CreateTable(FileAssetEntity.__table__).compile(dialect=postgresql.dialect()))
    assert "size_bytes BIGINT" in ddl


def test_state_backend_and_runtime_location_must_be_explicit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires sqlite"):
        create_state("postgresql://localhost/inventory")
    with pytest.raises(ValueError, match="not both"):
        create_runtime(tmp_path / "state.db", database_url="sqlite://")
