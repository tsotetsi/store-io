"""Tests for the transactional outbox publisher."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import StaticPool

from shared.constants import EVENT_ORDER_PLACED
from shared.outbox import OutboxEventMixin, publish_pending_events, purge_published_events

Base = declarative_base()


class OutboxEventDB(OutboxEventMixin, Base):  # type: ignore[misc, valid-type]
    """Concrete outbox table, shaped exactly like the services' own."""

    __tablename__ = "outbox_events"


class FakeBroker:
    """Stand-in for ``RabbitMQManager`` that records what would be published."""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, routing_key: str, payload_bytes: bytes) -> None:
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("broker unavailable")
        self.published.append((routing_key, json.loads(payload_bytes)))


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[misc]

    await engine.dispose()


async def add_event(
    session_factory: async_sessionmaker[AsyncSession],
    order_ref: str,
    *,
    created_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            OutboxEventDB(
                routing_key=EVENT_ORDER_PLACED,
                payload={"order_ref": order_ref, "items": [{"sku": "BAN-001", "qty": 1}]},
                created_at=created_at or datetime.now(UTC),
            )
        )
        await session.commit()


async def stored_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[OutboxEventDB]:
    async with session_factory() as session:
        rows = await session.execute(select(OutboxEventDB).order_by(OutboxEventDB.created_at))
        return list(rows.scalars())


async def test_publishes_pending_events_and_marks_them_published(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    await add_event(session_factory, "order-1", created_at=now)
    await add_event(session_factory, "order-2", created_at=now + timedelta(seconds=1))
    broker = FakeBroker()

    published = await publish_pending_events(session_factory, OutboxEventDB, broker, limit=10)  # type: ignore[arg-type]

    assert published == 2
    assert [payload["order_ref"] for _, payload in broker.published] == ["order-1", "order-2"]
    assert [routing_key for routing_key, _ in broker.published] == [EVENT_ORDER_PLACED] * 2

    events = await stored_events(session_factory)
    assert all(event.published_at is not None for event in events)
    assert all(event.attempts == 1 for event in events)

    # A second cycle must not redeliver what was already confirmed.
    assert await publish_pending_events(session_factory, OutboxEventDB, broker, limit=10) == 0  # type: ignore[arg-type]
    assert len(broker.published) == 2


async def test_broker_failure_keeps_the_event_pending_for_a_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_event(session_factory, "order-1")
    broker = FakeBroker(fail_on_call=1)

    published = await publish_pending_events(session_factory, OutboxEventDB, broker, limit=10)  # type: ignore[arg-type]

    # Failure is reported by return value, not by raising: the publisher loop
    # must survive a broker blip instead of dying.
    assert published == 0
    [event] = await stored_events(session_factory)
    assert event.published_at is None
    assert event.attempts == 1

    # Once the broker is back the same row is delivered.
    broker.fail_on_call = None
    assert await publish_pending_events(session_factory, OutboxEventDB, broker, limit=10) == 1  # type: ignore[arg-type]
    [event] = await stored_events(session_factory)
    assert event.published_at is not None
    assert event.attempts == 2


async def test_a_failure_stops_the_batch_so_ordering_is_preserved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    await add_event(session_factory, "order-1", created_at=now)
    await add_event(session_factory, "order-2", created_at=now + timedelta(seconds=1))
    await add_event(session_factory, "order-3", created_at=now + timedelta(seconds=2))
    broker = FakeBroker(fail_on_call=2)

    published = await publish_pending_events(session_factory, OutboxEventDB, broker, limit=10)  # type: ignore[arg-type]

    assert published == 1
    assert [payload["order_ref"] for _, payload in broker.published] == ["order-1"]

    events = await stored_events(session_factory)
    assert [event.published_at is not None for event in events] == [True, False, False]
    # The untouched third event was not attempted at all.
    assert [event.attempts for event in events] == [1, 1, 0]


async def test_batch_size_limits_one_cycle_without_losing_the_rest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    for index in range(3):
        await add_event(
            session_factory, f"order-{index}", created_at=now + timedelta(seconds=index)
        )
    broker = FakeBroker()

    assert await publish_pending_events(session_factory, OutboxEventDB, broker, limit=2) == 2  # type: ignore[arg-type]
    assert [payload["order_ref"] for _, payload in broker.published] == ["order-0", "order-1"]

    assert await publish_pending_events(session_factory, OutboxEventDB, broker, limit=2) == 1  # type: ignore[arg-type]
    assert [payload["order_ref"] for _, payload in broker.published] == [
        "order-0",
        "order-1",
        "order-2",
    ]


async def test_purge_removes_only_old_published_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)

    async with session_factory() as session:
        session.add_all(
            [
                OutboxEventDB(
                    routing_key=EVENT_ORDER_PLACED,
                    payload={"order_ref": "old-published"},
                    created_at=now - timedelta(hours=2),
                    published_at=now - timedelta(hours=2),
                ),
                OutboxEventDB(
                    routing_key=EVENT_ORDER_PLACED,
                    payload={"order_ref": "recent-published"},
                    created_at=now,
                    published_at=now,
                ),
                OutboxEventDB(
                    routing_key=EVENT_ORDER_PLACED,
                    payload={"order_ref": "old-pending"},
                    created_at=now - timedelta(hours=2),
                ),
            ]
        )
        await session.commit()

    await purge_published_events(session_factory, OutboxEventDB, retention_seconds=3600)  # type: ignore[arg-type]

    remaining = await stored_events(session_factory)
    assert sorted(event.payload["order_ref"] for event in remaining) == [
        "old-pending",
        "recent-published",
    ]
