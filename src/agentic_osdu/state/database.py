"""SQLAlchemy state boundaries for local SQLite and hosted PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True, slots=True)
class StateDatabase:
    engine: Engine
    session_factory: sessionmaker[Session]

    def dispose(self) -> None:
        self.engine.dispose()


def create_sqlite_state(url: str, **engine_options: Any) -> StateDatabase:
    """Create a SQLite state boundary with foreign keys and WAL enabled."""

    if not url.startswith("sqlite"):
        raise ValueError("EPIC-007 state requires a SQLite URL")
    engine = create_engine(url, future=True, **engine_options)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return StateDatabase(
        engine=engine,
        session_factory=sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        ),
    )


def create_state(url: str, **engine_options: Any) -> StateDatabase:
    """Keep SQLite policy local and require an explicit PostgreSQL driver."""

    driver = make_url(url).drivername
    if driver in {"sqlite", "sqlite+pysqlite"}:
        return create_sqlite_state(url, **engine_options)
    if driver != "postgresql+psycopg":
        raise ValueError("State requires sqlite or postgresql+psycopg")
    engine_options.setdefault("pool_pre_ping", True)
    engine = create_engine(url, **engine_options)
    return StateDatabase(
        engine=engine,
        session_factory=sessionmaker(
            engine, expire_on_commit=False, autoflush=False, class_=Session
        ),
    )


__all__ = ["StateDatabase", "create_sqlite_state", "create_state"]
