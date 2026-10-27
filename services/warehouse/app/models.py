from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.outbox import OutboxEventMixin
from warehouse.app.database import Base


class ProductInventoryDB(Base):
    """Inventory row for a single SKU."""

    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProcessedOrderDB(Base):
    """Ledger of handled ``order.placed`` events, keyed by the order reference."""

    __tablename__ = "processed_orders"

    order_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxEventDB(OutboxEventMixin, Base):
    """Stock feedback events committed atomically with the allocation."""

    __tablename__ = "outbox_events"
