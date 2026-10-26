from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from order.app.database import Base
from shared.outbox import OutboxEventMixin


class OrderDB(Base):
    """Stored order intent, keyed by the reference published to the broker."""

    __tablename__ = "orders"

    order_ref: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pricing_source: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    # PENDING -> CONFIRMED / FAILED once stock feedback arrives.
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OutboxEventDB(OutboxEventMixin, Base):
    """Events committed atomically with the order that produced them."""

    __tablename__ = "outbox_events"
