import { useEffect, useRef, useState } from "react";
import { BURST_CART, sendOrder, type BurstOutcome, type BurstResult } from "../api";

type Props = {
  onPacket: (lane: "intake" | "event", id: string, ok: boolean) => void;
  onSettled: () => void;
};

const MAX_REQUESTS = 50;
const STAGGER_MS = 25;

function detailOf(body: unknown): string | undefined {
  if (body && typeof body === "object" && "detail" in body) {
    return String((body as { detail: unknown }).detail);
  }
  return undefined;
}

function percentile(values: number[], fraction: number): number {
  if (values.length === 0) return 0;
  const index = Math.min(values.length - 1, Math.floor(fraction * values.length));
  return values[index] ?? 0;
}

export function BurstPanel({ onPacket, onSettled }: Props) {
  const [count, setCount] = useState(20);
  const [reuseRef, setReuseRef] = useState(true);
  const [reuseKey, setReuseKey] = useState(false);
  const [results, setResults] = useState<BurstResult[]>([]);
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const scheduled = timers.current;
    return () => scheduled.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const inflight = results.filter((result) => result.outcome === "inflight").length;
  const created = results.filter((result) => result.outcome === "created").length;
  const replayed = results.filter((result) => result.outcome === "replayed").length;
  const conflicts = results.filter((result) => result.outcome === "conflict").length;
  const errors = results.filter((result) => result.outcome === "error").length;

  const latencies = results
    .map((result) => result.latencyMs)
    .filter((value): value is number => value !== null)
    .sort((left, right) => left - right);

  const fire = () => {
    if (inflight > 0) return;

    const runId = Date.now().toString(36).slice(-5);
    const sharedRef = `ui-${runId}`;
    const sharedKey = crypto.randomUUID();

    // Build the whole batch up front so every request is rendered as pending
    // before the first response comes back.
    const planned: BurstResult[] = Array.from({ length: count }, (_, index) => ({
      id: `${runId}-${index}`,
      orderRef: reuseRef ? sharedRef : `${sharedRef}-${index}`,
      idempotencyKey: reuseKey ? sharedKey : crypto.randomUUID(),
      status: null,
      outcome: "inflight",
      latencyMs: null,
    }));

    setResults(planned);

    let remaining = planned.length;

    planned.forEach((request, index) => {
      const timer = window.setTimeout(() => {
        onPacket("intake", `req-${request.id}`, true);

        void sendOrder({ order_ref: request.orderRef, ...BURST_CART }, request.idempotencyKey)
          .then((response) => {
            const outcome: BurstOutcome =
              response.status < 300
                ? response.replayed
                  ? "replayed"
                  : "created"
                : response.status === 409
                  ? "conflict"
                  : "error";

            setResults((previous) =>
              previous.map((item) =>
                item.id === request.id
                  ? {
                      ...item,
                      status: response.status,
                      outcome,
                      latencyMs: response.latencyMs,
                      detail: detailOf(response.body),
                    }
                  : item,
              ),
            );

            // Only a freshly created order publishes an event for the warehouse.
            if (outcome === "created") {
              onPacket("event", `evt-${request.id}`, true);
            }
          })
          .catch((caught: unknown) => {
            setResults((previous) =>
              previous.map((item) =>
                item.id === request.id
                  ? { ...item, outcome: "error", detail: String(caught) }
                  : item,
              ),
            );
          })
          .finally(() => {
            remaining -= 1;
            if (remaining === 0) onSettled();
          });
      }, index * STAGGER_MS);

      timers.current.push(timer);
    });
  };

  return (
    <section className="panel burst-panel">
      <header className="panel-head">
        <div>
          <h2>Concurrent order burst</h2>
          <p className="muted">
            Fires real requests at <code>POST /orders</code>. Duplicates share the order
            reference, so the service must collapse them into one order.
          </p>
        </div>
        <span className="badge">{results.length > 0 ? `${results.length} sent` : "idle"}</span>
      </header>

      <div className="burst-controls">
        <label className="field">
          <span>
            Requests <strong>{count}</strong>
          </span>
          <input
            type="range"
            min={1}
            max={MAX_REQUESTS}
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
          />
        </label>

        <label className="toggle">
          <input
            type="checkbox"
            checked={reuseRef}
            onChange={(event) => setReuseRef(event.target.checked)}
          />
          <span>Reuse one order_ref (DB idempotency)</span>
        </label>

        <label className="toggle">
          <input
            type="checkbox"
            checked={reuseKey}
            onChange={(event) => setReuseKey(event.target.checked)}
          />
          <span>Reuse one idempotency key (Redis replay)</span>
        </label>

        <button
          type="button"
          className="btn primary fire"
          onClick={fire}
          disabled={inflight > 0}
        >
          {inflight > 0 ? `Firing… ${inflight} in flight` : `Fire ${count} requests`}
        </button>
      </div>

      <div className="burst-stats">
        <div className="stat">
          <span className="stat-label">created</span>
          <span className="stat-value ok-text">{created}</span>
        </div>
        <div className="stat">
          <span className="stat-label">replayed</span>
          <span className="stat-value info-text">{replayed}</span>
        </div>
        <div className="stat">
          <span className="stat-label">conflict</span>
          <span className="stat-value warn-text">{conflicts}</span>
        </div>
        <div className="stat">
          <span className="stat-label">failed</span>
          <span className="stat-value bad-text">{errors}</span>
        </div>
        <div className="stat">
          <span className="stat-label">p50 / max</span>
          <span className="stat-value">
            {latencies.length > 0
              ? `${Math.round(percentile(latencies, 0.5))} / ${Math.round(percentile(latencies, 0.99))} ms`
              : "—"}
          </span>
        </div>
      </div>

      <div className="burst-grid" aria-live="polite">
        {results.length === 0 ? (
          <p className="muted burst-empty">
            No requests yet. Fire a burst and watch each response settle: one{" "}
            <span className="badge created">202</span> and the rest{" "}
            <span className="badge replayed">200 replayed</span> when duplicates are on.
          </p>
        ) : (
          results.map((result) => (
            <span
              key={result.id}
              className={`burst-chip outcome-${result.outcome}`}
              title={
                result.detail ??
                `${result.orderRef} · key ${result.idempotencyKey.slice(0, 8)}…`
              }
            >
              <span className="chip-status">{result.status ?? "···"}</span>
              <span className="chip-latency">
                {result.latencyMs === null ? "queued" : `${Math.round(result.latencyMs)}ms`}
              </span>
            </span>
          ))
        )}
      </div>
    </section>
  );
}
