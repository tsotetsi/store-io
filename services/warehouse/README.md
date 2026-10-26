# Warehouse Service

Owns the product catalogue and stock. Consumes `order.placed`, deducts stock
under a row lock, and reports back with `stock.allocated` / `stock.failed`.

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/inventory` | Full inventory with prices and stock |
| `GET` | `/inventory/{sku}` | Stock and price for one SKU (used by order intake) |
| `POST` | `/inventory` | Upsert a product (idempotent seed) |
| `GET` | `/health` | Liveness check |

If the service is stopped, its durable queue keeps the `order.placed` messages.
Starting it again drains the backlog and the affected orders move from `PENDING`
to `CONFIRMED`.

## Installing Dependencies

### Prerequisites

To develop this service locally, you need to have the following installed:

- [uv](https://docs.astral.sh/uv/)
- [Python 3.14+](https://www.python.org/)
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

You can install warehouse service requirements by running the following commands in the `services/warehouse` directory:

```bash
uv sync               # installs base + development requirements into .venv
```

### Service-level Makefile targets

From `services/warehouse/`, run:

| Target | Description |
|---|---|
| `make build` | Build the warehouse-service Docker image |
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

1. Inside the warehouse service root dir (`services/warehouse/`), run `uv pip install <package-name>`
2. Open the `./requirements/base.in` file or `requirements/development.in` file and append the package name to the file.

3. Then run `make update` which will regenerate the `./requirements/base.txt` and `./requirements/development.txt` files (with hashes), respectively and perform a security check (CVE).

4. Bump the version number in the `./requirements/base.in` file or `requirements/development.in` file where necessary.
