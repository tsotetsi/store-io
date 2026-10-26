import type { Order } from "../api";
import { formatCents, formatTime } from "../api";

type Props = {
  orders: Order[];
  error: string | null;
};

function statusClass(status: string): string {
  if (status === "CONFIRMED") return "pill pill-confirmed";
  if (status === "FAILED") return "pill pill-failed";
  return "pill pill-pending";
}

export function OrdersTable({ orders, error }: Props) {
  return (
    <section className="panel orders-panel">
      <header className="panel-head">
        <div>
          <h2>Orders</h2>
          <p className="muted">
            Newest first. PENDING means the order is stored and the stock event is queued;
            CONFIRMED means the warehouse deducted stock.
          </p>
        </div>
        <span className="badge">{orders.length} shown</span>
      </header>

      {error && <p className="alert bad">{error}</p>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>order_ref</th>
              <th>customer</th>
              <th>items</th>
              <th>status</th>
              <th className="align-right">total</th>
              <th>pricing</th>
              <th>created</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={7} className="muted">
                  No orders yet — fire a burst above.
                </td>
              </tr>
            ) : (
              orders.map((order) => (
                <tr key={order.order_ref}>
                  <td className="mono">{order.order_ref}</td>
                  <td>{order.customer_id}</td>
                  <td className="muted">
                    {order.items
                      .map((item) => `${item.sku} ×${item.qty}`)
                      .join(" · ")}
                  </td>
                  <td>
                    {/* Keying on the status replays the animation when it flips. */}
                    <span key={order.status} className={statusClass(order.status)}>
                      {order.status}
                    </span>
                  </td>
                  <td className="align-right mono">{formatCents(order.total_cents)}</td>
                  <td>
                    <span className={`badge ${order.pricing_source === "cached" ? "warn" : ""}`}>
                      {order.pricing_source}
                    </span>
                  </td>
                  <td className="muted">{formatTime(order.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
