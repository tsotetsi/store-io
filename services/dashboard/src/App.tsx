import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatCents } from "./api";
import { BurstPanel } from "./components/BurstPanel";
import { InventoryPanel } from "./components/InventoryPanel";
import { LogStream } from "./components/LogStream";
import { OrdersTable } from "./components/OrdersTable";
import { ServiceCard } from "./components/ServiceCard";
import { Topology, type Packet } from "./components/Topology";
import { usePoll } from "./usePoll";

const PACKET_LIFETIME_MS = 1200;

export default function App() {
  const services = usePoll((signal) => api.services(signal), 2000);
  const summary = usePoll((signal) => api.summary(signal), 2500);
  const orders = usePoll((signal) => api.orders(25, signal), 3000);

  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [packets, setPackets] = useState<Packet[]>([]);

  const packetTimers = useRef<number[]>([]);
  const toastTimer = useRef<number | null>(null);

  useEffect(() => {
    const scheduled = packetTimers.current;
    return () => {
      scheduled.forEach((timer) => window.clearTimeout(timer));
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const notify = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  const spawnPacket = useCallback((lane: Packet["lane"], id: string, ok: boolean) => {
    setPackets((previous) => {
      const next = [...previous, { id, lane, ok }];
      // Keep the DOM bounded during very large bursts.
      return next.length > 40 ? next.slice(next.length - 40) : next;
    });

    const timer = window.setTimeout(() => {
      setPackets((previous) => previous.filter((packet) => packet.id !== id));
    }, PACKET_LIFETIME_MS);

    packetTimers.current.push(timer);
  }, []);

  const handleAction = useCallback(
    (name: string, action: "start" | "stop" | "restart") => {
      setBusy(name);
      api
        .serviceAction(name, action)
        .then((result) => {
          notify(`${name}: ${action} done (${result.service.state})`);
        })
        .catch((error: unknown) => {
          notify(`${name}: ${action} failed — ${String(error)}`);
        })
        .finally(() => {
          setBusy(null);
          services.refresh();
          summary.refresh();
        });
    },
    [notify, services, summary],
  );

  const refreshAfterBurst = useCallback(() => {
    orders.refresh();
    summary.refresh();
    services.refresh();
  }, [orders, summary, services]);

  const serviceList = services.data?.services ?? [];
  const running = serviceList.filter((service) => service.state === "running").length;
  const unhealthy = serviceList.filter(
    (service) => service.state === "running" && service.health === "unhealthy",
  ).length;

  const stats = summary.data?.orders;
  const cachedPricing = stats?.cached_pricing ?? 0;

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ◈
          </span>
          <div>
            <h1>Store-IO</h1>
            <p className="muted">Orders &amp; stock control room</p>
          </div>
        </div>

        <div className="header-meta">
          <span className={`chip ${unhealthy > 0 ? "chip-bad" : ""}`}>
            <span className={unhealthy > 0 ? "dot bad" : "dot ok"} />
            {running}/{serviceList.length || "—"} services up
          </span>
          {services.error && <span className="chip chip-bad">control API unreachable</span>}
          <a className="btn ghost" href="http://localhost:8000/docs" target="_blank" rel="noreferrer">
            Order API
          </a>
          <a
            className="btn ghost"
            href="http://localhost:8001/docs"
            target="_blank"
            rel="noreferrer"
          >
            Warehouse API
          </a>
        </div>
      </header>

      {toast && <div className="toast">{toast}</div>}

      <main className="app-main">
        <Topology packets={packets} services={serviceList} summary={summary.data} />

        <section className="tiles">
          <article className="tile">
            <span className="stat-label">orders</span>
            <strong>{stats?.total ?? 0}</strong>
            <span className="muted">
              {stats?.by_status.PENDING ?? 0} pending · {stats?.by_status.CONFIRMED ?? 0} confirmed
            </span>
          </article>
          <article className="tile">
            <span className="stat-label">confirmed revenue</span>
            <strong>{formatCents(stats?.revenue_cents ?? 0)}</strong>
            <span className="muted">sum of confirmed orders</span>
          </article>
          <article className="tile">
            <span className="stat-label">units sold</span>
            <strong>
              {Object.values(stats?.units_sold ?? {}).reduce((total, value) => total + value, 0)}
            </strong>
            <span className="muted">across confirmed orders</span>
          </article>
          <article className={`tile ${cachedPricing > 0 ? "tile-warn" : ""}`}>
            <span className="stat-label">priced from cache</span>
            <strong>{cachedPricing}</strong>
            <span className="muted">orders accepted while warehouse was down</span>
          </article>
        </section>

        <section className="split">
          <BurstPanel onPacket={spawnPacket} onSettled={refreshAfterBurst} />
          <InventoryPanel
            items={summary.data?.inventory ?? []}
            unitsSold={stats?.units_sold ?? {}}
          />
        </section>

        <OrdersTable orders={orders.data ?? []} error={orders.error} />

        <section className="panel services-panel">
          <header className="panel-head">
            <div>
              <h2>Services</h2>
              <p className="muted">
                Stop the warehouse mid-burst to see orders queue as PENDING, then start it again
                and watch stock catch up.
              </p>
            </div>
            <span className="badge">{serviceList.length} containers</span>
          </header>

          <div className="service-grid">
            {serviceList.length === 0 ? (
              <p className="muted">
                No services reported. Is the control service running with access to the Docker
                socket?
              </p>
            ) : (
              serviceList.map((service) => (
                <ServiceCard
                  key={service.name}
                  service={service}
                  busy={busy === service.name}
                  logsOpen={logsFor === service.name}
                  onAction={handleAction}
                  onToggleLogs={(name) =>
                    setLogsFor((current) => (current === name ? null : name))
                  }
                />
              ))
            )}
          </div>

          {logsFor && <LogStream service={logsFor} />}
        </section>
      </main>

      <footer className="app-footer">
        <span className="muted">
          Local demo stack · Orders → RabbitMQ → Warehouse · PostgreSQL for durable state
        </span>
        <span className="muted">refresh: services 2s · orders 3s</span>
      </footer>
    </div>
  );
}
