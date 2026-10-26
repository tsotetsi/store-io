import type { ServiceInfo } from "../api";
import { formatTime } from "../api";

const ROLES: Record<string, string> = {
  "order-service": "Order intake · pricing · status API",
  "warehouse-service": "Catalogue · stock allocation",
  "order-db-service": "PostgreSQL · orders",
  "warehouse-db-service": "PostgreSQL · inventory",
  "redis-service": "Idempotency keys · price cache",
  "rabbitmq-service": "Event broker · durable queues",
  "dashboard-service": "This UI · nginx",
  "control-service": "Docker control API",
};

type Props = {
  service: ServiceInfo;
  busy: boolean;
  logsOpen: boolean;
  onAction: (name: string, action: "start" | "stop" | "restart") => void;
  onToggleLogs: (name: string) => void;
};

function dotClass(service: ServiceInfo): string {
  if (service.state !== "running") return "dot bad";
  if (service.health === "starting") return "dot warn";
  if (service.health === "unhealthy") return "dot bad";
  return "dot ok";
}

export function ServiceCard({ service, busy, logsOpen, onAction, onToggleLogs }: Props) {
  const running = service.state === "running";
  const exposed = service.ports.find((port) => port.public !== null);
  // Stopping the control service takes this UI's API with it; restart is harmless.
  const isControlPlane = service.name === "control-service";

  return (
    <article className={`service-card ${running ? "" : "is-stopped"} ${logsOpen ? "is-open" : ""}`}>
      <header className="service-head">
        <span className={dotClass(service)} />
        <div>
          <h3>{service.name}</h3>
          <p className="muted">{ROLES[service.name] ?? "compose service"}</p>
        </div>
      </header>

      <dl className="service-meta">
        <div>
          <dt>state</dt>
          <dd>{service.state}</dd>
        </div>
        <div>
          <dt>health</dt>
          <dd>{service.health ?? "—"}</dd>
        </div>
        <div>
          <dt>port</dt>
          <dd>{exposed ? `${exposed.public} → ${exposed.private}` : "internal"}</dd>
        </div>
        <div>
          <dt>started</dt>
          <dd>{formatTime(service.started_at)}</dd>
        </div>
      </dl>

      <div className="service-actions">
        {running ? (
          <button
            type="button"
            className="btn danger"
            disabled={busy || isControlPlane}
            title={
              isControlPlane
                ? "Stopping this service disconnects the dashboard. Use `docker compose up -d control-service` to recover."
                : undefined
            }
            onClick={() => onAction(service.name, "stop")}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => onAction(service.name, "start")}
          >
            Start
          </button>
        )}
        <button
          type="button"
          className="btn"
          disabled={busy || !running}
          onClick={() => onAction(service.name, "restart")}
        >
          Restart
        </button>
        <button type="button" className="btn ghost" onClick={() => onToggleLogs(service.name)}>
          {logsOpen ? "Hide logs" : "Logs"}
        </button>
      </div>

      {service.restart_count > 0 && (
        <p className="service-restarts muted">restarts: {service.restart_count}</p>
      )}
    </article>
  );
}
