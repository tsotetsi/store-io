# Order Service.

Accepts order intents, prices them against the Warehouse Service, stores them in
PostgreSQL and publishes `order.placed` for stock allocation.

## HTTP API

| Method | Path | Description |
|---|---|---|
| `POST` | `/orders` | Create an order. `202` created, `200` duplicate replay (`X-Idempotent-Replay: true`), `400` unknown SKU, `409` reference reused with a different cart, `503` prices unavailable |
| `GET` | `/orders/{order_ref}` | Order details and status |
| `GET` | `/orders` | Recent orders (`limit`, `status`, `customer_id`) |
| `GET` | `/health` | Liveness |

`order_ref` is the idempotency key. A second submission for the same reference
never creates a second row: the primary key + `ON CONFLICT DO NOTHING` decide the
winner, and losers return the stored order. The optional `X-Idempotency-Key`
header additionally replays the exact original response from Redis and protects
requests where the client lets the service generate the reference.

Prices are frozen per line item at creation time (`unit_price_cents`,
`line_total_cents`, `total_cents`). If the warehouse is briefly unreachable, the
last known price is used and the order records `pricing_source="cached"`.

Schema changes are Alembic migrations, applied by the container entrypoint.
(`alembic upgrade head && uvicorn ...`).

## Install Dependencies

### Prerequisites

To develop this service locally, you need to have the following installed:

- [uv](https://docs.astral.sh/uv/)
- [Python 3.14+](https://www.python.org/)
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

You can install order service requirements by running the following commands in the `services/order` directory:

```bash
uv sync               # installs base + development requirements into .venv
```

### Service-level Makefile targets

From `services/order/`, run:

| Target | Description |
|---|---|
| `make build` | Build the order-service Docker image |
| `make update` | Update pinned requirements with hashes + run CVE security scan |
| `make audit` | Audit requirements with `pip-audit` |
| `make shell` | Open a shell in the running container |
| `make logs` | Stream container logs |
| `make ps` | List running compose services |
| `make alembic-migrations msg="..."` | Generate a new Alembic migration (requires running stack) |
| `make alembic-upgrade` | Apply pending migrations to the database |
| `make alembic-downgrade` | Rollback the last migration |

### Adding new requirements and checking vulnerabilities

To add a new package to the service, follow the steps below:

1. Inside the order service root dir (`services/order/`), run `uv pip install <package-name>`
2. Open the `./requirements/base.in` file or `requirements/development.in` file and append the package name to the file.

3. Then run `make update` which will regenerate the `./requirements/base.txt` and `./requirements/development.txt` files (with hashes), respectively and perform a security check (CVE).

4. Bump the version number in the `./requirements/base.in` file or `requirements/development.in` file where necessary.
