import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const MAX_LINES = 500;

function colourFor(line: string): string {
  if (line.includes("ERROR") || line.includes("error") || line.includes("Traceback")) return "log-line log-error";
  if (line.includes("WARNING") || line.includes("warning")) return "log-line log-warn";
  if (line.includes("Published") || line.includes("confirmed") || line.includes("allocated"))
    return "log-line log-ok";
  return "log-line";
}

export function LogStream({ service }: { service: string }) {
  const [lines, setLines] = useState<string[]>([]);
  const [autoscroll, setAutoscroll] = useState(true);
  const [connected, setConnected] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  // Seed with recent history, then follow the live SSE stream.
  useEffect(() => {
    let alive = true;
    setLines([]);

    api
      .logs(service, 150)
      .then((result) => {
        if (alive) setLines(result.lines.slice(-MAX_LINES));
      })
      .catch(() => undefined);

    const source = new EventSource(`/api/services/${service}/logs/stream?tail=0`);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (event: MessageEvent<string>) => {
      try {
        const line = JSON.parse(event.data) as string;
        setLines((previous) =>
          previous.length >= MAX_LINES
            ? [...previous.slice(previous.length - MAX_LINES + 1), line]
            : [...previous, line],
        );
      } catch {
        // Ignore malformed frames rather than killing the stream.
      }
    };

    return () => {
      alive = false;
      source.close();
    };
  }, [service]);

  useEffect(() => {
    if (autoscroll && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines, autoscroll]);

  return (
    <div className="logs">
      <header className="logs-header">
        <div className="logs-title">
          <span className={connected ? "dot ok" : "dot bad"} />
          <strong>{service}</strong>
          <span className="muted">live logs</span>
        </div>
        <div className="logs-actions">
          <button type="button" className="btn ghost sm" onClick={() => setLines([])}>
            Clear
          </button>
          <button
            type="button"
            className={`btn ghost sm ${autoscroll ? "is-on" : ""}`}
            onClick={() => setAutoscroll((value) => !value)}
          >
            Autoscroll {autoscroll ? "on" : "off"}
          </button>
        </div>
      </header>
      <div className="logs-body" ref={boxRef}>
        {lines.length === 0 ? (
          <p className="muted">Waiting for output…</p>
        ) : (
          lines.map((line, index) => (
            <div key={`${index}-${line.slice(0, 24)}`} className={colourFor(line)}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
