"""Listen for downstream stock feedback (stock.allocated / stock.failed).

The Warehouse Service reports back through the topic exchange, which is how an
order moves out of PENDING without the Order Service ever polling it.
"""

import asyncio
import json

import aio_pika
import aio_pika.abc
import structlog
from sqlalchemy import select

from order.app.database import AsyncSessionLocal
from order.app.models import OrderDB, OutboxEventDB
from shared.constants import EVENT_ORDER_CONFIRMED, EVENT_STOCK_ALLOCATED, EVENT_STOCK_FAILED
from shared.queue import RabbitMQManager

logger = structlog.get_logger("order-consumer")

FEEDBACK_QUEUE = "order-service.stock-feedback"
FEEDBACK_ROUTING_KEYS = (EVENT_STOCK_ALLOCATED, EVENT_STOCK_FAILED)


async def process_stock_allocation_feedback(
    message: aio_pika.abc.AbstractIncomingMessage,
) -> None:
    """Apply a stock feedback event to the stored order."""
    async with message.process():
        payload = json.loads(message.body.decode())
        order_ref = payload.get("order_ref")
        routing_key = message.routing_key

        logger.info("Received stock feedback", order_ref=order_ref, routing_key=routing_key)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(OrderDB).where(OrderDB.order_ref == order_ref))
            order = result.scalar_one_or_none()

            if order is None:
                # Nothing to update; acknowledging drops the poison message
                # instead of redelivering it forever.
                logger.error("Order not found for feedback event", order_ref=order_ref)
                return

            if order.status != "PENDING":
                # Feedback is at-least-once, so a redelivered allocation must not
                # publish a second order.confirmed event or flip a final state.
                logger.info(
                    "Ignoring duplicate stock feedback",
                    order_ref=order_ref,
                    status=order.status,
                    routing_key=routing_key,
                )
                return

            if routing_key == EVENT_STOCK_ALLOCATED:
                order.status = "CONFIRMED"
                # Hand state ownership to the next stage through the outbox, so the
                # status change and the event commit together.
                db.add(
                    OutboxEventDB(
                        routing_key=EVENT_ORDER_CONFIRMED,
                        payload={"order_ref": order_ref, "customer_id": order.customer_id},
                    )
                )
                await db.commit()
                logger.info("Order confirmed", order_ref=order_ref)
            elif routing_key == EVENT_STOCK_FAILED:
                order.status = "FAILED"
                await db.commit()
                logger.error("Order failed: stock unavailable", order_ref=order_ref)


async def start_order_consumers(queue_manager: RabbitMQManager) -> None:
    """Bind the feedback queue and consume until the task is cancelled."""
    await queue_manager.connect()

    connection = queue_manager.connection
    exchange = queue_manager.exchange
    if connection is None or exchange is None:
        raise RuntimeError("RabbitMQ connection is not available")

    # A dedicated channel keeps consumer prefetch from throttling publishers.
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)  # Protect this instance from flooding.

    queue = await channel.declare_queue(FEEDBACK_QUEUE, durable=True)
    for routing_key in FEEDBACK_ROUTING_KEYS:
        await queue.bind(exchange, routing_key=routing_key)

    logger.info("Order Service feedback consumer active", queue=FEEDBACK_QUEUE)

    await queue.consume(process_stock_allocation_feedback)

    # Keep consumption open indefinitely while the application runs.
    await asyncio.Future()
