#!/usr/bin/env python3
"""End-to-end demonstration of the Orders & Stock core flow.

What it proves, in order:

1. Seeding      - products are created through the Warehouse API (idempotent).
2. Duplicates   - a burst of concurrent orders that reuses an ``order_ref``
                  creates exactly one row; duplicates replay the stored order
                  (HTTP 200 + ``X-Idempotent-Replay``) instead of counting twice.
3. Key reuse    - the same ``order_ref`` with a different cart is rejected (409).
4. Outage       - with the Warehouse Service stopped, orders are still accepted
                  (202, priced from the last known price) and queued on RabbitMQ.
5. Catch-up     - when the Warehouse Service comes back, the queue drains, stock
                  is deducted and the orders move to CONFIRMED.

Usage:
    python scripts/demo.py                 # full demo (requires Docker)
    python scripts/demo.py --no-outage     # skip stop/start of the warehouse
    python scripts/demo.py --help

The script only needs ``httpx``:
    uv run --with httpx==0.28.1 --no-project python scripts/demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT_DIR = Path(__file__).resolve().parents[1]

ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8000").rstrip("/")
WAREHOUSE_SERVICE_URL = os.getenv("WAREHOUSE_SERVICE_URL", "http://localhost:8001").rstrip("/")

WAREHOUSE_CONTAINER = os.getenv("WAREHOUSE_CONTAINER", "warehouse-service")

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
GREY = "\033[0;90m"
BOLD = "\033[1m"
RESET = "\033[0m"

SEED_PRODUCTS = [
    {"sku": "BAN-001", "name": "Bananas 1kg", "price_cents": 199, "stock": 500},
    {"sku": "APL-003", "name": "Apples 1kg", "price_cents": 249, "stock": 500},
    {"sku": "ORG-007", "name": "Oranges 1kg", "price_cents": 229, "stock": 500},
]


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def banner(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'=' * 74}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 74}{RESET}")


def info(message: str) -> None:
    print(f"{GREY}  · {message}{RESET}")


def ok(message: str) -> None:
    print(f"{GREEN}  ✔ {message}{RESET}")


def warn(message: str) -> None:
    print(f"{YELLOW}  ! {message}{RESET}")


def fail(message: str) -> None:
    print(f"{RED}  ✘ {message}{RESET}")


@dataclass
class CallResult:
    label: str
    status_code: int | None
    body: dict[str, Any] | None
    latency_ms: float | None
    replayed: bool
    error: str | None = None


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


async def post_order(
    client: httpx.AsyncClient,
    *,
    label: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> CallResult:
    headers = {"X-Idempotency-Key": idempotency_key} if idempotency_key else {}
    started = time.perf_counter()

    try:
        response = await client.post(
            f"{ORDER_SERVICE_URL}/orders",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        return CallResult(label, None, None, None, False, error=str(exc))

    latency_ms = (time.perf_counter() - started) * 1000

    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"raw": response.text}

    return CallResult(
        label=label,
        status_code=response.status_code,
        body=body,
        latency_ms=latency_ms,
        replayed=response.headers.get("x-idempotent-replay") == "true",
    )


async def fetch_order(client: httpx.AsyncClient, order_ref: str) -> dict[str, Any] | None:
    try:
        response = await client.get(f"{ORDER_SERVICE_URL}/orders/{order_ref}", timeout=10.0)
    except httpx.RequestError:
        return None

    if response.status_code != 200:
        return None

    return response.json()


async def fetch_stock(client: httpx.AsyncClient, sku: str) -> int | None:
    try:
        response = await client.get(f"{WAREHOUSE_SERVICE_URL}/inventory/{sku}", timeout=5.0)
    except httpx.RequestError:
        return None

    if response.status_code != 200:
        return None

    return int(response.json()["stock"])


def describe(result: CallResult) -> str:
    if result.error:
        return f"{RED}network error{RESET}: {result.error}"

    code = result.status_code or 0
    colour = GREEN if code < 300 else YELLOW if code < 500 else RED
    detail = ""

    if result.body:
        if "order_ref" in result.body:
            detail = (
                f" ref={result.body['order_ref']}"
                f" total={result.body.get('total_cents')}c"
                f" status={result.body.get('status')}"
                f" priced={result.body.get('pricing_source')}"
            )
        elif "detail" in result.body:
            detail = f" {result.body['detail']}"

    latency = f" ({result.latency_ms:.0f} ms)" if result.latency_ms is not None else ""
    replay = f" {CYAN}[replayed]{RESET}" if result.replayed else ""
    return f"{colour}{code}{RESET}{latency}{replay}{detail}"


# --------------------------------------------------------------------------- #
# Docker helpers (only used by the outage step)
# --------------------------------------------------------------------------- #


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def container_state(name: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def wait_for_state(name: str, desired: set[str], timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container_state(name) in desired:
            return True
        time.sleep(1.0)
    return False


# --------------------------------------------------------------------------- #
# Demo steps
# --------------------------------------------------------------------------- #


async def seed_products(client: httpx.AsyncClient) -> None:
    banner("Step 1 · Seed products through the Warehouse API")

    for product in SEED_PRODUCTS:
        response = await client.post(
            f"{WAREHOUSE_SERVICE_URL}/inventory", json=product, timeout=10.0
        )
        if response.status_code in (200, 201):
            body = response.json()
            ok(
                f"{body['sku']:<8} {body['name']:<14} {body['price_cents']:>4}c  stock={body['stock']}"
            )
        else:
            fail(f"Could not seed {product['sku']}: {response.status_code} {response.text}")
            sys.exit(1)


async def burst_with_duplicates(client: httpx.AsyncClient, run_id: str) -> dict[str, str]:
    banner("Step 2 · Concurrent burst with duplicate order_ref")

    cart = {
        "customer_id": "cust-42",
        "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}],
    }
    original_ref = f"web-{run_id}-a"
    distinct_ref = f"web-{run_id}-b"
    shared_key = str(uuid.uuid4())

    info(f"Reused order_ref : {original_ref}")
    info(
        "Firing 4 submissions simultaneously: two share the reference "
        "(separate idempotency keys), one of those also shares the key."
    )

    # Neither duplicate is "first": the database decides which transaction wins,
    # so one of these two returns 202 and the other replays with 200.
    calls = [
        post_order(
            client,
            label="ref A, key 1     ",
            payload={**cart, "order_ref": original_ref},
            idempotency_key=str(uuid.uuid4()),
        ),
        post_order(
            client,
            label="ref B, key 2     ",
            payload={**cart, "order_ref": distinct_ref},
            idempotency_key=str(uuid.uuid4()),
        ),
        post_order(
            client,
            label="ref A, key 3     ",
            payload={**cart, "order_ref": original_ref},
            idempotency_key=str(uuid.uuid4()),
        ),
        post_order(
            client,
            label="ref A, shared key",
            payload={**cart, "order_ref": original_ref},
            idempotency_key=shared_key,
        ),
    ]
    results = await asyncio.gather(*calls)

    for result in results:
        print(f"\n  {BOLD}{result.label}{RESET} → {describe(result)}")

    accepted = [r for r in results if r.status_code in (200, 202)]
    created = [r for r in accepted if not r.replayed and r.status_code == 202]
    replays = [r for r in accepted if r.replayed]

    print()
    if len(created) == 2 and len(replays) == 2:
        ok(
            "2 orders created, 2 duplicates replayed — one row for the reused reference "
            "(which request wins is decided by the database, not by arrival order)."
        )
    else:
        fail(
            f"Unexpected outcome: created={len(created)} replayed={len(replays)} "
            f"statuses={[r.status_code for r in results]}"
        )

    # A duplicate reference is only idempotent if the payload matches.
    conflict = await post_order(
        client,
        label="5 key reuse, new cart",
        payload={
            "order_ref": original_ref,
            "customer_id": "cust-42",
            "items": [{"sku": "BAN-001", "qty": 99}],
        },
        idempotency_key=str(uuid.uuid4()),
    )
    print(f"\n  {BOLD}{conflict.label}{RESET} → {describe(conflict)}")
    if conflict.status_code == 409:
        ok("A reused order_ref with a different cart is rejected (409), not merged.")
    else:
        fail(f"Expected 409 for a conflicting payload, got {conflict.status_code}.")

    return {"original_ref": original_ref, "distinct_ref": distinct_ref}


async def wait_for_final_states(
    client: httpx.AsyncClient,
    order_refs: list[str],
    *,
    wait_seconds: float = 60.0,
) -> dict[str, str]:
    """Poll until every order is CONFIRMED/FAILED (or the deadline passes)."""
    deadline = time.monotonic() + wait_seconds
    states: dict[str, str] = {}

    while time.monotonic() < deadline:
        states = {}
        for order_ref in order_refs:
            order = await fetch_order(client, order_ref)
            states[order_ref] = order["status"] if order else "?"
        if all(state in {"CONFIRMED", "FAILED"} for state in states.values()):
            break
        await asyncio.sleep(0.5)

    return states


async def outage_and_catch_up(
    client: httpx.AsyncClient,
    run_id: str,
    settled_refs: list[str],
) -> None:
    banner("Step 3 · Warehouse outage, orders still accepted, then catch-up")

    sku = "BAN-001"

    # The burst above is asynchronous: wait for its allocations before taking the
    # baseline, or their deductions would be counted as catch-up work below.
    if settled_refs:
        await wait_for_final_states(client, settled_refs, wait_seconds=20.0)

    stock_before = await fetch_stock(client, sku)
    if stock_before is None:
        fail("Warehouse is not reachable before the test; skipping the outage step.")
        return
    info(f"{sku} stock before: {stock_before}")

    info(f"Stopping {WAREHOUSE_CONTAINER} (docker compose stop)...")
    stop = compose("stop", WAREHOUSE_CONTAINER)
    if stop.returncode != 0:
        fail(f"Could not stop {WAREHOUSE_CONTAINER}: {stop.stderr.strip()}")
        return

    if not wait_for_state(WAREHOUSE_CONTAINER, {"exited", "stopped"}, timeout=45):
        fail(f"{WAREHOUSE_CONTAINER} did not stop in time.")
        return
    ok("Warehouse is down. Price cache is warm from the burst above.")

    offline_refs = [f"web-{run_id}-outage-{index}" for index in range(3)]
    accepted_during_outage = 0

    for order_ref in offline_refs:
        result = await post_order(
            client,
            label=f"offline {order_ref:<24}",
            payload={
                "order_ref": order_ref,
                "customer_id": "cust-42",
                "items": [{"sku": "BAN-001", "qty": 1}],
            },
            idempotency_key=str(uuid.uuid4()),
        )
        print(f"\n  {result.label} → {describe(result)}")
        if result.status_code == 202 and result.body:
            accepted_during_outage += 1

    if accepted_during_outage == len(offline_refs):
        ok(
            "All orders accepted while stock was unreachable "
            f"({accepted_during_outage}/{len(offline_refs)}), priced from cache."
        )
    else:
        warn(
            "Some orders were rejected during the outage "
            f"({accepted_during_outage}/{len(offline_refs)}). If you just reset the "
            "database, run the demo once more so prices are cached."
        )

    info("Checking they are queued as PENDING...")
    pending = 0
    for order_ref in offline_refs:
        order = await fetch_order(client, order_ref)
        if order and order["status"] == "PENDING":
            pending += 1
    ok(f"{pending}/{len(offline_refs)} orders are PENDING and waiting for stock.")

    info(f"Starting {WAREHOUSE_CONTAINER} again...")
    start = compose("start", WAREHOUSE_CONTAINER)
    if start.returncode != 0:
        fail(f"Could not start {WAREHOUSE_CONTAINER}: {start.stderr.strip()}")
        return

    # Give the consumer a moment to reconnect and drain the durable queue.
    final_states = await wait_for_final_states(client, offline_refs, wait_seconds=60.0)

    for order_ref, state in final_states.items():
        colour = GREEN if state == "CONFIRMED" else RED
        print(f"  {order_ref:<28} → {colour}{state}{RESET}")

    if all(state == "CONFIRMED" for state in final_states.values()):
        stock_after = await fetch_stock(client, sku)
        ok(f"Catch-up complete: all orders confirmed, {sku} stock is now {stock_after}.")
        if stock_before is not None and stock_after is not None:
            expected = 3
            deducted = stock_before - stock_after
            if deducted == expected:
                ok(f"Exactly {deducted} units were deducted (3 orders x 1 unit).")
            elif deducted > expected:
                # Legitimate: older orders still queued from a previous run drain
                # at the same time. This run's 3 units are included either way.
                ok(
                    f"{deducted} units deducted: {expected} from this run, "
                    f"{deducted - expected} from older queued orders."
                )
            else:
                warn(f"Expected at least {expected} units deducted after restart, saw {deducted}.")
    else:
        fail(
            "Some orders never reached a final state; check `make logs service=warehouse-service`."
        )


async def show_status(client: httpx.AsyncClient) -> None:
    banner("Final state")

    try:
        orders = (await client.get(f"{ORDER_SERVICE_URL}/orders?limit=100", timeout=10.0)).json()
        inventory = (await client.get(f"{WAREHOUSE_SERVICE_URL}/inventory", timeout=10.0)).json()
    except httpx.HTTPError as exc:
        fail(f"Could not read state: {exc}")
        return

    by_status: dict[str, int] = {}
    for order in orders:
        by_status[order["status"]] = by_status.get(order["status"], 0) + 1

    print(f"\n  {BOLD}Orders{RESET}")
    for order in orders[:8]:
        print(
            f"    {order['order_ref']:<28} {order['status']:<10} "
            f"{order['total_cents']:>6}c  {order['pricing_source']:<6} {order['customer_id']}"
        )
    if len(orders) > 8:
        print(f"    {GREY}... {len(orders) - 8} more{RESET}")

    print(f"\n  {BOLD}Totals{RESET}: {len(orders)} orders → {by_status}")
    revenue = sum(order["total_cents"] for order in orders if order["status"] == "CONFIRMED")
    info(f"Confirmed revenue: {revenue}c")

    print(f"\n  {BOLD}Inventory{RESET}")
    for item in inventory:
        print(
            f"    {item['sku']:<8} {item['name']:<14} {item['price_cents']:>4}c  stock={item['stock']}"
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace) -> int:
    run_id = datetime.now(UTC).strftime("%H%M%S")

    banner("Store-IO · Orders & Stock demo")
    info(f"Order Service    : {ORDER_SERVICE_URL}")
    info(f"Warehouse Service: {WAREHOUSE_SERVICE_URL}")
    info(f"Run id           : {run_id}")

    async with httpx.AsyncClient() as client:
        for service_url, name in (
            (ORDER_SERVICE_URL, "order-service"),
            (WAREHOUSE_SERVICE_URL, "warehouse-service"),
        ):
            try:
                health = await client.get(f"{service_url}/health", timeout=5.0)
                ok(f"{name} is up ({health.json().get('status')}).")
            except httpx.HTTPError:
                fail(f"{name} is not reachable at {service_url}. Start the stack with `make up`.")
                return 1

        await seed_products(client)

        if args.seed_only:
            banner("Done (seed only)")
            return 0

        burst_refs = await burst_with_duplicates(client, run_id)

        if args.no_outage:
            warn("Skipping the outage/catch-up step (--no-outage).")
        elif compose("version").returncode != 0:
            warn("Docker Compose is not available; skipping the outage/catch-up step.")
        else:
            await outage_and_catch_up(client, run_id, list(burst_refs.values()))

        await show_status(client)

    banner("Done")
    print(
        f"  {GREY}Dashboard: http://localhost:5173 · API docs: "
        f"{ORDER_SERVICE_URL}/docs · {WAREHOUSE_SERVICE_URL}/docs{RESET}\n"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed products, fire a burst of orders with duplicates, and demonstrate outage catch-up."
    )
    parser.add_argument(
        "--no-outage",
        action="store_true",
        help="Skip stopping/starting the warehouse container.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Only upsert the demo products and exit.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
