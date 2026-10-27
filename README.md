<div align="center">
  <img src="/assets/logo.svg" alt="Store-IO Logo" width="200px" height="200px">
</div>

> Store-IO | An orders and stock core flow that stays correct when clients retry
> and services briefly disappear.

<hr>

<div align="center">

  [![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.162.0-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
  [![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.1.6-2496ED?logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org/)
  [![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18-316192?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org)
  [![RabbitMQ](https://img.shields.io/badge/RabbitMQ-4-FF6600?style=flat&logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com/)
  [![Redis](https://img.shields.io/badge/Redis-8-dc382d?style=flat&logo=redis&logoColor=white)](https://redis.io/)
  [![React](https://img.shields.io/badge/React-19-61DAFB?style=flat&logo=react&logoColor=black)](https://react.dev/)
  [![Python](https://img.shields.io/badge/Python-3.14+-3776ab?logo=python&logoColor=white)](https://www.python.org/)

</div>

## What this is

Two independently deployable Python services plus durable infrastructure:

| Piece | Responsibility |
|---|---|
| **order-service** (`:8000`) | Accepts orders, prices them from the warehouse, stores them in PostgreSQL, publishes `order.placed`, exposes order status |
| **warehouse-service** (`:8001`) | Owns the product catalogue and stock, allocates stock from `order.placed`, exposes stock per SKU |
| **PostgreSQL ×2** | One database per service; no shared tables |
| **RabbitMQ** | Topic exchange + durable queues; the backlog is what makes catch-up work |
| **Redis** | Idempotency responses and last-known prices |
| **control-service** (`:8010`, localhost) | Docker operations API for the dashboard |
| **dashboard-service** (`:5173`) | React control room: topology, burst testing, live logs, start/stop/restart |

The flow demonstrates both unhappy paths from the brief:

1. **Duplicate submissions** - a burst reusing one `order_ref` creates exactly one
   order; the rest replay the stored result (200 + `X-Idempotent-Replay: true`).
2. **Brief interruption + catch-up** - with the warehouse stopped, orders are
   still accepted (priced from cache) and queued; restarting the warehouse drains
   the backlog, deducts stock and confirms the orders.

`SOLUTION.md` explains the design and the trade-offs.

## Getting started

#### Prerequisites

- **Docker** & **Docker Compose**
- **uv** (for `make demo` and `make test`; they run in throwaway envs)
- Python 3.14+ / Node 22+ only if you want to run a service outside Docker

#### Quick start

```bash
# 1. Generate local secrets (Postgres, RabbitMQ, Redis) — idempotent
make init-secrets

# 2. Build the four images
make build

# 3. Start everything
make up

# 4. Seed products, fire duplicates, stop/start the warehouse, show catch-up
make demo
```

Then open **http://localhost:5173** for the dashboard,
**http://localhost:8000/docs** for the Orders API and
**http://localhost:8001/docs** for the Warehouse API.

## API

### Orders (`order-service`)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/orders` | `202` created · `200` duplicate replay · `400` unknown SKU · `409` ref reused with a different cart · `503` prices unavailable |
| `GET` | `/orders/{order_ref}` | Order details + current status (`PENDING` / `CONFIRMED` / `FAILED`) |
| `GET` | `/orders` | Recent orders (`limit`, `status`, `customer_id`) |
| `GET` | `/health` | Liveness |

```bash
curl -s -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{"order_ref":"web-100045","customer_id":"cust-42","items":[{"sku":"BAN-001","qty":2}]}' | jq
```

### Warehouse (`warehouse-service`)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/inventory` | Full catalogue with prices and stock |
| `GET` | `/inventory/{sku}` | Stock + price (used by intake for pricing) |
| `POST` | `/inventory` | Upsert a product (idempotent seed) |
| `GET` | `/health` | Liveness |

### Events (RabbitMQ)

`order.placed` → `stock.allocated` / `stock.failed` → `order.confirmed`.
`make events` starts a tiny consumer that prints every event as JSON.

## Commands

| Target | Description |
|---|---|
| `make init-secrets` | Create `.secrets/` (Postgres, RabbitMQ, Redis); idempotent |
| `make build` / `make build-fast` | Build images (sequentially / with `make -j build-fast`) |
| `make up` / `make down` / `make restart` | Start / stop / recreate the stack |
| `make fresh` | Drop volumes, rebuild and start with an empty database |
| `make demo` | Full demo: seed -> duplicate burst -> age -> catch-up |
| `make demo-fast` | Duplicate burst only |
| `make seed` | Seed the demo products |
| `make events` | Print the event feed from RabbitMQ |
| `make dashboard` | Print dashboard and API URLs |
| `make migrate` | Apply Alembic migrations in both services |
| `make ps` / `make logs service=…` / `make shell service=…` | Inspect the stack |
| `make test` | Unit test suite (pytest, no Docker needed) |
| `make lint` / `make format` / `make typecheck` | Code quality |
| `make update` / `make audit` | Refresh pinned requirements / CVE scan |

Each service also has its own `Makefile` under `services/<name>/`.

## Dashboard

The React control room (see `services/dashboard/README.md`) visualises:

- the service topology with animated request/event packets,
- a **burst panel** to fire 1-50 real order submissions, with toggles to reuse one
  `order_ref` and/or one idempotency key - very response lands as a chip with its
  latency and status,
- orders and their PENDING -> CONFIRMED transitions,
- stock levels per SKU,
- per-service state, health, start/stop/restart buttons and a live log stream.

## Layout

```markdown
services/
  order/          order intake, pricing, idempotency, status API, migrations, tests
  warehouse/      catalogue, stock allocation consumer, migrations
  shared/         settings, RabbitMQ manager, chaos middleware
  control/        Docker operations API for the dashboard (local tooling)
  dashboard/      React + Vite control room
scripts/
  demo.py         seed + duplicate burst + outage/catch-up demonstration
  consume_events.py  event feed consumer (Option A integration surface)
  init-secrets.sh secrets generator
```
