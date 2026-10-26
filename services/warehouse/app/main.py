"""Warehouse Service HTTP API."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.middleware import ChaosMonkeyMiddleware
from shared.outbox import run_outbox_publisher
from shared.queue import RabbitMQManager
from warehouse.app.consumers import start_warehouse_consumers
from warehouse.app.database import AsyncSessionLocal, engine, get_db
from warehouse.app.models import OutboxEventDB, ProductInventoryDB
from warehouse.app.schemas import InventoryResponseSchema, InventoryUpsertSchema

logger = structlog.get_logger("warehouse-api")

rabbitmq_mgr = RabbitMQManager()

DEFAULT_CATALOGUE = (
    ("BAN-001", "Bananas 1kg", 199, 50),
    ("APL-003", "Apples 1kg", 249, 100),
    ("ORG-007", "Oranges 1kg", 229, 40),
)


async def seed_initial_inventory() -> None:
    """Populate a small catalogue the first time the schema is created."""
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(ProductInventoryDB).limit(1))
        if existing.scalar_one_or_none() is not None:
            return

        session.add_all(
            [
                ProductInventoryDB(sku=sku, name=name, price_cents=price_cents, stock=stock)
                for sku, name, price_cents, stock in DEFAULT_CATALOGUE
            ]
        )
        await session.commit()
        logger.info("Seeded baseline inventory", products=len(DEFAULT_CATALOGUE))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Seed the catalogue and boot the broker consumer.

    The schema is owned by Alembic and applied by the container entrypoint
    (``alembic upgrade head``), so startup does not create tables.
    """
    await seed_initial_inventory()

    # Boot the broker, the order intake consumer and the outbox publisher.
    await rabbitmq_mgr.connect()
    consumer_task = asyncio.create_task(start_warehouse_consumers(rabbitmq_mgr))
    outbox_task = asyncio.create_task(
        run_outbox_publisher(AsyncSessionLocal, OutboxEventDB, rabbitmq_mgr)
    )

    try:
        yield
    finally:
        # SHUTDOWN: stop background work before dropping the connection.
        for task in (consumer_task, outbox_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await rabbitmq_mgr.close()
        await engine.dispose()


app = FastAPI(
    title="Store-IO - Warehouse Service",
    description=(
        "Inventory catalogue plus asynchronous stock allocation. Prices and stock "
        "are authoritative here; the Order Service reads them at order time."
    ),
    lifespan=lifespan,
)

app.add_middleware(ChaosMonkeyMiddleware)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness probe used by Docker and Compose health checks."""
    return {"status": "healthy", "service": "warehouse-service"}


@app.get("/inventory", response_model=list[InventoryResponseSchema])
async def list_inventory(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> list[ProductInventoryDB]:
    """List the catalogue. The dashboard uses this to render stock levels."""
    result = await db.execute(
        select(ProductInventoryDB).order_by(ProductInventoryDB.sku).limit(limit)
    )
    return list(result.scalars())


@app.post(
    "/inventory",
    response_model=InventoryResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_product(
    payload: InventoryUpsertSchema,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> ProductInventoryDB:
    """Create or update a product. Idempotent, so the seed script can be re-run."""
    stmt = (
        pg_insert(ProductInventoryDB)
        .values(
            sku=payload.sku,
            name=payload.name,
            price_cents=payload.price_cents,
            stock=payload.stock,
        )
        .on_conflict_do_update(
            index_elements=[ProductInventoryDB.sku],
            set_={
                "name": payload.name,
                "price_cents": payload.price_cents,
                "stock": payload.stock,
            },
        )
        .returning(ProductInventoryDB)
    )
    product = (await db.execute(stmt)).scalar_one()
    await db.commit()

    logger.info("Product upserted", sku=payload.sku, stock=payload.stock)
    return product


@app.get("/inventory/{sku}", response_model=InventoryResponseSchema)
async def get_sku_stock(
    sku: str,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> ProductInventoryDB:
    """Return current stock and price for a single SKU."""
    result = await db.execute(select(ProductInventoryDB).where(ProductInventoryDB.sku == sku))
    product = result.scalar_one_or_none()

    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SKU {sku!r} not found.",
        )

    return product
