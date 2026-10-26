"""Order Service HTTP API.

Accepts order intents, prices them against the Warehouse Service, stores them in
PostgreSQL and publishes an ``order.placed`` event so stock can be allocated. The
stock feedback loop that moves the order to CONFIRMED / FAILED lives in
``order.app.consumers``.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated

import httpx
import redis.asyncio as aioredis
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from order.app.config import get_order_settings
from order.app.consumers import start_order_consumers
from order.app.database import AsyncSessionLocal, engine, get_db
from order.app.intake import intent_signature, resolve_order_ref
from order.app.middleware import REPLAY_HEADER, RedisIdempotencyMiddleware
from order.app.models import OrderDB, OutboxEventDB
from order.app.pricing import PricingUnavailableError, UnknownSkuError, price_items
from order.app.schemas import OrderCreateSchema, OrderResponseSchema
from shared.constants import EVENT_ORDER_PLACED
from shared.middleware import ChaosMonkeyMiddleware
from shared.outbox import run_outbox_publisher
from shared.queue import RabbitMQManager

logger = structlog.get_logger("order-api")

settings = get_order_settings()
rabbitmq_mgr = RabbitMQManager()

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="X-Idempotency-Key",
        description="Optional client key. Retries with the same key replay the first response.",
    ),
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare shared infrastructure, then release it on shutdown."""
    app.state.http = httpx.AsyncClient(timeout=settings.WAREHOUSE_TIMEOUT_SECONDS)
    app.state.price_cache = aioredis.from_url(settings.redis_url, decode_responses=True)

    # Boot the broker, the feedback consumer and the outbox publisher.
    await rabbitmq_mgr.connect()
    consumer_task = asyncio.create_task(start_order_consumers(rabbitmq_mgr))
    outbox_task = asyncio.create_task(
        run_outbox_publisher(AsyncSessionLocal, OutboxEventDB, rabbitmq_mgr)
    )

    try:
        yield
    finally:
        # SHUTDOWN: stop background work before dropping the connections.
        for task in (consumer_task, outbox_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await rabbitmq_mgr.close()
        await app.state.http.aclose()
        await app.state.price_cache.aclose()
        await engine.dispose()


app = FastAPI(
    title="Store-IO - Order Service",
    description=(
        "Order intake with idempotent creation, price snapshots, status lookup and "
        "asynchronous stock allocation through RabbitMQ."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.add_middleware(ChaosMonkeyMiddleware)
app.add_middleware(RedisIdempotencyMiddleware, redis_url=settings.redis_url)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness probe used by Docker and Compose health checks."""
    return {"status": "healthy", "service": "order-service"}


async def _replay_existing_order(
    order_ref: str,
    payload: OrderCreateSchema,
    response: Response,
    db: AsyncSession,
) -> OrderResponseSchema:
    """Return the stored order for a duplicate reference, or reject a conflict."""
    existing = await db.get(OrderDB, order_ref)

    if existing is None:
        # The winning transaction has not committed yet.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {order_ref!r} is being created right now; retry shortly.",
        )

    stored_intent = intent_signature(existing.customer_id, [dict(item) for item in existing.items])
    requested_intent = intent_signature(
        payload.customer_id, [dict(item.model_dump()) for item in payload.items]
    )

    if stored_intent != requested_intent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order reference {order_ref!r} was already used for a different order.",
        )

    logger.info("Duplicate order submission replayed", order_ref=order_ref)
    response.status_code = status.HTTP_200_OK
    response.headers[REPLAY_HEADER] = "true"
    return OrderResponseSchema.model_validate(existing)


@app.post(
    "/orders",
    response_model=OrderResponseSchema,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"description": "Duplicate submission; the stored order is replayed."},
        400: {"description": "Unknown SKU."},
        409: {"description": "The order reference was reused with a different payload."},
        503: {"description": "Prices could not be resolved; retry shortly."},
    },
)
async def create_order(
    payload: OrderCreateSchema,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey = None,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> OrderResponseSchema:
    """Create a new order, price it, and hand it to the warehouse for allocation."""
    order_ref = resolve_order_ref(payload, idempotency_key)

    # A duplicate must be answered from PostgreSQL alone: if the warehouse is
    # unreachable, replaying an existing order must not fail on a price lookup.
    existing = await db.get(OrderDB, order_ref)
    if existing is not None:
        return await _replay_existing_order(order_ref, payload, response, db)

    try:
        priced_lines, total_cents, pricing_source = await price_items(
            payload.items,
            client=request.app.state.http,
            cache=request.app.state.price_cache,
            settings=settings,
        )
    except UnknownSkuError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown SKU {exc.sku!r}.",
        ) from exc
    except PricingUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Prices are temporarily unavailable for SKU {exc.sku!r}; retry shortly. "
                "The order was not created."
            ),
        ) from exc

    priced_items = [line.as_dict() for line in priced_lines]

    insert_stmt = (
        pg_insert(OrderDB)
        .values(
            order_ref=order_ref,
            customer_id=payload.customer_id,
            items=priced_items,
            total_cents=total_cents,
            pricing_source=pricing_source,
            status="PENDING",
        )
        .on_conflict_do_nothing(index_elements=[OrderDB.order_ref])
        .returning(OrderDB)
    )
    created = (await db.execute(insert_stmt)).scalar_one_or_none()

    if created is None:
        # Lost a race with a concurrent submission of the same reference.
        return await _replay_existing_order(order_ref, payload, response, db)

    # The event is written in the same transaction as the order: either both are
    # durable or neither is. The outbox publisher delivers it to RabbitMQ, so a
    # broker outage delays allocation instead of losing it.
    db.add(
        OutboxEventDB(
            routing_key=EVENT_ORDER_PLACED,
            payload={
                "order_ref": order_ref,
                "customer_id": payload.customer_id,
                "items": [{"sku": line.sku, "qty": line.qty} for line in priced_lines],
                "total_cents": total_cents,
            },
        )
    )
    await db.commit()

    logger.info(
        "Order placed",
        order_ref=order_ref,
        total_cents=total_cents,
        pricing_source=pricing_source,
    )

    return OrderResponseSchema.model_validate(created)


@app.get("/orders/{order_ref}", response_model=OrderResponseSchema)
async def get_order(
    order_ref: str,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> OrderResponseSchema:
    """Fetch a single order and its current status."""
    order = await db.get(OrderDB, order_ref)

    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_ref!r} not found.",
        )

    return OrderResponseSchema.model_validate(order)


@app.get("/orders", response_model=list[OrderResponseSchema])
async def list_orders(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency idiom
) -> list[OrderResponseSchema]:
    """List recent orders, newest first. Used by the dashboard and for demos."""
    stmt = select(OrderDB).order_by(OrderDB.created_at.desc()).limit(limit)

    if customer_id:
        stmt = stmt.where(OrderDB.customer_id == customer_id)
    if status_filter:
        stmt = stmt.where(OrderDB.status == status_filter.upper())

    result = await db.execute(stmt)
    return [OrderResponseSchema.model_validate(order) for order in result.scalars()]
