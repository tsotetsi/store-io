import { useEffect, useRef, useState } from "react";

type PollState<T> = {
  data: T | null;
  error: string | null;
  refresh: () => void;
};

/**
 * Poll an async loader, scheduling the next call only after the previous one
 * settles. This keeps a 2s refresh steady even when a service is stopped and the
 * request hangs until the timeout.
 */
export function usePoll<T>(load: (signal: AbortSignal) => Promise<T>, intervalMs: number): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();

    const tick = async () => {
      try {
        const next = await loadRef.current(controller.signal);
        if (!cancelled) {
          setData(next);
          setError(null);
        }
      } catch (caught) {
        if (!cancelled && (caught as Error).name !== "AbortError") {
          setError((caught as Error).message);
        }
      }

      if (!cancelled) {
        timer = window.setTimeout(() => void tick(), intervalMs);
      }
    };

    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [intervalMs, nonce]);

  return { data, error, refresh: () => setNonce((value) => value + 1) };
}
