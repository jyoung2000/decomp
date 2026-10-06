import { StudioStore } from './lib/store';
import type { WebSocketLike } from './lib/events';
import type { ControllerEvent } from './lib/types';

export class TestSocket implements WebSocketLike {
  onopen: WebSocketLike['onopen'] = null;
  onmessage: WebSocketLike['onmessage'] = null;
  onclose: WebSocketLike['onclose'] = null;
  onerror: WebSocketLike['onerror'] = null;
  constructor(public url: string) {}
  emit(ev: ControllerEvent) {
    this.onmessage?.({ data: JSON.stringify(ev) });
  }
  close() {}
}

export type Routes = Record<string, unknown | ((body: unknown, method: string) => unknown)>;

/** A store whose fetch answers from a route table (path without query → JSON) and whose sockets are captured. */
export function makeStore(routes: Routes, opts: { autoOpen?: boolean } = {}) {
  const sockets: TestSocket[] = [];
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://test');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname + url.search, body });
    const key = `${method} ${url.pathname}`;
    const hit = key in routes ? routes[key] : routes[url.pathname];
    if (hit === undefined) return new Response(JSON.stringify({ error: { code: 'not_found', message: `${key} not mocked` } }), { status: 404 });
    const data = typeof hit === 'function' ? (hit as (b: unknown, m: string) => unknown)(body, method) : hit;
    if (data instanceof Response) return data;
    return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  const store = new StudioStore(
    { baseUrl: '', token: 't', mock: false, source: 'url' },
    {
      fetchImpl,
      createSocket: (u) => {
        const s = new TestSocket(u);
        sockets.push(s);
        if (opts.autoOpen !== false) queueMicrotask(() => s.onopen?.());
        return s;
      },
    },
  );
  return { store, sockets, calls };
}

export const health = (hb = 30, latest = 0) => ({ ok: true, version: '9.9.9-test', pid: 1, heartbeat_seconds: hb, latest_seq: latest, started_at: '2026-01-01T00:00:00Z' });

export const demoCase = {
  case_id: 'c1',
  name: 'Test project',
  source_root: 'C:\\src',
  output_root: 'D:\\out',
  target_language: 'rust',
  output_type: 'exe',
  ai_policy: { mode: 'no_ai' },
  launch_profile: { execute_original: false },
  status: 'running',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

export function caseRoutes(extra: Routes = {}): Routes {
  return {
    '/health': health(),
    '/cases': [demoCase],
    '/cases/c1': demoCase,
    '/cases/c1/jobs': [],
    '/cases/c1/plan': { revision: 3, items: [], unknown_scope: ['plugins'], progress: { analysis: { done: 12, total: null, unit: 'files' }, recovery: { done: 2, total: 8, unit: 'functions' } }, eta: null },
    '/cases/c1/plan/revisions': [],
    '/cases/c1/features': [],
    '/cases/c1/candidates': [],
    '/cases/c1/previews': [],
    '/cases/c1/feedback': [],
    '/cases/c1/comparisons': [],
    '/events': [],
    '/budgets': [],
    '/ai/calls': [],
    '/capabilities': { output_combinations: [{ target_language: 'auto', output_type: 'exe', state: 'unsupported', reason: 'decide target first' }] },
    ...extra,
  };
}
