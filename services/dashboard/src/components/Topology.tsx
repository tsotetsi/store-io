import type { ServiceInfo, Summary } from "../api";

export type Packet = {
  id: string;
  lane: "intake" | "event";
  ok: boolean;
};

type Props = {
  packets: Packet[];
  services: ServiceInfo[];
  summary: Summary | null;
};

function healthOf(services: ServiceInfo[], name: string): string {
  const service = services.find((item) => item.name === name);
  return service?.health ?? (service?.state === "running" ? "running" : "stopped");
}

function statusClass(health: string): string {
  if (health === "healthy" || health === "running") return "dot ok";
  if (health === "starting") return "dot warn";
  return "dot bad";
}

function Lane({
  label,
  packets,
  reverse = false,
}: {
  label: string;
  packets: Packet[];
  reverse?: boolean;
}) {
  return (
    <div className="lane-row">
      <span className="lane-label">{label}</span>
      <div className={`lane ${reverse ? "lane-reverse" : ""}`}>
        <span className="lane-track" />
        <span className="lane-arrow">{reverse ? "◀" : "▶"}</span>
        {packets.map((packet) => (
          <span
            key={packet.id}
            className={`packet ${packet.ok ? "packet-ok" : "packet-info"} ${
              reverse ? "packet-reverse" : ""
            }`}
            aria-hidden="true"
          />
        ))}
        {packets.length > 0 && <span className="lane-count">{packets.length}</span>}
      </div>
    </div>
  );
}

export function Topology({ packets, services, summary }: Props) {
  const intake = packets.filter((packet) => packet.lane === "intake");
  const events = packets.filter((packet) => packet.lane === "event");

  const orderHealth = healthOf(services, "order-service");
  const warehouseHealth = healthOf(services, "warehouse-service");
  const brokerHealth = healthOf(services, "rabbitmq-service");
  const redisHealth = healthOf(services, "redis-service");

  const confirmed = summary?.orders.by_status.CONFIRMED ?? 0;
  const pending = summary?.orders.by_status.PENDING ?? 0;
  const failed = summary?.orders.by_status.FAILED ?? 0;

  return (
    <section className="topology" aria-label="Service topology">
      <article className="topology-node">
        <header>
          <span className={statusClass(orderHealth)} />
          <h3>Orders</h3>
        </header>
        <p className="topology-endpoint">POST /orders</p>
        <p className="topology-meta">FastAPI · Python</p>
        <dl className="topology-stats">
          <div>
            <dt>pending</dt>
            <dd>{pending}</dd>
          </div>
          <div>
            <dt>confirmed</dt>
            <dd className="ok-text">{confirmed}</dd>
          </div>
          <div>
            <dt>failed</dt>
            <dd className={failed > 0 ? "bad-text" : ""}>{failed}</dd>
          </div>
        </dl>
        <footer className="topology-db">
          <span className={statusClass(healthOf(services, "order-db-service"))} />
          PostgreSQL · orders
        </footer>
      </article>

      <div className="topology-links">
        <Lane label="POST /orders" packets={intake} />
        <div className="broker-chips">
          <span className="chip">
            <span className={statusClass(brokerHealth)} />
            RabbitMQ
          </span>
          <span className="chip">
            <span className={statusClass(redisHealth)} />
            Redis · idempotency
          </span>
        </div>
        <Lane label="order.placed" packets={events} />
        <Lane label="stock.allocated" packets={[]} reverse />
      </div>

      <article className="topology-node">
        <header>
          <span className={statusClass(warehouseHealth)} />
          <h3>Warehouse</h3>
        </header>
        <p className="topology-endpoint">GET /inventory/:sku</p>
        <p className="topology-meta">FastAPI · Python</p>
        <dl className="topology-stats">
          <div>
            <dt>SKUs</dt>
            <dd>{summary?.inventory.length ?? 0}</dd>
          </div>
          <div>
            <dt>units</dt>
            <dd>{summary?.inventory.reduce((total, item) => total + item.stock, 0) ?? 0}</dd>
          </div>
        </dl>
        <footer className="topology-db">
          <span className={statusClass(healthOf(services, "warehouse-db-service"))} />
          PostgreSQL · inventory
        </footer>
      </article>
    </section>
  );
}
