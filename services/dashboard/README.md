# Dashboard Service

A React (Vite + TypeScript) control room for the local stack. It is demo
tooling, not part of the order flow.

## What it shows

- **Topology** — Orders → RabbitMQ/Redis → Warehouse, with a dot per dependency
  and animated packets when a request is in flight or an event is published.
- **Concurrent order burst** — fire 1–50 real `POST /orders` requests, optionally
  reusing one `order_ref` and/or one `X-Idempotency-Key`. Every response lands as
  a chip (202 created / 200 replayed / 409 conflict / 5xx) with its latency, so
  duplicate handling is visible rather than asserted.
- **Orders table** — status transitions from PENDING to CONFIRMED/FAILED as the
  warehouse catches up.
- **Stock** — live levels per SKU and units sold from confirmed orders.
- **Services** — container state, health, ports, start/stop/restart buttons and a
  streaming log viewer (SSE) for every compose service.

## How it talks to the stack

```
browser ──▶ dashboard-service (nginx:80)
                 ├── /            static React bundle
                 └── /api/*       proxy ──▶ control-service:8010
                                                  ├── Docker Engine API (socket)
                                                  ├── order-service:8000
                                                  └── warehouse-service:8000
```

The browser never needs the Docker socket, CORS exceptions or service hostnames:
nginx keeps everything same-origin, and the control service is the only container
with socket access.

## Local development

```bash
# from the repo root
make up                        # brings the stack (and thus control-service) up
cd services/dashboard
npm install
npm run dev                    # http://localhost:5173, /api proxied to :8010
```

`npm run build` runs the TypeScript check and produces `dist/`, which is what the
Docker image serves.
