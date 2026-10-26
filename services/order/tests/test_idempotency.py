"""Tests for the Redis idempotency middleware."""

import asyncio
from typing import Any

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from order.app.middleware import REPLAY_HEADER, RedisIdempotencyMiddleware

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
CART = {"customer_id": "cust-1", "items": [{"sku": "BAN-001", "qty": 2}]}
OTHER_CART = {"customer_id": "cust-1", "items": [{"sku": "BAN-001", "qty": 5}]}


class FakeRedis:
    """Only the three commands the middleware uses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail

    def _guard(self) -> None:
        if self.fail:
            raise RedisConnectionError("redis is down")

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> Any:
        self._guard()
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        self._guard()
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self._guard()
        self.store.pop(key, None)


def build_client(
    redis: FakeRedis,
    calls: list[dict[str, Any]],
    *,
    delay: float = 0.0,
    status_code: int = 202,
) -> httpx.AsyncClient:
    """An ASGI client whose order endpoint records how often it really ran."""

    async def create_order(request: Request) -> JSONResponse:
        calls.append(await request.json())
        if delay:
            await asyncio.sleep(delay)
        return JSONResponse({"order_ref": "web-abc123"}, status_code=status_code)

    async def list_orders(request: Request) -> JSONResponse:
        calls.append({})
        return JSONResponse({"orders": []})

    app = Starlette(
        routes=[
            Route("/orders", create_order, methods=["POST"]),
            Route("/orders", list_orders, methods=["GET"]),
        ]
    )
    middleware = RedisIdempotencyMiddleware(app, redis_url="redis://127.0.0.1:6379/15")
    # Replace the lazily-created real client: no Redis server needed.
    middleware.redis = redis  # type: ignore[assignment]

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),  # type: ignore[arg-type]
        base_url="http://order-service",
    )


def post(client: httpx.AsyncClient, key: str | None, body: dict[str, Any]) -> Any:
    headers = {IDEMPOTENCY_HEADER: key} if key else {}
    return client.post("/orders", json=body, headers=headers)


async def test_concurrent_duplicates_execute_once_and_replay_the_same_body() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls, delay=0.3)

    async with client:
        first, second = await asyncio.gather(
            post(client, "burst-key", CART),
            post(client, "burst-key", CART),
        )

    assert len(calls) == 1
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    replay_headers = [REPLAY_HEADER in response.headers for response in (first, second)]
    # Exactly one request did the work; the other was told it is a replay.
    assert sorted(replay_headers) == [False, True]


async def test_sequential_retry_replays_without_re_executing() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls)

    async with client:
        original = await post(client, "retry-key", CART)
        retried = await post(client, "retry-key", CART)

    assert len(calls) == 1
    assert retried.status_code == original.status_code
    assert retried.json() == original.json()
    assert retried.headers[REPLAY_HEADER] == "true"


async def test_reusing_a_key_for_a_different_cart_is_a_conflict() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls)

    async with client:
        await post(client, "reused-key", CART)
        conflicting = await post(client, "reused-key", OTHER_CART)

    assert len(calls) == 1
    assert conflicting.status_code == 409
    assert "different order payload" in conflicting.json()["detail"]


async def test_different_keys_are_independent_orders() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls)

    async with client:
        await post(client, "key-a", CART)
        await post(client, "key-b", CART)

    assert len(calls) == 2


async def test_requests_without_a_key_are_not_deduplicated() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls)

    async with client:
        await post(client, None, CART)
        await post(client, None, CART)

    assert len(calls) == 2


async def test_a_503_releases_the_key_so_the_client_can_retry() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls, status_code=503)

    async with client:
        first = await post(client, "outage-key", CART)
        second = await post(client, "outage-key", CART)

    # "Prices temporarily unavailable" is transient: caching it would block a
    # retry for the whole TTL.
    assert first.status_code == second.status_code == 503
    assert len(calls) == 2
    assert REPLAY_HEADER not in second.headers


async def test_redis_outage_fails_open() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(fail=True), calls)

    async with client:
        first = await post(client, "shared-key", CART)
        second = await post(client, "shared-key", CART)

    # The database primary key still protects the order; intake must not 500
    # just because the cache is unavailable.
    assert first.status_code == second.status_code == 202
    assert len(calls) == 2


async def test_reads_are_not_intercepted() -> None:
    calls: list[dict[str, Any]] = []
    client = build_client(FakeRedis(), calls)

    async with client:
        response = await client.get("/orders")

    assert response.status_code == 200
    assert len(calls) == 1


async def test_a_failed_request_releases_the_key_for_a_retry() -> None:
    calls: list[dict[str, Any]] = []
    redis = FakeRedis()

    async def flaky(request: Request) -> JSONResponse:
        calls.append(await request.json())
        if len(calls) == 1:
            raise RuntimeError("handler blew up")
        return JSONResponse({"order_ref": "web-abc123"}, status_code=202)

    app = Starlette(routes=[Route("/orders", flaky, methods=["POST"])])
    middleware = RedisIdempotencyMiddleware(app, redis_url="redis://127.0.0.1:6379/15")
    middleware.redis = redis  # type: ignore[assignment]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),  # type: ignore[arg-type]
        base_url="http://order-service",
    ) as client:
        with pytest.raises(RuntimeError):
            await post(client, "crash-key", CART)

        # The in-flight marker must not outlive the crashed request.
        assert "idempotency:crash-key" not in redis.store

        response = await post(client, "crash-key", CART)

    assert response.status_code == 202
    assert len(calls) == 2
