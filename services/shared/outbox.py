"""Transactional outbox shared by the services.

Publishing to RabbitMQ inside a request handler leaves a gap: the database commit
can succeed and the process can die before the message reaches the broker (or the
broker can be down), so an order exists that nobody will ever allocate.

The outbox removes that gap:

1. The business change and an ``outbox_events`` row are written in **one**
   PostgreSQL transaction. Either both exist or neither does.
2. A background publisher reads unpublished rows, publishes them to the topic
   exchange and marks them published.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import JSON, DateTime, Integer, String, Uuid, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from shared.config import get_shared_settings
from shared.queue import RabbitMQManager

logger = structlog.get_logger("outbox")


class OutboxEventMixin:
    """Columns for an event waiting to be published.

    Each service declares ``class OutboxEventDB(OutboxEventMixin, Base)`` so the
    outbox lives in that service's own database, next to the data it describes.
    """

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


async def publish_pending_events(
    session_factory: async_sessionmaker[AsyncSession],
    model: Any,
    manager: RabbitMQManager,
    *,
    limit: int,
) -> int:
    """Publish a batch of unpublished events, marking each one after the broker acks.

    Returns the number of events published. Stops at the first failure so ordering
    is preserved; the row stays unpublished and is retried on the next cycle.
    """
    published = 0

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(model)
                    .where(model.published_at.is_(None))
                    .order_by(model.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        for row in rows:
            try:
                await manager.publish_event(
                    routing_key=row.routing_key,
                    payload_bytes=json.dumps(row.payload).encode(),
                )
            except Exception:
                row.attempts += 1
                await session.commit()
                logger.exception(
                    "Outbox publish failed; event will be retried",
                    event_id=str(row.id),
                    routing_key=row.routing_key,
                    attempts=row.attempts,
                )
                break

            row.published_at = datetime.now(UTC)
            row.attempts += 1
            # Commit per event: a later failure must not republish the ones that
            # already reached the broker.
            await session.commit()
            published += 1

    return published


async def purge_published_events(
    session_factory: async_sessionmaker[AsyncSession],
    model: Any,
    *,
    retention_seconds: int,
) -> None:
    """Trim delivered events so the table does not grow without bound."""
    cutoff = datetime.now(UTC) - timedelta(seconds=retention_seconds)

    async with session_factory() as session:
        await session.execute(
            delete(model).where(
                model.published_at.is_not(None),
                model.published_at < cutoff,
            )
        )
        await session.commit()


async def run_outbox_publisher(
    session_factory: async_sessionmaker[AsyncSession],
    model: Any,
    manager: RabbitMQManager,
) -> None:
    """Publish pending events until the task is cancelled."""
    settings = get_shared_settings()
    last_cleanup = 0.0
    loop = asyncio.get_running_loop()

    while True:
        try:
            await publish_pending_events(
                session_factory,
                model,
                manager,
                limit=settings.OUTBOX_BATCH_SIZE,
            )

            now = loop.time()
            if now - last_cleanup > 60:
                await purge_published_events(
                    session_factory,
                    model,
                    retention_seconds=settings.OUTBOX_RETENTION_SECONDS,
                )
                last_cleanup = now

        except asyncio.CancelledError:
            raise
        except Exception:
            # A database or broker blip must not kill the publisher task.
            logger.exception("Outbox cycle failed; retrying")

        await asyncio.sleep(settings.OUTBOX_POLL_INTERVAL_SECONDS)
