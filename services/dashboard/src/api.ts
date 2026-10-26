// Thin client for the control service. Every call is same-origin: nginx (prod)
// or the Vite dev proxy forwards /api to control-service.

export type ServiceInfo = {
  name: string;
  container: string;
  id: string;
  image: string;
  state: string;
  status: string;
  health: string | null;
  started_at: string | null;
  restart_count: number;
  ports: { private: number; public: number | null; type: string }[];
};

export type OrderItem = {
  sku: string;
  qty: number;
  unit_price_cents: number;
  line_total_cents: number;
};

export type Order = {
  order_ref: string;
  customer_id: string;
  items: OrderItem[];
  total_cents: number;
  status: "PENDING" | "CONFIRMED" | "FAILED" | string;
  pricing_source: "live" | "cached" | string;
  created_at: string;
};

export type InventoryItem = {
  sku: string;
  name: string;
  price_cents: number;
  stock: number;
};

export type Summary = {
  orders: {
    total: number;
    by_status: Record<string, number>;
    revenue_cents: number;
    units_sold: Record<string, number>;
    cached_pricing: number;
  };
  inventory: InventoryItem[];
  unavailable: string[];
};

export type OrderPayload = {
  order_ref?: string;
  customer_id: string;
  items: { sku: string; qty: number }[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body.slice(0, 200)}`);
  }

  return (await response.json()) as T;
}

export const api = {
  services: (signal?: AbortSignal) => request<{ services: ServiceInfo[] }>("/api/services", { signal }),
  summary: (signal?: AbortSignal) => request<Summary>("/api/summary", { signal }),
  orders: (limit = 50, signal?: AbortSignal) =>
    request<Order[]>(`/api/orders?limit=${limit}`, { signal }),
  serviceAction: (name: string, action: "start" | "stop" | "restart") =>
    request<{ ok: boolean; service: ServiceInfo }>(`/api/services/${name}/${action}`, {
      method: "POST",
    }),
  logs: (name: string, tail = 200) =>
    request<{ service: string; lines: string[] }>(`/api/services/${name}/logs?tail=${tail}`),
};

export type BurstOutcome = "inflight" | "created" | "replayed" | "conflict" | "error";

export type BurstResult = {
  id: string;
  orderRef: string;
  idempotencyKey: string;
  status: number | null;
  outcome: BurstOutcome;
  latencyMs: number | null;
  detail?: string;
};

export const BURST_CART = {
  customer_id: "cust-42",
  items: [
    { sku: "BAN-001", qty: 2 },
    { sku: "APL-003", qty: 1 },
  ],
};

/** Fire a single order. Returns the parsed outcome instead of throwing. */
export async function sendOrder(payload: OrderPayload, idempotencyKey: string): Promise<{
  status: number;
  replayed: boolean;
  body: unknown;
  latencyMs: number;
}> {
  const started = performance.now();

  const response = await fetch("/api/orders", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });

  const latencyMs = performance.now() - started;

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  return {
    status: response.status,
    replayed: response.headers.get("x-idempotent-replay") === "true",
    body,
    latencyMs,
  };
}

export function formatCents(cents: number): string {
  return (cents / 100).toLocaleString(undefined, {
    style: "currency",
    currency: "ZAR",
    maximumFractionDigits: 2,
  });
}

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString(undefined, { hour12: false });
}
