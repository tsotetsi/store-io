"""Unit tests for price resolution.

The warehouse call is the one hard dependency intake has on stock, and the brief
requires orders to keep being accepted during a brief outage. These tests pin the
three outcomes that matter: live price, last-known price, and an honest failure
when neither exists.
"""

from collections.abc import Callable

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from order.app.config import get_order_settings
from order.app.pricing import (
    CACHE_KEY_PREFIX,
    PricedLine,
    PricingUnavailableError,
    UnknownSkuError,
    price_items,
)
from order.app.schemas import OrderItemSchema

settings = get_order_settings()

ItemList = list[OrderItemSchema]


def items(*lines: tuple[str, int]) -> ItemList:
    return [OrderItemSchema(sku=sku, qty=qty) for sku, qty in lines]


class FakePriceCache:
    """Minimal stand-in for ``redis.asyncio.Redis`` used by the price cache."""

    def __init__(
        self,
        initial: dict[str, str] | None = None,
        *,
        fail_on_read: bool = False,
        fail_on_write: bool = False,
    ) -> None:
        self.data = dict(initial or {})
        self.fail_on_read = fail_on_read
        self.fail_on_write = fail_on_write
        self.writes: list[tuple[str, int, int | None]] = []

    async def get(self, key: str) -> str | None:
        if self.fail_on_read:
            raise RedisConnectionError("cache unavailable")
        return self.data.get(key)

    async def set(self, key: str, value: int, ex: int | None = None) -> None:
        if self.fail_on_write:
            raise RedisConnectionError("cache unavailable")
        self.data[key] = str(value)
        self.writes.append((key, int(value), ex))


def cached(sku: str, price_cents: int) -> dict[str, str]:
    return {f"{CACHE_KEY_PREFIX}:{sku}": str(price_cents)}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def catalogue(prices: dict[str, int]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve prices by SKU from ``/inventory/{sku}``; unknown SKUs are 404s."""

    def handler(request: httpx.Request) -> httpx.Response:
        sku = request.url.path.rsplit("/", 1)[-1]
        if sku not in prices:
            return httpx.Response(httpx.codes.NOT_FOUND)
        return httpx.Response(
            httpx.codes.OK,
            json={"sku": sku, "price_cents": prices[sku], "stock": 10},
        )

    return handler


def warehouse_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


async def test_prices_from_the_warehouse_and_caches_the_result() -> None:
    cache = FakePriceCache()

    async with client_for(catalogue({"BAN-001": 199, "APP-002": 250})) as client:
        lines, total, source = await price_items(
            items(("BAN-001", 2), ("BAN-001", 1), ("APP-002", 1)),
            client=client,
            cache=cache,  # type: ignore[arg-type] - structural stand-in for Redis
            settings=settings,
        )

    # Repeated SKUs are merged, so the warehouse is asked once per product.
    assert lines == [
        PricedLine(sku="BAN-001", qty=3, unit_price_cents=199, line_total_cents=597),
        PricedLine(sku="APP-002", qty=1, unit_price_cents=250, line_total_cents=250),
    ]
    assert total == 847
    assert source == "live"
    assert cache.writes == [
        (f"{CACHE_KEY_PREFIX}:BAN-001", 199, settings.PRICE_CACHE_TTL_SECONDS),
        (f"{CACHE_KEY_PREFIX}:APP-002", 250, settings.PRICE_CACHE_TTL_SECONDS),
    ]


async def test_unknown_sku_is_a_client_error_not_an_outage() -> None:
    cache = FakePriceCache(initial=cached("NOPE-999", 100))

    async with client_for(catalogue({"BAN-001": 199})) as client:
        with pytest.raises(UnknownSkuError) as excinfo:
            await price_items(
                items(("NOPE-999", 1)),
                client=client,
                cache=cache,  # type: ignore[arg-type]
                settings=settings,
            )

    # A 404 must not silently fall back to a cached price for a removed product.
    assert excinfo.value.sku == "NOPE-999"
    assert cache.writes == []


async def test_outage_falls_back_to_the_last_known_price() -> None:
    cache = FakePriceCache(initial=cached("BAN-001", 189))

    async with client_for(warehouse_down) as client:
        lines, total, source = await price_items(
            items(("BAN-001", 2)),
            client=client,
            cache=cache,  # type: ignore[arg-type]
            settings=settings,
        )

    assert lines == [PricedLine(sku="BAN-001", qty=2, unit_price_cents=189, line_total_cents=378)]
    assert total == 378
    assert source == "cached"


async def test_outage_without_a_cached_price_refuses_to_invent_one() -> None:
    cache = FakePriceCache()

    async with client_for(warehouse_down) as client:
        with pytest.raises(PricingUnavailableError) as excinfo:
            await price_items(
                items(("BAN-001", 1)),
                client=client,
                cache=cache,  # type: ignore[arg-type]
                settings=settings,
            )

    assert excinfo.value.sku == "BAN-001"
    assert cache.writes == []


async def test_malformed_price_response_is_treated_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            httpx.codes.OK,
            json={"sku": "BAN-001", "price_cents": "one hundred"},
        )

    async with client_for(handler) as client:
        with pytest.raises(PricingUnavailableError):
            await price_items(
                items(("BAN-001", 1)),
                client=client,
                cache=FakePriceCache(),  # type: ignore[arg-type]
                settings=settings,
            )


async def test_a_redis_outage_never_blocks_live_pricing() -> None:
    cache = FakePriceCache(fail_on_read=True, fail_on_write=True)

    async with client_for(catalogue({"BAN-001": 199})) as client:
        _, total, source = await price_items(
            items(("BAN-001", 1)),
            client=client,
            cache=cache,  # type: ignore[arg-type]
            settings=settings,
        )

    assert total == 199
    assert source == "live"


async def test_a_corrupt_cache_entry_is_ignored() -> None:
    cache = FakePriceCache(initial={f"{CACHE_KEY_PREFIX}:BAN-001": "not-a-number"})

    async with client_for(warehouse_down) as client:
        with pytest.raises(PricingUnavailableError):
            await price_items(
                items(("BAN-001", 1)),
                client=client,
                cache=cache,  # type: ignore[arg-type]
                settings=settings,
            )
