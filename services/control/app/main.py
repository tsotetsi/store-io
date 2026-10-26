"""Operations API behind the Store-IO dashboard.

The dashboard needs three things the domain services cannot provide themselves:
the state of every container, a way to stop/start/restart one service, and the
live log stream. All three are served by the Docker Engine API, which is plain
HTTP over ``/var/run/docker.sock`` - no docker CLI or SDK dependency needed.

This service is developer tooling: socket access is root-equivalent and there is
no authentication. It is bound to 127.0.0.1 in docker-compose.yml and must not be
exposed beyond the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logger = structlog.get_logger("control-api")

COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "store-io")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000").rstrip("/")
WAREHOUSE_SERVICE_URL = os.getenv("WAREHOUSE_SERVICE_URL", "http://warehouse-service:8000").rstrip(
    "/"
)

# Stable ordering for the topology view.
SERVICE_ORDER = [
    "dashboard-service",
    "control-service",
    "order-service",
    "warehouse-service",
    "order-db-service",
    "warehouse-db-service",
    "redis-service",
    "rabbitmq-service",
]

ServiceAction = Literal["start", "stop", "restart"]


class DockerLogDecoder:
    """Decode Docker's 8-byte multiplexed log frames into plain lines."""

    HEADER_SIZE = 8

    def __init__(self) -> None:
        self._frames = b""
        self._text = ""
        self._raw = False

    def feed(self, chunk: bytes) -> list[str]:
        self._frames += chunk
        lines: list[str] = []

        if self._raw:
            lines.extend(self._consume_text(self._frames))
            self._frames = b""
            return lines

        while len(self._frames) >= self.HEADER_SIZE:
            stream_type = self._frames[0]

            # A TTY container sends a raw stream with no frame headers.
            if stream_type not in (0, 1, 2):
                self._raw = True
                lines.extend(self._consume_text(self._frames))
                self._frames = b""
                break

            size = int.from_bytes(self._frames[4:8], "big")
            if len(self._frames) < self.HEADER_SIZE + size:
                break

            payload = self._frames[self.HEADER_SIZE : self.HEADER_SIZE + size]
            self._frames = self._frames[self.HEADER_SIZE + size :]
            lines.extend(self._consume_text(payload))

        return lines

    def flush(self) -> list[str]:
        lines: list[str] = []
        if self._frames:
            lines.extend(self._consume_text(self._frames))
            self._frames = b""
        if self._text:
            lines.append(self._text)
            self._text = ""
        return [line for line in lines if line]

    def _consume_text(self, payload: bytes) -> list[str]:
        self._text += payload.decode("utf-8", errors="replace")
        parts = self._text.split("\n")
        self._text = parts.pop()
        return [part for part in parts if part]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the two HTTP clients: one for Docker, one for the services."""
    app.state.docker = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET),
        base_url="http://docker",
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    app.state.upstream = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

    try:
        yield
    finally:
        await app.state.docker.aclose()
        await app.state.upstream.aclose()


app = FastAPI(
    title="Store-IO - Control Service",
    description="Local operations API: container state, lifecycle actions, log streams and read proxies.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _filters() -> str:
    return json.dumps({"label": [f"com.docker.compose.project={COMPOSE_PROJECT}"]})


async def _list_containers() -> list[dict[str, Any]]:
    docker: httpx.AsyncClient = app.state.docker
    response = await docker.get("/containers/json", params={"all": "1", "filters": _filters()})

    if response.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Docker daemon returned {response.status_code}: {response.text[:200]}",
        )

    return response.json()


async def _resolve_container(name: str) -> dict[str, Any]:
    """Find the container for a compose service name."""
    for container in await _list_containers():
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.service") == name:
            return container

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No container found for service {name!r} in project {COMPOSE_PROJECT!r}.",
    )


async def _inspect(container_id: str) -> dict[str, Any]:
    docker: httpx.AsyncClient = app.state.docker
    response = await docker.get(f"/containers/{container_id}/json")

    if response.status_code != status.HTTP_200_OK:
        return {}

    payload: dict[str, Any] = response.json()
    return payload


async def _describe(container: dict[str, Any]) -> dict[str, Any]:
    labels = container.get("Labels") or {}
    inspect = await _inspect(container["Id"])
    state = inspect.get("State") or {}
    running = container.get("State") == "running"
    # Health only means something while the container runs; a stopped container
    # keeps the last health value forever.
    health = (state.get("Health") or {}).get("Status") if running else None

    ports = [
        {
            "private": port.get("PrivatePort"),
            "public": port.get("PublicPort"),
            "type": port.get("Type"),
        }
        for port in container.get("Ports") or []
        if port.get("PublicPort") is not None
    ]
    # Docker lists one entry per address family; keep the first per mapping.
    ports = list({(port["public"], port["private"], port["type"]): port for port in ports}.values())

    return {
        "name": labels.get("com.docker.compose.service"),
        "container": (container.get("Names") or ["?"])[0].lstrip("/"),
        "id": container["Id"][:12],
        "image": container.get("Image"),
        "state": container.get("State"),
        "status": container.get("Status"),
        "health": health,
        "started_at": state.get("StartedAt"),
        "restart_count": state.get("RestartCount", 0),
        "ports": ports,
    }


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe for Compose. Also confirms the Docker socket is usable."""
    try:
        await _list_containers()
    except (HTTPException, httpx.HTTPError) as exc:
        return {"status": "degraded", "docker": str(exc)}

    return {"status": "healthy", "service": "control-service"}


@app.get("/api/services")
async def list_services() -> dict[str, Any]:
    """Current state of every container in the compose project."""
    containers = await _list_containers()
    described = await asyncio.gather(*(_describe(container) for container in containers))

    order = {name: index for index, name in enumerate(SERVICE_ORDER)}
    described.sort(key=lambda item: order.get(item["name"] or "", len(SERVICE_ORDER)))

    return {"project": COMPOSE_PROJECT, "services": described}


@app.post("/api/services/{name}/{action}")
async def control_service(name: str, action: ServiceAction) -> dict[str, Any]:
    """Start, stop or restart one compose service."""
    container = await _resolve_container(name)
    docker: httpx.AsyncClient = app.state.docker

    params = {"t": "10"} if action in {"stop", "restart"} else None
    response = await docker.post(f"/containers/{container['Id']}/{action}", params=params)

    # 204 = done, 304 = already in the requested state; both are success.
    if response.status_code not in (204, 304):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Docker could not {action} {name}: {response.status_code} {response.text[:200]}",
        )

    logger.info("Service action applied", service=name, action=action)

    refreshed = await _resolve_container(name)
    return {"ok": True, "action": action, "service": await _describe(refreshed)}


@app.get("/api/services/{name}/logs")
async def service_logs(name: str, tail: int = 200) -> dict[str, Any]:
    """Recent log lines for one service, newest last."""
    container = await _resolve_container(name)
    docker: httpx.AsyncClient = app.state.docker

    response = await docker.get(
        f"/containers/{container['Id']}/logs",
        params={
            "stdout": "1",
            "stderr": "1",
            "timestamps": "1",
            "tail": str(max(1, min(tail, 2000))),
        },
    )

    if response.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not read logs for {name}: {response.status_code}",
        )

    decoder = DockerLogDecoder()
    lines = decoder.feed(response.content)
    lines.extend(decoder.flush())

    return {"service": name, "lines": lines}


@app.get("/api/services/{name}/logs/stream")
async def stream_service_logs(name: str, tail: int = 100) -> StreamingResponse:
    """Server-Sent Events stream of a service's logs (follow mode)."""
    container = await _resolve_container(name)
    container_id = container["Id"]
    docker: httpx.AsyncClient = app.state.docker

    async def event_stream() -> AsyncIterator[str]:
        decoder = DockerLogDecoder()
        # Tell the browser how quickly to reconnect when the container stops.
        yield "retry: 3000\n\n"

        try:
            async with docker.stream(
                "GET",
                f"/containers/{container_id}/logs",
                params={
                    "stdout": "1",
                    "stderr": "1",
                    "timestamps": "1",
                    "follow": "1",
                    "tail": str(max(1, min(tail, 1000))),
                },
            ) as response:
                if response.status_code != status.HTTP_200_OK:
                    yield f"event: error\ndata: {json.dumps(response.status_code)}\n\n"
                    return

                async for chunk in response.aiter_bytes():
                    for line in decoder.feed(chunk):
                        yield f"data: {json.dumps(line)}\n\n"

                for line in decoder.flush():
                    yield f"data: {json.dumps(line)}\n\n"

        except httpx.HTTPError as exc:
            yield f"event: error\ndata: {json.dumps(str(exc))}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stop nginx from buffering the stream.
            "X-Accel-Buffering": "no",
        },
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text[:500]}


async def _proxy(method: str, url: str, **kwargs: Any) -> JSONResponse:
    upstream: httpx.AsyncClient = app.state.upstream

    try:
        response = await upstream.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Upstream service unreachable: {exc}"},
        )

    headers = {}
    if response.headers.get("x-idempotent-replay"):
        headers["X-Idempotent-Replay"] = "true"

    return JSONResponse(
        status_code=response.status_code,
        content=_safe_json(response),
        headers=headers,
    )


@app.post("/api/orders")
async def proxy_create_order(request: Request) -> JSONResponse:
    """Forward an order submission, passing the idempotency key through.

    The dashboard fires each request from the browser, so this stays a thin proxy:
    no retries, no caching, no key generation beyond a fresh one when the caller
    did not send a key.
    """
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Request body must be JSON."},
        )

    idempotency_key = request.headers.get("x-idempotency-key") or str(uuid.uuid4())

    return await _proxy(
        "POST",
        f"{ORDER_SERVICE_URL}/orders",
        json=payload,
        headers={"X-Idempotency-Key": idempotency_key},
    )


@app.get("/api/orders")
async def proxy_list_orders(limit: int = 50) -> JSONResponse:
    """Recent orders, proxied so the browser needs no CORS or service discovery."""
    return await _proxy(
        "GET", f"{ORDER_SERVICE_URL}/orders", params={"limit": max(1, min(limit, 200))}
    )


@app.get("/api/orders/{order_ref}")
async def proxy_get_order(order_ref: str) -> JSONResponse:
    """A single order and its current status."""
    return await _proxy("GET", f"{ORDER_SERVICE_URL}/orders/{order_ref}")


@app.get("/api/inventory")
async def proxy_list_inventory(limit: int = 100) -> JSONResponse:
    """Current stock and prices."""
    return await _proxy(
        "GET", f"{WAREHOUSE_SERVICE_URL}/inventory", params={"limit": max(1, min(limit, 200))}
    )


@app.get("/api/summary")
async def summary() -> dict[str, Any]:
    """Aggregated numbers for the dashboard tiles.

    This is presentation data assembled from the two service APIs, not a
    contractual report endpoint: the assessment's Option A (event feed) is what
    other teams integrate with.
    """
    orders: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    unavailable: list[str] = []

    orders_result, inventory_result = await asyncio.gather(
        _proxy("GET", f"{ORDER_SERVICE_URL}/orders", params={"limit": 200}),
        _proxy("GET", f"{WAREHOUSE_SERVICE_URL}/inventory", params={"limit": 200}),
        return_exceptions=True,
    )

    if isinstance(orders_result, JSONResponse) and orders_result.status_code == 200:
        orders = json.loads(bytes(orders_result.body))
    else:
        unavailable.append("order-service")

    if isinstance(inventory_result, JSONResponse) and inventory_result.status_code == 200:
        inventory = json.loads(bytes(inventory_result.body))
    else:
        unavailable.append("warehouse-service")

    by_status: dict[str, int] = {"PENDING": 0, "CONFIRMED": 0, "FAILED": 0}
    revenue_cents = 0
    units_sold: dict[str, int] = {}

    for order in orders:
        order_status = str(order.get("status", "UNKNOWN"))
        by_status[order_status] = by_status.get(order_status, 0) + 1

        if order_status == "CONFIRMED":
            revenue_cents += int(order.get("total_cents", 0))
            for item in order.get("items", []):
                sku = str(item.get("sku"))
                units_sold[sku] = units_sold.get(sku, 0) + int(item.get("qty", 0))

    return {
        "orders": {
            "total": len(orders),
            "by_status": by_status,
            "revenue_cents": revenue_cents,
            "units_sold": units_sold,
            "cached_pricing": sum(1 for order in orders if order.get("pricing_source") == "cached"),
        },
        "inventory": inventory,
        "unavailable": unavailable,
    }
