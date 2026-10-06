// Controller event client: WebSocket `/ws?token&since=<seq>` with seq dedupe, in-order delivery, gap filling over REST,
// exponential reconnect that replays from the last contiguous seq, and `lastEventAt` for stale detection.
// State derivation (jobs/plan/features/...) lives in ./derive.ts and is re-exported here; it uses only events and REST
// snapshots — never timers — so nothing in the UI can "progress" on its own.
import type { ControllerEvent } from './types';

export * from './derive';

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

export interface WebSocketLike {
  onopen: ((ev?: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev?: unknown) => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  close(): void;
}

export interface EventClientOptions {
  /** Builds the WebSocket URL for a given `since` seq. */
  url: (since: number) => string;
  /** REST fallback used to fill gaps: GET /events?since=N */
  fetchSince?: (since: number) => Promise<ControllerEvent[]>;
  createSocket?: (url: string) => WebSocketLike;
  now?: () => number;
  initialSeq?: number;
  backoff?: { initialMs: number; maxMs: number; factor: number };
  /** How long an out-of-order event waits for the missing seqs before a REST fill (then delivered anyway). */
  gapTimeoutMs?: number;
  /** After this many consecutive failed attempts the status reads "disconnected" (retries continue at max delay). */
  disconnectedAfter?: number;
}

export interface ClientSnapshot {
  status: ConnectionStatus;
  /** Highest seq delivered. */
  lastSeq: number;
  /** All seqs <= this have been delivered or declared absent; reconnects replay from here. */
  contiguousSeq: number;
  /** Client clock (ms) when any event was last received; null if none yet. */
  lastEventAt: number | null;
  /** Client clock (ms) of the last successful socket open. */
  connectedAt: number | null;
  reconnectAttempt: number;
  nextRetryAt: number | null;
  duplicatesDropped: number;
}

export function isControllerEvent(x: unknown): x is ControllerEvent {
  if (!x || typeof x !== 'object') return false;
  const o = x as Record<string, unknown>;
  return typeof o.seq === 'number' && Number.isFinite(o.seq) && typeof o.kind === 'string';
}

export class EventClient {
  private opts: Required<Omit<EventClientOptions, 'fetchSince'>> & Pick<EventClientOptions, 'fetchSince'>;
  private socket: WebSocketLike | null = null;
  private stopped = true;
  private delivered = new Set<number>();
  private buffer = new Map<number, ControllerEvent>();
  private gapTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<(ev: ControllerEvent) => void>();
  private statusListeners = new Set<(s: ClientSnapshot) => void>();
  private snap: ClientSnapshot;
  private floorSeq: number;

  constructor(options: EventClientOptions) {
    this.opts = {
      createSocket: (u: string) => new WebSocket(u) as unknown as WebSocketLike,
      now: () => Date.now(),
      initialSeq: 0,
      backoff: { initialMs: 500, maxMs: 15000, factor: 2 },
      gapTimeoutMs: 1500,
      disconnectedAfter: 4,
      ...(Object.fromEntries(Object.entries(options).filter(([, v]) => v !== undefined)) as EventClientOptions),
    };
    this.floorSeq = this.opts.initialSeq;
    this.snap = {
      status: 'idle',
      lastSeq: this.opts.initialSeq,
      contiguousSeq: this.opts.initialSeq,
      lastEventAt: null,
      connectedAt: null,
      reconnectAttempt: 0,
      nextRetryAt: null,
      duplicatesDropped: 0,
    };
  }

  get snapshot(): ClientSnapshot {
    return this.snap;
  }

  onEvent(fn: (ev: ControllerEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: (s: ClientSnapshot) => void): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  /** Re-anchor (e.g. after REST snapshots were loaded at `seq`). Only moves forward. */
  setBaseline(seq: number): void {
    this.floorSeq = Math.max(this.floorSeq, seq);
    if (seq > this.snap.contiguousSeq) {
      for (const [s] of this.buffer) if (s <= seq) this.buffer.delete(s);
      this.update({ contiguousSeq: seq, lastSeq: Math.max(this.snap.lastSeq, seq) });
      this.drain();
    }
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect('connecting');
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    const s = this.socket;
    this.socket = null;
    if (s) {
      s.onclose = null;
      s.onmessage = null;
      s.onerror = null;
      try {
        s.close();
      } catch {
        /* ignore */
      }
    }
    this.update({ status: 'idle', nextRetryAt: null });
  }

  /** Force a reconnect now (e.g. user pressed "Retry"). */
  reconnectNow(): void {
    if (this.stopped) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.dropSocket();
    this.connect(this.snap.reconnectAttempt >= this.opts.disconnectedAfter ? 'disconnected' : 'reconnecting');
  }

  /** Feed an event obtained from any source (WS frame or REST). Returns true if it was new. */
  ingest(ev: ControllerEvent): boolean {
    this.update({ lastEventAt: this.opts.now() });
    const seq = ev.seq;
    if (this.delivered.has(seq) || this.buffer.has(seq) || seq <= this.floor) {
      this.update({ duplicatesDropped: this.snap.duplicatesDropped + 1 });
      return false;
    }
    if (seq <= this.snap.contiguousSeq) {
      // A late event inside a range previously declared absent: deliver it; reducers guard per entity by seq.
      this.deliver(ev);
      return true;
    }
    if (seq === this.snap.contiguousSeq + 1) {
      this.deliver(ev);
      this.update({ contiguousSeq: seq });
      this.drain();
      return true;
    }
    this.buffer.set(seq, ev);
    this.armGapTimer();
    return true;
  }

  // --- internals -----------------------------------------------------------------------------------------------
  /** Seqs at or below the baseline are never delivered (they are covered by REST snapshots). */
  private get floor(): number {
    return this.floorSeq;
  }

  private deliver(ev: ControllerEvent) {
    this.delivered.add(ev.seq);
    if (this.delivered.size > 20000) {
      const cut = this.snap.contiguousSeq - 10000;
      for (const s of this.delivered) if (s < cut) this.delivered.delete(s);
    }
    if (ev.seq > this.snap.lastSeq) this.update({ lastSeq: ev.seq });
    for (const fn of this.listeners) {
      try {
        fn(ev);
      } catch (e) {
        console.error('event listener failed', e);
      }
    }
  }

  private drain() {
    let next = this.snap.contiguousSeq + 1;
    while (this.buffer.has(next)) {
      const ev = this.buffer.get(next)!;
      this.buffer.delete(next);
      this.deliver(ev);
      this.update({ contiguousSeq: next });
      next++;
    }
    if (this.buffer.size === 0 && this.gapTimer) {
      clearTimeout(this.gapTimer);
      this.gapTimer = null;
    }
  }

  private armGapTimer() {
    if (this.gapTimer) return;
    this.gapTimer = setTimeout(() => {
      this.gapTimer = null;
      void this.fillGap();
    }, this.opts.gapTimeoutMs);
  }

  private async fillGap() {
    if (this.buffer.size === 0) return;
    if (this.opts.fetchSince) {
      try {
        const evs = await this.opts.fetchSince(this.snap.contiguousSeq);
        for (const ev of evs.sort((a, b) => a.seq - b.seq)) if (isControllerEvent(ev)) this.ingest(ev);
      } catch {
        /* fall through: deliver what we have */
      }
    }
    if (this.buffer.size === 0) return;
    // Missing seqs never arrived: declare them absent and deliver the buffered events in order.
    const seqs = [...this.buffer.keys()].sort((a, b) => a - b);
    for (const s of seqs) {
      const ev = this.buffer.get(s)!;
      this.buffer.delete(s);
      this.deliver(ev);
      this.update({ contiguousSeq: s });
    }
    this.drain();
  }

  private connect(status: ConnectionStatus) {
    if (this.stopped) return;
    this.update({ status, nextRetryAt: null });
    let sock: WebSocketLike;
    try {
      sock = this.opts.createSocket(this.opts.url(this.snap.contiguousSeq));
    } catch {
      this.scheduleRetry();
      return;
    }
    this.socket = sock;
    sock.onopen = () => {
      if (this.socket !== sock) return;
      this.update({ status: 'connected', reconnectAttempt: 0, connectedAt: this.opts.now() });
    };
    sock.onmessage = (m) => {
      if (this.socket !== sock) return;
      let data: unknown;
      try {
        data = typeof m.data === 'string' ? JSON.parse(m.data) : m.data;
      } catch {
        return;
      }
      const list = Array.isArray(data) ? data : [data];
      for (const ev of list) if (isControllerEvent(ev)) this.ingest(ev);
    };
    sock.onerror = () => {
      /* onclose follows */
    };
    sock.onclose = () => {
      if (this.socket !== sock) return;
      this.socket = null;
      this.scheduleRetry();
    };
  }

  private dropSocket() {
    const s = this.socket;
    this.socket = null;
    if (s) {
      s.onclose = null;
      s.onmessage = null;
      try {
        s.close();
      } catch {
        /* ignore */
      }
    }
  }

  private scheduleRetry() {
    if (this.stopped) return;
    const attempt = this.snap.reconnectAttempt + 1;
    const { initialMs, maxMs, factor } = this.opts.backoff;
    const delay = Math.min(maxMs, initialMs * Math.pow(factor, attempt - 1));
    const status: ConnectionStatus = attempt >= this.opts.disconnectedAfter ? 'disconnected' : 'reconnecting';
    this.update({ status, reconnectAttempt: attempt, nextRetryAt: this.opts.now() + delay });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect(status);
    }, delay);
  }

  private clearTimers() {
    if (this.gapTimer) clearTimeout(this.gapTimer);
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.gapTimer = null;
    this.retryTimer = null;
  }

  private update(p: Partial<ClientSnapshot>) {
    let changed = false;
    for (const k of Object.keys(p) as (keyof ClientSnapshot)[]) {
      if (this.snap[k] !== p[k]) {
        changed = true;
        break;
      }
    }
    if (!changed) return;
    this.snap = { ...this.snap, ...p };
    for (const fn of this.statusListeners) fn(this.snap);
  }
}

export interface Staleness {
  stale: boolean;
  reason: 'disconnected' | 'no_events' | 'never_received' | null;
  /** ms since last event, null when none */
  ageMs: number | null;
  thresholdMs: number;
}

/** UI must mark state stale/unknown when no event (any kind) arrived for 2× heartbeat interval or the WS is down. */
export function computeStaleness(args: {
  now: number;
  lastEventAt: number | null;
  heartbeatSeconds: number | null | undefined;
  status: ConnectionStatus;
  connectedAt?: number | null;
}): Staleness {
  const hb = args.heartbeatSeconds && args.heartbeatSeconds > 0 ? args.heartbeatSeconds : 30;
  const thresholdMs = 2 * hb * 1000;
  const ageMs = args.lastEventAt == null ? null : Math.max(0, args.now - args.lastEventAt);
  if (args.status !== 'connected') return { stale: true, reason: 'disconnected', ageMs, thresholdMs };
  if (ageMs == null) {
    // connected but nothing yet: give the server one threshold window from connect before calling it stale
    const since = args.connectedAt != null ? args.now - args.connectedAt : Infinity;
    return { stale: since > thresholdMs, reason: since > thresholdMs ? 'never_received' : null, ageMs, thresholdMs };
  }
  if (ageMs > thresholdMs) return { stale: true, reason: 'no_events', ageMs, thresholdMs };
  return { stale: false, reason: null, ageMs, thresholdMs };
}
