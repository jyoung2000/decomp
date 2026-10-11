import { useEffect, useState } from 'react';

/** Loads one request keyed by `key`; stale answers (an older selection) are dropped. Shared by the function pane's views. */
export function useLoad<T>(key: string, load: () => Promise<T>) {
  const [state, setState] = useState<{ key: string; data?: T; error?: unknown } | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setState(null);
    load().then(
      (data) => alive && setState({ key, data }),
      (error) => alive && setState({ key, error }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);
  const current = state && state.key === key ? state : null;
  return { data: current?.data, error: current?.error, loading: !current, reload: () => setTick((t) => t + 1) };
}
