"""Durable receipts, including uncertain attempts; file capabilities remain local."""

from copy import deepcopy

from sqlalchemy import JSON, BigInteger, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentic_osdu.state.database import StateDatabase


class ReceiptBase(DeclarativeBase):
    pass


class ReceiptEntity(ReceiptBase):
    __tablename__ = "mcp_operation_receipt"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    inventory_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state_version: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)


class SqlLedger:
    def __init__(self, database: StateDatabase) -> None:
        self.database = database
        ReceiptBase.metadata.create_all(database.engine)

    def get(self, request_id: str) -> dict | None:
        with self.database.session_factory() as session:
            row = session.get(ReceiptEntity, request_id)
            return deepcopy(row.payload) if row else None

    def save(self, receipt: dict) -> None:
        persisted = (
            receipt["operation"] == "persist_observed_inventory"
            and receipt["status"] == "succeeded"
        )
        result = receipt.get("result", {})
        with self.database.session_factory.begin() as session:
            session.merge(
                ReceiptEntity(
                    request_id=receipt["request_id"],
                    inventory_id=result.get("inventory_id") if persisted else None,
                    state_version=result["output"]["state_version"] if persisted else None,
                    payload=deepcopy(receipt),
                )
            )

    def inventory_receipt(self, inventory_id: str) -> dict | None:
        with self.database.session_factory() as session:
            row = session.scalar(
                select(ReceiptEntity)
                .where(ReceiptEntity.inventory_id == inventory_id)
                .order_by(ReceiptEntity.state_version.desc())
                .limit(1)
            )
            return deepcopy(row.payload) if row else None
