"""Allocate stock for each ``order.placed`` event and report the outcome back.

Every line item is locked with ``SELECT ... FOR UPDATE`` before stock is deducted,
so two concurrent orders cannot oversell the same SKU. A shortage rolls the
deduction back to a savepoint but keeps the ledger claim: the outcome is final and
must not be re-evaluated on redelivery.
"""

import asyncio
import json

import aio_pika
import aio_pika.abc
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.constants import EVENT_ORDER_PLACED, EVENT_STOCK_ALLOCATED, EVENT_STOCK_FAILED
from shared.queue import RabbitMQManager
from warehouse.app.database import AsyncSessionLocal
from warehouse.app.models import OutboxEventDB, ProcessedOrderDB, ProductInventoryDB

logger = structlog.get_logger("warehouse-consumer")

ORDERS_QUEUE = "warehouse-service.order-placed"


class InsufficientStock(Exception):
    """Raised inside the deduction savepoint so partial work is rolled back."""

    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(sku)


async def process_order_allocation(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    """Reserve stock for every line item, or fail the whole order — exactly once."""
    async with message.process():
        payload = json.loads(message.body.decode())
        order_ref = payload.get("order_ref")
        items = payload.get("items", [])

        logger.info("Checking stock allocation", order_ref=order_ref, item_count=len(items))

        async with AsyncSessionLocal() as db:
            claimed = (
                await db.execute(
                    pg_insert(ProcessedOrderDB)
                    .values(order_ref=order_ref)
                    .on_conflict_do_nothing(index_elements=[ProcessedOrderDB.order_ref])
                    .returning(ProcessedOrderDB.order_ref)
                )
            ).scalar_one_or_none()

            if claimed is None:
                await db.rollback()
                logger.info(
                    "Duplicate order.placed ignored (already processed)",
                    order_ref=order_ref,
                )
                return

            shortage_sku: str | None = None

            try:
                # Savepoint: a shortage releases the locks and stock changes but
                # keeps the ledger claim made above.
                async with db.begin_nested():
                    for item in items:
                        sku = item["sku"]
                        qty = int(item["qty"])

                        # Lock the row so a concurrent order cannot deduct it too.
                        product = (
                            await db.execute(
                                select(ProductInventoryDB)
                                .where(ProductInventoryDB.sku == sku)
                                .with_for_update()
                            )
                        ).scalar_one_or_none()

                        if product is None or product.stock < qty:
                            shortage_sku = sku
                            raise InsufficientStock(sku)

                        product.stock -= qty
            except InsufficientStock:
                pass

            if shortage_sku is not None:
                db.add(
                    OutboxEventDB(
                        routing_key=EVENT_STOCK_FAILED,
                        payload={
                            "order_ref": order_ref,
                            "reason": "OutOfStock",
                            "sku": shortage_sku,
                        },
                    )
                )
                await db.commit()
                logger.warning(
                    "Allocation rejected: insufficient stock",
                    order_ref=order_ref,
                    sku=shortage_sku,
                )
                return

            db.add(
                OutboxEventDB(
                    routing_key=EVENT_STOCK_ALLOCATED,
                    payload={"order_ref": order_ref},
                )
            )
            await db.commit()
            logger.info("Stock reserved; allocation event queued", order_ref=order_ref)


async def start_warehouse_consumers(queue_manager: RabbitMQManager) -> None:
    """Bind the order intake queue and consume until the task is cancelled."""
    await queue_manager.connect()

    connection = queue_manager.connection
    exchange = queue_manager.exchange
    if connection is None or exchange is None:
        raise RuntimeError("RabbitMQ connection is not available")

    channel = await connection.channel()
    await channel.set_qos(prefetch_count=5)  # Stock checks are DB-bound; keep it low.

    queue = await channel.declare_queue(ORDERS_QUEUE, durable=True)
    await queue.bind(exchange, routing_key=EVENT_ORDER_PLACED)

    logger.info("Warehouse order intake consumer active", queue=ORDERS_QUEUE)

    await queue.consume(process_order_allocation)

    # Keep consumption open indefinitely while the application runs.
    await asyncio.Future()
