"""Durable run receipts and warning provenance, separate from source filesystem state."""

from copy import deepcopy

from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentic_osdu.state.database import StateDatabase


class CloudBase(DeclarativeBase):
    pass


class RunEntity(CloudBase):
    __tablename__ = "foundry_exploration_run"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(36), index=True)
    completed_stage: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)


class SqlLedger:
    def __init__(self, database: StateDatabase) -> None:
        self.database = database
        CloudBase.metadata.create_all(database.engine)

    def get(self, run_id: str) -> dict | None:
        with self.database.session_factory() as session:
            row = session.get(RunEntity, run_id)
            return deepcopy(row.payload) if row is not None else None

    def save(self, state: dict) -> None:
        with self.database.session_factory.begin() as session:
            session.merge(
                RunEntity(
                    run_id=state["request"]["run_id"],
                    instance_id=state["instance_id"],
                    completed_stage=state["completed_stage"],
                    status=state["status"],
                    payload=deepcopy(state),
                )
            )

    def has_active(self, instance_id: str) -> bool:
        with self.database.session_factory() as session:
            return (
                session.scalar(
                    select(RunEntity.run_id)
                    .where(
                        RunEntity.instance_id == instance_id,
                        RunEntity.completed_stage < 6,
                        RunEntity.status != "blocked",
                    )
                    .limit(1)
                )
                is not None
            )
