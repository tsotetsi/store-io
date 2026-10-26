import type { InventoryItem } from "../api";
import { formatCents } from "../api";

type Props = {
  items: InventoryItem[];
  unitsSold: Record<string, number>;
};

export function InventoryPanel({ items, unitsSold }: Props) {
  const maxStock = Math.max(1, ...items.map((item) => item.stock));

  return (
    <section className="panel inventory-panel">
      <header className="panel-head">
        <div>
          <h2>Stock</h2>
          <p className="muted">Live catalogue owned by the Warehouse Service.</p>
        </div>
        <span className="badge">{items.length} SKUs</span>
      </header>

      <ul className="inventory-list">
        {items.length === 0 ? (
          <li className="muted">Warehouse unavailable — start the service to see stock.</li>
        ) : (
          items.map((item) => {
            const width = Math.max(4, Math.round((item.stock / maxStock) * 100));
            return (
              <li key={item.sku} className="inventory-row">
                <div className="inventory-head">
                  <span className="mono">{item.sku}</span>
                  <span className="muted">{item.name}</span>
                  <span className="mono">{formatCents(item.price_cents)}</span>
                </div>
                <div className="inventory-bar">
                  <span style={{ width: `${width}%` }} />
                </div>
                <div className="inventory-foot muted">
                  <span>{item.stock} in stock</span>
                  <span>{unitsSold[item.sku] ?? 0} sold</span>
                </div>
              </li>
            );
          })
        )}
      </ul>
    </section>
  );
}
