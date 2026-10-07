// Live log helpers: turn controller events into plain-English LogEntry rows (mirrors controller/rebuild_controller/livelog.py),
// merge REST + live rows by seq, filter, and render as text for Copy / Save. Everything that reaches the screen goes through
// `scrub`, so a key or Authorization header that somehow slipped into an event is still never displayed.
import type { ControllerEvent, Job, LogEntry, LogLevel } from './types';

export const LIVE_LOG_LIMIT = 1000;

const SCRUBBERS: [RegExp, string][] = [
  [/sk-[A-Za-z0-9_-]{16,}/g, '[REDACTED]'],
  [/AIza[0-9A-Za-z_-]{20,}/g, '[REDACTED]'],
  [/\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, 'Bearer [REDACTED]'],
  [/(authorization["']?\s*[:=]\s*["']?)(?:basic|digest|token|negotiate|ntlm)\s+[^\s"',;}]{6,}/gi, '$1[REDACTED]'],
  [/((?:x-api-key|x-goog-api-key|api[_-]?key|authorization)["']?\s*[:=]\s*["']?)(?!\[REDACTED\])[^\s"',;}]{6,}/gi, '$1[REDACTED]'],
  [/([?&](?:key|api_key|apikey|access_token)=)[^&\s"']+/gi, '$1[REDACTED]'],
];

export function scrub(text: string): string {
  let s = text;
  for (const [re, to] of SCRUBBERS) s = s.replace(re, to);
  return s;
}

const str = (x: unknown): string | undefined => (typeof x === 'string' && x ? x : undefined);
const level = (x: unknown): LogLevel => (x === 'warn' || x === 'error' ? x : 'info');
const firstLine = (x: unknown, n = 300) => (String(x ?? '').split('\n').find((l) => l.trim()) ?? '').trim().slice(0, n);

type JobInfo = Pick<Job, 'stage' | 'title' | 'milestone_id'> | undefined;

/** One controller event -> a log row, or null when the event is not part of the live log. Only whitelisted fields are read (never prompts). */
export function logEntryFromEvent(ev: ControllerEvent, jobOf: (id: string) => JobInfo = () => undefined): LogEntry | null {
  const p = ev.payload ?? {};
  const jid = str(p.job_id) ?? ev.job_id ?? null;
  const info = jid ? jobOf(jid) : undefined;
  const stage = str(p.stage) ?? info?.stage ?? null;
  const base = { seq: ev.seq, at: str(p.at) ?? ev.ts, job_id: jid, stage, milestone: str(p.milestone) ?? info?.milestone_id ?? null, plan_item_id: str(p.plan_item_id) ?? null };
  const title = str(p.title) ?? info?.title ?? stage ?? 'job';
  switch (ev.kind) {
    case 'job.log': {
      const text = str(p.text) ?? str(p.message);
      return text ? { ...base, kind: 'log', level: level(p.level), text: text.slice(0, 600), detail: str(p.detail)?.slice(0, 2000) ?? null } : null;
    }
    case 'ai.activity': {
      const text = str(p.text);
      if (!text) return null;
      const bad = ['failed', 'error', 'refused', 'timeout'].includes(String(p.outcome ?? ''));
      return { ...base, kind: 'ai', level: bad ? 'warn' : 'info', text: text.slice(0, 600), provider: str(p.provider) ?? null, model: str(p.model) ?? null, outcome: str(p.outcome) ?? null };
    }
    case 'job.started': {
      const att = typeof p.attempt === 'number' ? p.attempt : 1;
      return { ...base, kind: 'job', level: 'info', text: `Started: ${title}${att > 1 ? ` (attempt ${att})` : ''}` };
    }
    case 'job.completed':
      return { ...base, kind: 'job', level: 'info', text: `Finished: ${title}` };
    case 'job.failed': {
      const err = str(p.error);
      return { ...base, kind: 'job', level: 'error', text: `Failed: ${title}${err ? ` — ${firstLine(err)}` : ''}`, detail: err ? err.split('\n').filter((l) => l.trim()).slice(-5).join('\n').slice(-1500) : null };
    }
    case 'job.cancelled':
      return { ...base, kind: 'job', level: 'info', text: `Cancelled: ${title}` };
    case 'job.blocked':
      return { ...base, kind: 'job', level: 'warn', text: `Waiting: ${title} — ${firstLine(p.blocker ?? p.error ?? 'blocked')}` };
    case 'job.retry':
      return { ...base, kind: 'job', level: 'warn', text: `Retrying ${title} (attempt ${String(p.attempt ?? '?')} did not finish): ${firstLine(p.error, 200)}` };
    default:
      return null;
  }
}

/** Union by seq (live rows win), oldest first. */
export function mergeLog(...lists: (LogEntry[] | undefined)[]): LogEntry[] {
  const m = new Map<number, LogEntry>();
  for (const l of lists) for (const e of l ?? []) m.set(e.seq, e);
  return [...m.values()].sort((a, b) => a.seq - b.seq);
}

export type LogFilter = { kind: 'all' | 'problems' | 'ai' | 'stage'; stage?: string };

export function filterLog(entries: LogEntry[], f: LogFilter): LogEntry[] {
  switch (f.kind) {
    case 'problems':
      return entries.filter((e) => e.level !== 'info');
    case 'ai':
      return entries.filter((e) => e.kind === 'ai');
    case 'stage':
      return entries.filter((e) => e.stage === f.stage);
    default:
      return entries;
  }
}

export const STAGE_LABEL: Record<string, string> = {
  inventory: 'Scan', dependency_graph: 'Dependencies', analyze_module: 'Analyse', recover_managed: 'Recover', recover_engine: 'Recover', recover_web: 'Recover',
  recover_jvm: 'Recover', discover_features: 'Features', capture_original: 'Original', reconstruct: 'Reconstruct', implement_loop: 'AI build', build_candidate: 'Build',
  compare_candidate: 'Compare', repair: 'Repair', deliver: 'Deliver', barrier: 'Summary',
};
export const stageLabel = (s?: string | null) => (s ? STAGE_LABEL[s] ?? s.replace(/_/g, ' ') : 'General');

export const LEVEL_ICON: Record<LogLevel, { glyph: string; label: string }> = {
  info: { glyph: 'ℹ', label: 'Info' },
  warn: { glyph: '⚠', label: 'Warning' },
  error: { glyph: '✖', label: 'Error' },
};

function pad(n: number) {
  return String(n).padStart(2, '0');
}

/** HH:MM:SS in local time (deterministic format for saved logs). */
export function stamp(at: string): string {
  const t = Date.parse(at);
  if (Number.isNaN(t)) return '--:--:--';
  const d = new Date(t);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function entryText(e: LogEntry): string {
  const head = `[${stamp(e.at)}] ${e.level === 'info' ? 'INFO ' : e.level === 'warn' ? 'WARN ' : 'ERROR'} ${e.kind === 'ai' ? 'AI' : stageLabel(e.stage)}: ${e.text}`;
  const detail = e.detail ? '\n' + e.detail.split('\n').map((l) => `    ${l}`).join('\n') : '';
  return scrub(head + detail);
}

export const logToText = (entries: LogEntry[]) => entries.map(entryText).join('\n') + (entries.length ? '\n' : '');
