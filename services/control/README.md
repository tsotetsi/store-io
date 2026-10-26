# Control Service

Small FastAPI service that powers the dashboard's operational controls. It talks
to the Docker Engine API directly over `/var/run/docker.sock` (plain HTTP over a
unix socket), so it needs no docker CLI or SDK inside the container.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness + Docker socket check |
| `GET /api/services` | State, health, ports and image for every compose container |
| `POST /api/services/{name}/start` | Start one service |
| `POST /api/services/{name}/stop` | Stop one service (10s grace) |
| `POST /api/services/{name}/restart` | Restart one service |
| `GET /api/services/{name}/logs?tail=200` | Recent log lines (demuxed) |
| `GET /api/services/{name}/logs/stream` | Live logs as Server-Sent Events |
| `POST /api/orders` | Thin proxy to `POST /orders`, forwards the idempotency key |
| `GET /api/orders` | Recent orders |
| `GET /api/orders/{ref}` | One order and its status |
| `GET /api/inventory` | Current stock and prices |
| `GET /api/summary` | Aggregated tiles for the dashboard UI |

## Notes

- Docker's log endpoint returns multiplexed frames (8-byte header + payload); the
  service decodes them into plain lines and supports TTY (raw) streams too.
- The order/inventory endpoints are thin proxies: retries, idempotency and state
  live in the domain services, never here.
