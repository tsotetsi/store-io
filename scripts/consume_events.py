#!/usr/bin/env python3
"""Print order/stock events from RabbitMQ to stdout.

This is the "another system consumes order-accepted information" surface from the
assignment: a durable queue bound to every lifecycle routing key on the shared
topic exchange. Any team can run it, or copy the pattern into their own worker.

    make events     # or: uv run --with aio-pika==10.1.1 --no-project python scripts/consume_events.py

Events printed:
    order.placed | stock.allocated | order.confirmed | stock.failed
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import aio_pika

ROOT_DIR = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT_DIR / ".secrets"

EXCHANGE_NAME = "store_io_exchange"
QUEUE_NAME = os.getenv("EVENT_FEED_QUEUE", "audit-event-feed")
ROUTING_KEYS = ("order.placed", "stock.allocated", "order.confirmed", "stock.failed")

GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
GREY = "\033[0;90m"
RESET = "\033[0m"


def _read_secret(relative_path: str) -> str | None:
    path = SECRETS_DIR / relative_path
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return None


def rabbitmq_url() -> str:
    """Build the AMQP URL from env vars or the local .secrets/ files."""
    if explicit := os.getenv("RABBITMQ_URL"):
        return explicit

    user = os.getenv("RABBITMQ_USER") or _read_secret("rabbitmq/rabbitmq_user") or "guest"
    password = (
        os.getenv("RABBITMQ_PASSWORD") or _read_secret("rabbitmq/rabbitmq_password") or "guest"
    )
    host = os.getenv("RABBITMQ_HOST", "localhost")
    port = os.getenv("RABBITMQ_PORT", "5672")

    return f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}//"


async def main() -> int:
    url = rabbitmq_url()
    print(f"{GREY}Connecting to RabbitMQ and binding {QUEUE_NAME}...{RESET}")

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=20)

        exchange = await channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        queue = await channel.declare_queue(QUEUE_NAME, durable=True)
        for routing_key in ROUTING_KEYS:
            await queue.bind(exchange, routing_key=routing_key)

        print(f"{GREEN}Listening for {', '.join(ROUTING_KEYS)}. Ctrl+C to stop.{RESET}\n")

        async with queue.iterator() as events:
            async for message in events:
                async with message.process():
                    try:
                        payload = json.loads(message.body.decode())
                    except json.JSONDecodeError:
                        payload = message.body.decode()

                    print(
                        f"{CYAN}{message.routing_key:<16}{RESET} "
                        f"{json.dumps(payload, separators=(',', ':'))}",
                        flush=True,
                    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
