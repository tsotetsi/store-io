"""Price resolution for order intake.

1. Prices are fetched concurrently with a short timeout, so one slow SKU does not
   serialise the whole cart.
2. Every successful lookup is cached in Redis. If the warehouse cannot answer, the
   last known price is used and the order is marked ``pricing_source="cached"``.
   A SKU we have never seen a price for is still rejected with 503, because
   guessing a price is worse than asking the client to retry.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from order.app.config import OrderSettings
from order.app.schemas import OrderItemSchema

logger = structlog.get_logger("order-pricing")

CACHE_KEY_PREFIX = "order:price"


class UnknownSkuError(Exception):
    """The warehouse answered 404: the SKU does not exist, retrying will not help."""

    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(sku)


class PricingUnavailableError(Exception):
    """The warehouse is unreachable and no cached price exists for the SKU."""

    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(sku)


@dataclass(frozen=True, slots=True)
class PricedLine:
    """A line item with the unit price locked in at order time."""

    sku: str
    qty: int
    unit_price_cents: int
    line_total_cents: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "qty": self.qty,
            "unit_price_cents": self.unit_price_cents,
            "line_total_cents": self.line_total_cents,
        }


def _cache_key(sku: str) -> str:
    return f"{CACHE_KEY_PREFIX}:{sku}"


def _aggregate_quantities(items: list[OrderItemSchema]) -> dict[str, int]:
    """Merge repeated SKUs so each one is priced once, keeping first-seen order."""
    quantities: dict[str, int] = {}
    for item in items:
        quantities[item.sku] = quantities.get(item.sku, 0) + item.qty
    return quantities


async def _live_price(
    client: httpx.AsyncClient,
    settings: OrderSettings,
    sku: str,
) -> int:
    """Ask the warehouse for one price, translating failures into domain errors."""
    response = await client.get(
        f"{settings.WAREHOUSE_SERVICE_URL}/inventory/{sku}",
        timeout=settings.WAREHOUSE_TIMEOUT_SECONDS,
    )

    if response.status_code == httpx.codes.NOT_FOUND:
        raise UnknownSkuError(sku)

    response.raise_for_status()
    price = response.json()["price_cents"]

    if not isinstance(price, int) or price < 0:
        raise ValueError(f"Warehouse returned an invalid price for {sku!r}: {price!r}")

    return price


async def _cached_price(cache: aioredis.Redis, sku: str) -> int | None:
    """Read the last known price, tolerating a Redis outage."""
    try:
        raw = await cache.get(_cache_key(sku))
    except RedisError as exc:
        logger.warning("Price cache read failed", sku=sku, error=str(exc))
        return None

    if raw is None:
        return None

    try:
        return int(raw)
    except TypeError, ValueError:
        return None


async def _store_price(cache: aioredis.Redis, sku: str, price_cents: int, ttl: int) -> None:
    try:
        await cache.set(_cache_key(sku), price_cents, ex=ttl)
    except RedisError as exc:
        # Caching is an optimisation: never fail an order because it did not work.
        logger.warning("Price cache write failed", sku=sku, error=str(exc))


async def price_items(
    items: list[OrderItemSchema],
    *,
    client: httpx.AsyncClient,
    cache: aioredis.Redis,
    settings: OrderSettings,
) -> tuple[list[PricedLine], int, str]:
    """Price a cart, returning priced lines, the total and the pricing source.

    Raises:
        UnknownSkuError: a SKU is not in the catalogue (client error, 400).
        PricingUnavailableError: the warehouse is down and no price is cached (503).
    """
    quantities = _aggregate_quantities(items)

    async def resolve(sku: str) -> tuple[int, bool]:
        """Return (unit_price_cents, used_cached_price)."""
        try:
            price = await _live_price(client, settings, sku)
        except UnknownSkuError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            cached = await _cached_price(cache, sku)
            if cached is None:
                logger.error("No live or cached price available", sku=sku, error=str(exc))
                raise PricingUnavailableError(sku) from exc

            logger.warning(
                "Warehouse unreachable; pricing from cache",
                sku=sku,
                error=str(exc),
                cached_price_cents=cached,
            )
            return cached, True

        await _store_price(cache, sku, price, settings.PRICE_CACHE_TTL_SECONDS)
        return price, False

    resolved = await asyncio.gather(*(resolve(sku) for sku in quantities))

    lines = [
        PricedLine(
            sku=sku,
            qty=quantities[sku],
            unit_price_cents=unit_price,
            line_total_cents=unit_price * quantities[sku],
        )
        for sku, (unit_price, _) in zip(quantities, resolved, strict=True)
    ]

    used_cache = any(from_cache for _, from_cache in resolved)
    return lines, sum(line.line_total_cents for line in lines), "cached" if used_cache else "live"
