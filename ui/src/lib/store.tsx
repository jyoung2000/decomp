import { createContext, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Api } from './api';
import { resolveConfig, wsUrl, type StudioConfig } from './config';
import { EventClient, type ClientSnapshot, type WebSocketLike } from './events';
import { applyEvent, applyHistory, applySnapshot, emptyCase, emptyStudio, type CaseState, type RefreshKey, type Snapshot, type StudioState } from './derive';
import type { ControllerEvent, Health } from './types';

type Listener = () => void;

export interface StoreOptions {
  createSocket?: (url: string) => WebSocketLike;
  fetchImpl?: typeof fetch;
}

const CASE_KEYS: RefreshKey[] = ['case', 'jobs', 'plan', 'features', 'candidates', 'previews', 'feedback'];

/** Owns the API client, the event client and the derived state. Views subscribe through hooks below. */
export class StudioStore {
  readonly api: Api;
  readonly config: StudioConfig;
  client: EventClient | null = null;
  state: StudioState = emptyStudio();
  health: Health | null = null;
  healthError: unknown = null;
  clientSnap: ClientSnapshot | null = null;
  private listeners = new Set<Listener>();
  private loadedCases = new Set<string>();
  private pending = new Map<string, ReturnType<typeof setTimeout>>();
  private bootTimer: ReturnType<typeof setTimeout> | null = null;
  private bootAttempt = 0;
  private stopped = false;
  private eventListeners = new Set<(ev: ControllerEvent) => void>();

  constructor(config: StudioConfig = resolveConfig(), private opts: StoreOptions = {}) {
    this.config = config;
    this.api = new Api(config, opts.fetchImpl);
  }

  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  private emit() {
    for (const fn of this.listeners) fn();
  }

  onControllerEvent(fn: (ev: ControllerEvent) => void) {
    this.eventListeners.add(fn);
    return () => {
      this.eventListeners.delete(fn);
    };
  }

  get isMock(): boolean {
    return this.config.mock || this.health?.mode === 'mock';
  }

  private gen = 0;

  async boot(): Promise<void> {
    if (this.bootTimer) clearTimeout(this.bootTimer);
    this.stopped = false;
    const gen = ++this.gen;
    try {
      const health = await this.api.health();
      if (gen !== this.gen || this.stopped) return;
      this.health = health;
      this.healthError = null;
      this.bootAttempt = 0;
      this.startClient(this.health.latest_seq ?? 0);
      for (const id of this.loadedCases) void this.loadCase(id, true);
    } catch (e) {
      if (gen !== this.gen || this.stopped) return;
      this.healthError = e;
      this.bootAttempt++;
      const delay = Math.min(15000, 500 * 2 ** Math.min(this.bootAttempt, 5));
      this.bootTimer = setTimeout(() => void this.boot(), delay);
    }
    this.emit();
  }

  stop() {
    this.stopped = true;
    if (this.bootTimer) clearTimeout(this.bootTimer);
    for (const t of this.pending.values()) clearTimeout(t);
    this.pending.clear();
    this.client?.stop();
    this.client = null;
    this.clientSnap = null;
  }

  private startClient(since: number) {
    if (this.client) return;
    const client = new EventClient({
      url: (s) => wsUrl(this.config, s),
      fetchSince: (s) => this.api.events(s),
      initialSeq: since,
      createSocket: this.opts.createSocket,
    });
    this.client = client;
    this.clientSnap = client.snapshot;
    let wasConnected = false;
    client.onStatus((snap) => {
      const reconnected = snap.status === 'connected' && !wasConnected && this.clientSnap?.connectedAt != null;
      wasConnected = snap.status === 'connected';
      this.clientSnap = snap;
      if (reconnected) {
        // replay covers up to 5000 events; snapshots make sure nothing older was missed
        void this.api.health().then((h) => {
          this.health = h;
          this.emit();
        }).catch(() => undefined);
        for (const id of this.loadedCases) for (const k of CASE_KEYS) this.scheduleRefresh(id, k);
      }
      this.emit();
    });
    client.onEvent((ev) => this.dispatch(ev));
    client.start();
  }

  dispatch(ev: ControllerEvent) {
    const r = applyEvent(this.state, ev);
    this.state = r.state;
    for (const { caseId, key } of r.refresh) if (caseId && this.loadedCases.has(caseId)) this.scheduleRefresh(caseId, key);
    for (const fn of this.eventListeners) fn(ev);
    this.emit();
  }

  caseState(caseId: string): CaseState {
    return this.state.cases[caseId] ?? emptyCase(caseId);
  }

  async loadCase(caseId: string, force = false): Promise<void> {
    if (this.loadedCases.has(caseId) && !force) return;
    this.loadedCases.add(caseId);
    await Promise.all([...CASE_KEYS.map((k) => this.refresh(caseId, k)), this.loadHistory(caseId)]);
  }

  /** Recent past events for this case (latest meaningful event + raw log) — display only, reducers are not re-run. */
  async loadHistory(caseId: string): Promise<void> {
    const latest = this.health?.latest_seq ?? this.client?.snapshot.lastSeq ?? 0;
    try {
      const evs = await this.api.events(Math.max(0, latest - 2000), caseId);
      if (Array.isArray(evs)) {
        this.state = applyHistory(this.state, caseId, evs);
        this.emit();
      }
    } catch {
      /* optional */
    }
  }

  scheduleRefresh(caseId: string, key: RefreshKey) {
    const id = `${caseId}:${key}`;
    if (this.pending.has(id)) return;
    this.pending.set(
      id,
      setTimeout(() => {
        this.pending.delete(id);
        void this.refresh(caseId, key);
      }, 150),
    );
  }

  async refresh(caseId: string, key: RefreshKey): Promise<void> {
    const asOf = this.client?.snapshot.lastSeq ?? this.health?.latest_seq ?? 0;
    const at = new Date().toISOString();
    try {
      let snap: Snapshot;
      switch (key) {
        case 'case':
          snap = { kind: 'case', data: await this.api.getCase(caseId) };
          break;
        case 'jobs':
          snap = { kind: 'jobs', data: await this.api.jobs(caseId) };
          break;
        case 'plan':
          snap = { kind: 'plan', data: await this.api.plan(caseId) };
          break;
        case 'features':
          snap = { kind: 'features', data: await this.api.features(caseId) };
          break;
        case 'candidates':
          snap = { kind: 'candidates', data: await this.api.candidates(caseId) };
          break;
        case 'previews':
          snap = { kind: 'previews', data: await this.api.previews(caseId) };
          break;
        case 'feedback':
          snap = { kind: 'feedback', data: await this.api.feedback(caseId) };
          break;
        default:
          return;
      }
      // apply against the *current* state (other snapshots/events may have landed while awaiting)
      this.state = applySnapshot(this.state, caseId, snap, asOf, at);
      this.emit();
    } catch {
      /* a failed snapshot leaves event-derived state in place; the banner shows connectivity */
    }
  }
}

const StoreCtx = createContext<StudioStore | null>(null);

export function StudioProvider({ store, children }: { store?: StudioStore; children: ReactNode }) {
  const ref = useRef<StudioStore | null>(store ?? null);
  if (!ref.current) ref.current = new StudioStore();
  useEffect(() => {
    const s = ref.current!;
    void s.boot();
    return () => s.stop();
  }, []);
  return <StoreCtx.Provider value={ref.current}>{children}</StoreCtx.Provider>;
}

export function useStore(): StudioStore {
  const s = useContext(StoreCtx);
  if (!s) throw new Error('StudioProvider missing');
  return s;
}

export function useApi() {
  return useStore().api;
}

export function useStoreSelector<T>(sel: (s: StudioStore) => T): T {
  const store = useStore();
  return useSyncExternalStore(store.subscribe, () => sel(store), () => sel(store));
}

export function useCaseState(caseId: string | null | undefined): CaseState | null {
  const store = useStore();
  useEffect(() => {
    if (caseId) void store.loadCase(caseId);
  }, [store, caseId]);
  return useStoreSelector((s) => (caseId ? s.state.cases[caseId] ?? null : null));
}

/** Re-render on a fixed cadence for relative times ("12 s ago"). Never used to advance progress. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Fetch REST data and refetch when `version` changes (bumped by matching events). */
export function useResource<T>(fetcher: (() => Promise<T>) | null, deps: unknown[], version: unknown = 0): Resource<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState<boolean>(!!fetcher);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    if (!fetcher) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    fetcher().then(
      (d) => {
        if (!alive) return;
        setData(d);
        setError(null);
        setLoading(false);
      },
      (e) => {
        if (!alive) return;
        setError(e);
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version, tick]);
  return { data, error, loading, reload };
}
