# Store-IO - Design & Trade-offs

This is a minimal orders & stock core flow: Python + PostgreSQL, two independent
services, durable messaging between them, duplicate-safe intake, price snapshots,
and a demonstrated recovery after a brief stock outage.

The solution allows either one process or two, and a simple polling/outbox catch-up.
This solution runs **two FastAPI services**, order and warehouse, sharing a **RabbitMQ topic exchange**.

---

## 1. Architecture

```markdown
                        ┌───────────────────────────────────────────┐
  POST /orders  ──────▶ │ order-service :8000                       │
                        │  price lookup ───────────────┐            │
                        │  INSERT order + outbox (1 tx)│            │
                        └──────────┬───────────────────┼────────────┘
                                   │                   │ GET /inventory/{sku}
                        ┌──────────▼──────────┐        │
                        │ PostgreSQL: orders, │        ▼
                        │ outbox_events       │  ┌──────────────────────────┐
                        └──────────┬──────────┘  │ warehouse-service :8001  │
                                   │             │  deduct stock + ledger   │
                                   │             │  INSERT outbox (stock.*) │
                                   │             └────────────┬─────────────┘
                                   │  outbox publisher        │ outbox publisher
                                   ▼                          ▼
                        ┌──────────────────────────────────────────┐
                        │ RabbitMQ store_io_exchange (topic)       │
                        │ order.placed / stock.* / order.confirmed │
                        └──────────────────────────────────────────┘
```

| Component | Owns | Scales independently because |
|---|---|---|
| **order-service** | `orders` + its outbox, order lifecycle, price snapshots | it publishes events; it never touches inventory rows. |
| **warehouse-service** | `inventory`, the processed-order ledger and its outbox | it consumes events; it never writes orders. |
| **PostgreSQL** (two instances) | durable state per service | no shared database, no cross-service joins. |
| **RabbitMQ** | event transport, backlog during outages | durable topic queues buffer a dead consumer. |
| **Redis** | idempotency responses, last-known prices | both are cache-only; losing Redis degrades, never corrupts. |
| **control-service** | Docker Engine API (local tooling) | keeps host control out of the domain services |
| **dashboard-service** | React UI over the control API | pure presentation, out of scope. |

***Why FastAPI***: Native async for non-blocking DB/broker/HTTP work, Pydantic
validation for the payload and the `X-Idempotency-Key` header, and free OpenAPI
docs at `/docs` for each service.

***Why RabbitMQ rather than plain polling***: Durable
queues are the simplest thing that gives at-least-once delivery and a backlog
that survives a restart, which is exactly the "catch up after recovery"
constraint.

***Why Redis***: `SET NX` command is the one atomic primitive needed to collapse a
burst of duplicates, and price caching falls out of the same connection.

---

## 2. Data model

```sql
-- order-service
CREATE TABLE orders (
    order_ref      VARCHAR(64) PRIMARY KEY,   -- client idempotency key.
    customer_id    VARCHAR NOT NULL,
    items          JSON NOT NULL,             -- [{sku, qty, unit_price_cents, line_total_cents}].
    total_cents    INTEGER NOT NULL,          -- price snapshot at order time.
    pricing_source VARCHAR(16) NOT NULL,      -- live | cached.
    status         VARCHAR(16) NOT NULL,      -- PENDING | CONFIRMED | FAILED.
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- both services (identical shape)
CREATE TABLE outbox_events (
    id         BIGSERIAL PRIMARY KEY,
    event_type VARCHAR NOT NULL,
    payload    JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,                 -- NULL until the broker confirms.
    attempts   INTEGER NOT NULL DEFAULT 0
);

-- warehouse-service
CREATE TABLE inventory (
    sku         VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    price_cents INTEGER NOT NULL,
    stock       INTEGER NOT NULL
);

CREATE TABLE processed_orders (               -- consumer-side dedupe ledger.
    order_ref    VARCHAR PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Line items are stored as JSON because an order is an immutable snapshot of what the customer is buying right now(Prices changes all the time depending on the market); normalising them would invite someone to "fix" historical prices later. The total is stored, not recomputed, for the same reason - the requirements asks for prices *at the time of the order*.

The outbox and ledger tables are the delivery mechanism; keeping them in the same database as the business tables is what makes the write atomic.

Schema changes ship as Alembic migrations inside each image, applied by the
container entrypoint (`alembic upgrade head && uvicorn …`). No `create_all` in
production paths, so the deployed schema is always a versioned, reviewable
artifact.

---

## 3. Order intake - `POST /orders`

1. Validate the payload (`order_ref` optional, `qty` 1..1000).
2. Resolve the reference: client `order_ref` -> derived from `X-Idempotency-Key` (SHA-256 prefix) -> random `web-***`. We can also do `mobile-***` for metrics related activities.
3. Price the cart: one concurrent HTTP call per distinct SKU, short timeout.
4. `INSERT … ON CONFLICT (order_ref) DO NOTHING RETURNING *`.
5. **Winner**: commit the order *and* its `order.placed` outbox row in one
   transaction, return `202 Accepted`. The publisher sends it afterwards.
   **Loser**: return the stored order with `200 OK` and `X-Idempotent-Replay: true`.
6. If the same reference arrives with a *different* cart, return `409` - silently
   merging two different orders would be a data-integrity bug, not idempotency.

`GET /orders/{order_ref}` returns the order and its live status;
`GET /orders?status=&customer_id=&limit=` feeds the dashboard.

### Idempotency, in two layers

| Layer | Protects | Mechanism | Survives |
|---|---|---|---|
| Redis middleware | repeated `X-Idempotency-Key`, including 50-way concurrent bursts | `SET NX` command, waiters poll for the cached response | within TTL (24 h) |
| PostgreSQL primary key | repeated `order_ref`, always | `ON CONFLICT DO NOTHING` | forever |

The database is the source of truth. Redis exists to replay the exact HTTP body without touching Postgres, and it fails **open**: if Redis is down, orders still get created and duplicates are still rejected by the primary key. A replay is byte-identical to the original response, so the only signal is the `X-Idempotent-Replay: true` header (plus `200` instead of `202`).

A duplicate whose original is still in flight waits up to 10s for the result instead of returning an unhelpful 409. A key reused with a different request body is rejected with 409, so a client cannot accidentally turn one order into another.

---

## 4. Pricing - The real dependency on stock

Prices live in the warehouse"

- Each successful lookup is cached in Redis for 24 h (`order:price:<sku>`).
- If the warehouse errors or times out, the cached price is used and the order is
  marked `pricing_source="cached"` (visible in the API and the dashboard).
- If there is **no** cached price for a SKU, intake returns `503`(Service Unavailable) and does not
  create the order. Inventing a price is worse than asking the client to retry.
- An unknown SKU (`404` from the warehouse) is a `400`, not a retry.

Stock availability is deliberately *not* checked here: that is the warehouse's
job, and it happens asynchronously. An order that turns out to be unfulfillable
moves to `FAILED` instead of blocking the intake path.

---

## 5. Stock handling and catch-up

`warehouse-service` consumes `order.placed` with `prefetch=5`, locks each row
(`SELECT … FOR UPDATE`) and deducts stock in one transaction. Any shortage rolls
the whole order back and publishes `stock.failed`; success publishes
`stock.allocated`. `order-service` consumes both and moves the order to
`CONFIRMED` / `FAILED`, publishing `order.confirmed` as the terminal event.

Catch-up is a property of the broker, not of custom retry code:

1. `docker compose stop warehouse-service` - the consumer disappears.
2. Orders keep returning `202`(Accepted); their rows are `PENDING`, priced from cache.
3. `order.placed` messages accumulate in the durable queue.
4. Start the warehouse again - it reconnects (robust connection), drains the
   backlog, deducts stock and emits the feedback events.
5. Orders flip to `CONFIRMED`; stock is exactly N units lower.

The consumer is written to be *at-least-once safe*: it only applies feedback to a
`PENDING` order, so a redelivered message cannot re-publish `order.confirmed` or
overwrite a final state.

### Option A was chosen: integration surface

Every lifecycle event goes through the topic exchange (`order.placed`, `stock.allocated`, `order.confirmed`, `stock.failed`). `scripts/consume_events.py` binds a durable queue to those keys and prints JSON lines. That is the "other teams can consume accepted orders" surface, and it doubles as an audit feed.

---

## 6. Delivery: the transactional outbox

Publishing inside the request handler leaves a window: the database commit can
succeed and the process can die (or the broker can be down) before the message is
sent, leaving an order nobody will allocate. Both services use an outbox instead.

1. The business write and an `outbox_events` row commit in **one** transaction.
2. A background publisher reads unpublished rows, publishes them with publisher
   confirms, and marks each row published.
3. A failure leaves the row unpublished and the next cycle retries it (0.5 s).

That makes delivery *at-least-once*, so consumers deduplicate:

* The warehouse claims `order_ref` in `processed_orders` in the same transaction
  as the stock deduction. A redelivered `order.placed` is a no-op - no second
  deduction, no second `stock.allocated`.
* The order service only applies stock feedback to a `PENDING` order, so a
  redelivered `stock.allocated` cannot re-publish `order.confirmed` or overwrite a
  final state.

A useful side effect: intake no longer depends on the broker at all. With
RabbitMQ stopped, `POST /orders` still returns `202` in milliseconds; the event
sits in the outbox and is delivered when the broker returns.

The publisher assumes one instance per service. Multiple replicas would claim
rows with `SELECT ... FOR UPDATE SKIP LOCKED`; the *at-least-once + dedupe
semantics* stay the same. Delivered rows are purged after 24 h.

---

## 7. Failure behaviour

| Scenario | Behaviour | Why acceptable |
|---|---|---|
| Duplicate `order_ref` (sequential or concurrent) | one row, duplicate gets `200` + stored body | primary key decides, no *check-then-insert race* |
| Duplicate `X-Idempotency-Key` while first in flight | waiters get the original response | Redis marker + polling |
| Warehouse down for seconds | orders accepted, priced from cache, `PENDING` | explicit as per requirement brief |
| Warehouse down, no cached price | `503`, no order written | never invent a price |
| Redis down | orders proceed; DB still deduplicates | cache failure is not a correctness failure |
| RabbitMQ down at request time | order accepted; event waits in the outbox and is published after recovery | intake has no broker dependency |
| Warehouse restarts mid-allocation | *unacked* message is redelivered; the ledger makes it a no-op | claim + deduction in one transaction |
| Order DB down | `500`(Internal Server Error); no partial writes | single(ATOMIC) transaction |
| Client stops mid-request | order and its event are committed together | outbox in the same transaction |

---

## 8. Verification performed

Against the real Compose stack (not mocks or tests):

- `make demo` - seeds 3 products; 4 concurrent submissions (2 references, 2 duplicates) -> **2 × 202 created, 2 × 200 replayed**, 5th conflicting request -> 409.
- Warehouse stopped -> 3 orders accepted (`202`, `pricing_source=cached`,
  `PENDING`); warehouse started -> all 3 `CONFIRMED`, `BAN-001` stock down by
  exactly 3.
- `make test` - 30 pytest tests, no Docker required:
  - *intake*: reference derivation from `order_ref` / `X-Idempotency-Key` / random,
    and intent comparison (split lines and reordered lines are the same intent).
  - *pricing*: live pricing with merged SKUs, cached fallback during an outage,
    unknown SKU vs. unavailable price, malformed price, Redis read/write failure.
  - *outbox*: publish + mark, retry after a broker failure, stop-at-first-failure
    ordering, batch limits, and retention purge (real SQL via SQLite).
  - *idempotency middleware*: 20-way style bursts over overlapping HTTP requests
    (one execution, byte-identical replay), key reuse with a different cart (409),
    `503` released for retry, crash releases the key, Redis outage fails open.
- *Outbox under a broker outage (on the built images)*: with RabbitMQ stopped,
  `POST /orders` returned `202` in milliseconds, the `order.placed` row stayed
  `published=false` while `attempts` climbed; after RabbitMQ restarted the row was
  published and the order moved to `CONFIRMED`.
- Headless browser run of the dashboard: 8 service cards rendered, a 20-request
  burst produced **1 created / 19 replayed** with animated packets and no console
  errors, order rows appeared, and the live log stream rendered.
- `pre-commit run --all-files` (ruff lint + format + mypy) passes; the dashboard
  `tsc --noEmit` and `vite build` pass.

---

## 10. Known gaps and what I would do next

1. **Single publisher instance.** The outbox publisher assumes it is the only
   one running; horizontal replicas would need `SELECT … FOR UPDATE SKIP
   LOCKED`. At-least-once delivery and consumer dedupe already hold, so this is
   a throughput limit, not a correctness gap.
2. **No compensation.** `stock.failed` marks the order `FAILED`, but partially
   deducted stock from a multi-line order is rolled back by the transaction,  
   - A true saga/compensation is still missing for cross-order cases.
3. **Idempotency cache TTL vs. business lifetime.** After 24h the same
   `X-Idempotency-Key` would be treated as a new request (the `order_ref` check
   still protects the common case).
4. **No auth, rate limiting or per-customer quotas** - explicitly out of scope in
   the requirement document.
5. **Metrics/tracing**: Prometheus client is already a dependency but I did not add anything around that. For each order, I wanted to add tracing to track the order lifecycle.
6. **Dashboard/control service are local-only.** Socket access is root-equivalent;
   they are deliberately unauthenticated and bound to `127.0.0.1`.

Link to vidio demostration - "https://drive.google.com/file/d/1_1eQpNOaviYIRUSUvYrQzQZ7i0EOF-c0/view?usp=sharing"

 --- Thanks ----
