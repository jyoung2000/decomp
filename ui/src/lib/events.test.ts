import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventClient, computeStaleness, type WebSocketLike } from './events';
import {
  applyEvent, applyHistory, applySnapshot, describeScopeNote, emptyStudio, phaseViews, progressPercent, values, type StudioState,
} from './derive';
import type { ControllerEvent, Job, Plan } from './types';

class FakeSocket implements WebSocketLike {
  onopen: WebSocketLike['onopen'] = null;
  onmessage: WebSocketLike['onmessage'] = null;
  onclose: WebSocketLike['onclose'] = null;
  onerror: WebSocketLike['onerror'] = null;
  closed = false;
  constructor(public url: string) {}
  open() {
    this.onopen?.();
  }
  send(ev: ControllerEvent | ControllerEvent[]) {
    this.onmessage?.({ data: JSON.stringify(ev) });
  }
  drop() {
    this.onclose?.();
  }
  close() {
    this.closed = true;
  }
}

const ev = (seq: number, kind = 'job.log', payload: Record<string, unknown> = {}, case_id: string | null = 'c1', job_id: string | null = null): ControllerEvent => ({
  seq,
  ts: new Date(1_700_000_000_000 + seq * 1000).toISOString(),
  case_id,
  job_id,
  kind,
  payload,
});

function setup(opts: Partial<ConstructorParameters<typeof EventClient>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  let clock = 1_000;
  const delivered: number[] = [];
  const client = new EventClient({
    url: (since) => `ws://x/ws?token=t&since=${since}`,
    createSocket: (u) => {
      const s = new FakeSocket(u);
      sockets.push(s);
      return s;
    },
    now: () => clock,
    ...opts,
  });
  client.onEvent((e) => delivered.push(e.seq));
  return { client, sockets, delivered, tick: (ms: number) => (clock += ms), get clock() { return clock; } };
}

describe('EventClient', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('connects with since=<initial seq> and reports connected', () => {
    const t = setup({ initialSeq: 41 });
    t.client.start();
    expect(t.sockets[0].url).toContain('since=41');
    expect(t.client.snapshot.status).toBe('connecting');
    t.sockets[0].open();
    expect(t.client.snapshot.status).toBe('connected');
  });

  it('drops duplicate seqs', () => {
    const t = setup();
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send(ev(1));
    t.sockets[0].send(ev(1));
    t.sockets[0].send(ev(2));
    t.sockets[0].send([ev(2), ev(3)]);
    expect(t.delivered).toEqual([1, 2, 3]);
    expect(t.client.snapshot.duplicatesDropped).toBe(2);
    expect(t.client.snapshot.lastSeq).toBe(3);
  });

  it('ignores events at or below the snapshot baseline', () => {
    const t = setup({ initialSeq: 10 });
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send([ev(9), ev(10), ev(11)]);
    expect(t.delivered).toEqual([11]);
  });

  it('re-orders out-of-order events and delivers them in seq order', () => {
    const t = setup();
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send(ev(1));
    t.sockets[0].send(ev(3));
    t.sockets[0].send(ev(4));
    expect(t.delivered).toEqual([1]);
    t.sockets[0].send(ev(2));
    expect(t.delivered).toEqual([1, 2, 3, 4]);
    expect(t.client.snapshot.contiguousSeq).toBe(4);
  });

  it('fills a gap over REST, then delivers in order', async () => {
    const fetchSince = vi.fn(async (since: number) => [ev(since + 1), ev(since + 2)]);
    const t = setup({ fetchSince, gapTimeoutMs: 100 });
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send(ev(1));
    t.sockets[0].send(ev(4));
    expect(t.delivered).toEqual([1]);
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchSince).toHaveBeenCalledWith(1);
    expect(t.delivered).toEqual([1, 2, 3, 4]);
  });

  it('declares missing seqs absent when REST cannot supply them', async () => {
    const t = setup({ fetchSince: async () => [], gapTimeoutMs: 50 });
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send(ev(1));
    t.sockets[0].send(ev(5));
    await vi.advanceTimersByTimeAsync(50);
    expect(t.delivered).toEqual([1, 5]);
    expect(t.client.snapshot.contiguousSeq).toBe(5);
    // a late arrival inside the absent range is still delivered once (reducers guard by seq)
    t.sockets[0].send(ev(3));
    t.sockets[0].send(ev(3));
    expect(t.delivered).toEqual([1, 5, 3]);
  });

  it('reconnects with exponential backoff and replays from the last contiguous seq', () => {
    const t = setup({ backoff: { initialMs: 100, maxMs: 1000, factor: 2 }, disconnectedAfter: 3 });
    t.client.start();
    t.sockets[0].open();
    t.sockets[0].send([ev(1), ev(2), ev(3)]);
    t.sockets[0].drop();
    expect(t.client.snapshot.status).toBe('reconnecting');
    expect(t.client.snapshot.nextRetryAt).toBe(t.clock + 100);
    vi.advanceTimersByTime(100);
    expect(t.sockets[1].url).toContain('since=3');
    t.sockets[1].drop(); // attempt 2 fails → 200 ms
    vi.advanceTimersByTime(199);
    expect(t.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(3);
    t.sockets[2].drop(); // attempt 3 → disconnected, 400 ms
    expect(t.client.snapshot.status).toBe('disconnected');
    expect(t.client.snapshot.reconnectAttempt).toBe(3);
    vi.advanceTimersByTime(400);
    const s = t.sockets[3];
    s.open();
    expect(t.client.snapshot.status).toBe('connected');
    expect(t.client.snapshot.reconnectAttempt).toBe(0);
    // server replays everything after since=3 — including an overlap that must be deduped
    s.send([ev(3), ev(4), ev(5)]);
    expect(t.delivered).toEqual([1, 2, 3, 4, 5]);
  });

  it('caps the backoff at maxMs', () => {
    const t = setup({ backoff: { initialMs: 100, maxMs: 300, factor: 2 } });
    t.client.start();
    for (let i = 0; i < 5; i++) {
      t.sockets[t.sockets.length - 1].drop();
      const wait = t.client.snapshot.nextRetryAt! - t.clock;
      expect(wait).toBeLessThanOrEqual(300);
      vi.advanceTimersByTime(wait);
    }
    t.sockets[t.sockets.length - 1].drop();
    expect(t.client.snapshot.nextRetryAt! - t.clock).toBe(300);
  });

  it('tracks lastEventAt for every received event (including duplicates and heartbeats)', () => {
    const t = setup();
    t.client.start();
    t.sockets[0].open();
    expect(t.client.snapshot.lastEventAt).toBeNull();
    t.sockets[0].send(ev(1, 'controller.heartbeat', {}, null));
    expect(t.client.snapshot.lastEventAt).toBe(1000);
    t.tick(5000);
    t.sockets[0].send(ev(1, 'controller.heartbeat', {}, null));
    expect(t.client.snapshot.lastEventAt).toBe(6000);
  });

  it('stop() closes the socket and does not reconnect', () => {
    const t = setup();
    t.client.start();
    t.sockets[0].open();
    t.client.stop();
    expect(t.sockets[0].closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(t.sockets).toHaveLength(1);
    expect(t.client.snapshot.status).toBe('idle');
  });
});

describe('computeStaleness', () => {
  const base = { heartbeatSeconds: 30, status: 'connected' as const };
  it('is live when an event arrived within 2× heartbeat', () => {
    expect(computeStaleness({ ...base, now: 60_000, lastEventAt: 1 }).stale).toBe(false);
  });
  it('is stale after 2× heartbeat without any event', () => {
    const s = computeStaleness({ ...base, now: 60_001 + 1, lastEventAt: 1 });
    expect(s).toMatchObject({ stale: true, reason: 'no_events', thresholdMs: 60_000 });
  });
  it('is unknown whenever the socket is not connected', () => {
    expect(computeStaleness({ ...base, status: 'reconnecting', now: 10, lastEventAt: 9 })).toMatchObject({ stale: true, reason: 'disconnected' });
  });
  it('gives a freshly connected socket one window before calling it stale', () => {
    expect(computeStaleness({ ...base, now: 10_000, lastEventAt: null, connectedAt: 0 }).stale).toBe(false);
    expect(computeStaleness({ ...base, now: 61_000, lastEventAt: null, connectedAt: 0 })).toMatchObject({ stale: true, reason: 'never_received' });
  });
  it('defaults to 30 s heartbeat when unknown', () => {
    expect(computeStaleness({ status: 'connected', heartbeatSeconds: null, now: 59_000, lastEventAt: 0 }).stale).toBe(false);
  });
});

describe('derive: jobs', () => {
  const job: Job = { job_id: 'j1', case_id: 'c1', stage: 'inventory', title: 'Inventory', state: 'queued', attempt: 0, progress: {}, blocker: null, heartbeat_at: null };
  const run = (events: ControllerEvent[], s: StudioState = emptyStudio()) => events.reduce((acc, e) => applyEvent(acc, e).state, s);

  it('follows the job lifecycle from events only', () => {
    const s = run([
      ev(1, 'job.created', { job }, 'c1', 'j1'),
      ev(2, 'job.started', { job_id: 'j1', attempt: 1 }, 'c1', 'j1'),
      ev(3, 'job.progress', { job_id: 'j1', progress: { done: 3, total: null, unit: 'files' } }, 'c1', 'j1'),
    ]);
    const j = s.cases.c1.jobs.j1.value;
    expect(j.state).toBe('running');
    expect(j.attempt).toBe(1);
    expect(j.progress).toEqual({ done: 3, total: null, unit: 'files' });
    expect(progressPercent(j.progress)).toBeNull();
  });

  it('never lets an older event overwrite newer state', () => {
    const s = run([ev(1, 'job.created', { job }, 'c1', 'j1'), ev(5, 'job.completed', { job_id: 'j1' }, 'c1', 'j1'), ev(4, 'job.progress', { job_id: 'j1', progress: { done: 1, total: 2 } }, 'c1', 'j1')]);
    expect(s.cases.c1.jobs.j1.value.state).toBe('completed');
    expect(s.cases.c1.jobs.j1.value.progress).toEqual({});
  });

  it('requests a jobs snapshot when an event names an unknown job', () => {
    const r = applyEvent(emptyStudio(), ev(1, 'job.started', { job_id: 'jx', attempt: 1, stage: 'build' }, 'c1', 'jx'));
    expect(r.refresh).toContainEqual({ caseId: 'c1', key: 'jobs' });
    expect(r.state.cases.c1.jobs.jx.value.state).toBe('running');
  });

  it('records blockers, retries and needs_retest', () => {
    const s = run([
      ev(1, 'job.created', { job }, 'c1', 'j1'),
      ev(2, 'job.blocked', { job_id: 'j1', blocker: 'dependency j0 failed' }, 'c1', 'j1'),
      ev(3, 'job.unblocked', { job_id: 'j1' }, 'c1', 'j1'),
      ev(4, 'job.retry', { job_id: 'j1', attempt: 2, error: 'timeout' }, 'c1', 'j1'),
      ev(5, 'job.needs_retest', { job_id: 'j1', reason: 'knowledge rolled back' }, 'c1', 'j1'),
    ]);
    const j = s.cases.c1.jobs.j1.value;
    expect(j.state).toBe('needs_retest');
    expect(j.attempt).toBe(2);
    expect(j.blocker).toBe('knowledge rolled back');
  });

  it('heartbeats update worker count and are not "meaningful" events', () => {
    const s = run([ev(1, 'job.log', { message: 'hello' }), ev(2, 'controller.heartbeat', { active: 2 }, null)]);
    expect(s.workersActive).toBe(2);
    expect(s.cases.c1.latestMeaningful?.seq).toBe(1);
  });
});

describe('derive: plan progress, unknown denominators and scope notes', () => {
  const plan = (progress: Plan['progress']): Plan => ({ revision: 1, items: [], unknown_scope: [], progress, eta: null });

  it('shows raw counts and no percentage when the total is unknown', () => {
    const views = phaseViews({ analysis: { done: 12, total: null, unit: 'files' } });
    expect(views[0]).toMatchObject({ label: 'Discovery', done: 12, total: null, percent: null, scopeKnown: false, reported: true });
    expect(views[1]).toMatchObject({ label: 'Recovery', reported: false, percent: null });
  });

  it('computes a percentage only from a real denominator', () => {
    expect(phaseViews({ recovery: { done: 4, total: 8 } })[1].percent).toBe(50);
    expect(progressPercent({ done: 4, total: 0 })).toBeNull();
    expect(progressPercent({ done: 4 })).toBeNull();
  });

  it('accepts "discovery" as an alias for analysis', () => {
    expect(phaseViews({ discovery: { done: 1, total: 2 } })[0].percent).toBe(50);
  });

  it('adds a note when the denominator changes', () => {
    let s = applySnapshot(emptyStudio(), 'c1', { kind: 'plan', data: plan({ recovery: { done: 4, total: 8 } }) }, 10);
    expect(s.cases.c1.scopeNotes).toEqual([]);
    s = applyEvent(s, ev(11, 'plan.revised', { revision: 2, reason: 'found 3 more functions', progress: { recovery: { done: 6, total: 11 } }, items: [] })).state;
    const notes = s.cases.c1.scopeNotes;
    expect(notes).toHaveLength(1);
    expect(notes[0]).toMatchObject({ phase: 'recovery', from: 8, to: 11, seq: 11 });
    expect(describeScopeNote(notes[0])).toMatch(/Recovery: scope changed from 8 to 11 items \(new work discovered\)\. Reason: found 3 more functions/);
  });

  it('notes when scope becomes known or unknown', () => {
    let s = applyEvent(emptyStudio(), ev(1, 'plan.revised', { revision: 1, progress: { analysis: { done: 3, total: null } }, items: [] })).state;
    s = applyEvent(s, ev(2, 'plan.revised', { revision: 2, progress: { analysis: { done: 52, total: 52 } }, items: [] })).state;
    s = applyEvent(s, ev(3, 'plan.revised', { revision: 3, progress: { analysis: { done: 52, total: null } }, items: [] })).state;
    expect(s.cases.c1.scopeNotes.map(describeScopeNote)).toEqual(['Discovery: scope became known — 52 items.', 'Discovery: scope became unknown again (was 52).']);
  });

  it('does not invent notes when only done changes', () => {
    let s = applyEvent(emptyStudio(), ev(1, 'plan.revised', { revision: 1, progress: { build: { done: 0, total: 1 } }, items: [] })).state;
    s = applyEvent(s, ev(2, 'plan.revised', { revision: 2, progress: { build: { done: 1, total: 1 } }, items: [] })).state;
    expect(s.cases.c1.scopeNotes).toEqual([]);
  });

  it('keeps ETA only when the API provides it', () => {
    let s = applyEvent(emptyStudio(), ev(1, 'plan.revised', { revision: 1, items: [] })).state;
    expect(s.cases.c1.eta).toBeNull();
    s = applyEvent(s, ev(2, 'plan.revised', { revision: 2, eta: { seconds: 60, uncertainty: 30, updated_at: 'x' }, items: [] })).state;
    expect(s.cases.c1.eta?.seconds).toBe(60);
    s = applyEvent(s, ev(3, 'plan.revised', { revision: 3, eta: null, items: [] })).state;
    expect(s.cases.c1.eta).toBeNull();
  });

  it('asks for a plan snapshot when plan.revised carries no items', () => {
    const r = applyEvent(emptyStudio(), ev(1, 'plan.revised', { revision: 2 }));
    expect(r.refresh).toContainEqual({ caseId: 'c1', key: 'plan' });
  });
});

describe('derive: snapshots, features, previews, feedback', () => {
  it('keeps event state newer than the snapshot request', () => {
    let s = applyEvent(emptyStudio(), ev(20, 'feature.updated', { feature: { feature_id: 'F1', title: 'List', verify_status: 'verified', impl_status: 'runnable', critical: true, verify_candidate: null, user_review: null } })).state;
    s = applySnapshot(s, 'c1', { kind: 'features', data: [{ feature_id: 'F1', title: 'List', verify_status: 'untested', impl_status: 'planned', critical: true, verify_candidate: null, user_review: null }] }, 10);
    expect(s.cases.c1.features.F1.value.verify_status).toBe('verified');
    s = applySnapshot(s, 'c1', { kind: 'features', data: [{ feature_id: 'F1', title: 'List', verify_status: 'failed', impl_status: 'runnable', critical: true, verify_candidate: null, user_review: null }] }, 30);
    expect(s.cases.c1.features.F1.value.verify_status).toBe('failed');
  });

  it('marks previews stale from preview.stale', () => {
    let s = applyEvent(emptyStudio(), ev(1, 'preview.published', { preview: { preview_id: 'p1', candidate_id: 'c', kind: 'real', stale: false } })).state;
    s = applyEvent(s, ev(2, 'preview.stale', { preview_id: 'p1', reason: 'superseded' })).state;
    expect(s.cases.c1.previews.p1.value).toMatchObject({ stale: true, stale_reason: 'superseded' });
  });

  it('upserts feedback from events', () => {
    let s = applyEvent(emptyStudio(), ev(1, 'feedback.created', { feedback: { feedback_id: 'f1', status: 'received' } })).state;
    s = applyEvent(s, ev(2, 'feedback.updated', { feedback: { feedback_id: 'f1', status: 'ready_to_retest' } })).state;
    expect(values(s.cases.c1.feedback).map((f) => f.status)).toEqual(['ready_to_retest']);
  });

  it('bumps the comparisons version so views refetch', () => {
    const r = applyEvent(emptyStudio(), ev(1, 'comparison.recorded', { comparison_id: 'x' }));
    expect(r.state.cases.c1.versions.comparisons).toBe(1);
  });

  it('seeds latest event and log from history without re-running reducers', () => {
    const s = applyHistory(emptyStudio(), 'c1', [ev(1, 'job.log', { message: 'a' }), ev(2, 'controller.heartbeat', {}, 'c1'), ev(3, 'job.started', { job_id: 'j' }), ev(4, 'job.log', {}, 'other')]);
    expect(s.cases.c1.latestMeaningful?.seq).toBe(3);
    expect(s.cases.c1.log.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(s.cases.c1.jobs).toEqual({});
  });
});

describe('idle keep-alive heartbeat (controller re-sends the last seq)', () => {
  it('is reported as liveness, not delivered and not counted as a duplicate', () => {
    let t = 1000;
    const sockets: { onmessage: ((e: { data: unknown }) => void) | null; onopen: (() => void) | null }[] = [];
    const c = new EventClient({
      url: () => 'ws://x',
      now: () => t,
      createSocket: () => {
        const s = { onopen: null, onmessage: null, onclose: null, onerror: null, close() {} };
        sockets.push(s as never);
        return s as never;
      },
    });
    const got: number[] = [];
    const beats: number[] = [];
    c.onEvent((e) => got.push(e.seq));
    c.onHeartbeat((e) => beats.push(e.seq));
    c.start();
    sockets[0].onopen?.();
    sockets[0].onmessage?.({ data: JSON.stringify({ seq: 1, ts: 'a', case_id: null, job_id: null, kind: 'job.log', payload: {} }) });
    t = 50_000;
    sockets[0].onmessage?.({ data: JSON.stringify({ seq: 1, ts: 'b', case_id: null, job_id: null, kind: 'controller.heartbeat', payload: { idle: true } }) });
    expect(got).toEqual([1]);
    expect(beats).toEqual([1]);
    expect(c.snapshot.duplicatesDropped).toBe(0);
    expect(c.snapshot.lastEventAt).toBe(50_000);
  });
});
