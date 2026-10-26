import aio_pika
import aio_pika.abc
import structlog

from shared.config import get_shared_settings
from shared.constants import EXCHANGE_NAME, EXCHANGE_TYPE

settings = get_shared_settings()
logger = structlog.get_logger(__name__)


class RabbitMQManager:
    """
    RabbitMQ connection and channel manager.

    Provides async abstraction for connecting to RabbitMQ. Handles
    non-blocking and guarantees robust exchange registration.
    """

    def __init__(self):
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractRobustChannel | None = None
        self.exchange: aio_pika.abc.AbstractRobustExchange | None = None

    async def connect(self):
        """
        Establishes an async connection that auto-recovers from network glitches.
        """
        if not self.connection or self.connection.is_closed:
            connection = await aio_pika.connect_robust(settings.rabbitmq_url)
            channel = await connection.channel()

            # Declare the main topic exchange for core system choreography.
            exchange = await channel.declare_exchange(
                name=EXCHANGE_NAME,
                type=EXCHANGE_TYPE,
                durable=True,  # Survived broker restarts.
            )

            self.connection = connection
            self.channel = channel
            self.exchange = exchange
            logger.info("Robust RabbitMQ exchange declared", exchange_name=EXCHANGE_NAME)

    async def publish_event(self, routing_key: str, payload_bytes: bytes):
        """
        Publishes durable message packets safely across nodes.

        :param routing_key: The routing key to use for dispatch the event.
        :param payload_bytes: The serialized payload of the event.
        """
        if not self.exchange:
            await self.connect()

        exchange = self.exchange
        if exchange is None:
            raise RuntimeError("RabbitMQ exchange is not available")

        message = aio_pika.Message(
            body=payload_bytes,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # Holds message to disk.
        )
        await exchange.publish(message, routing_key=routing_key)
        logger.info(f"Published event to routing key: {routing_key}")

    async def close(self):
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
