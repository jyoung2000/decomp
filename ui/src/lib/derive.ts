// Pure reducers that derive per-case state from controller events and REST snapshots.
// Every entity remembers the seq that last wrote it, so an older (out-of-order or replayed) event never overwrites
// newer state. Nothing here reads a clock: progress only moves when the controller reports it.
import type {
  Budget, Candidate, Case, ControllerEvent, Eta, Feature, Feedback, Job, JobState, Phase, Plan, PlanItem, PhaseProgress,
  Outcome, Preview, UnknownScopeEntry, AiActivity, LogEntry,
} from './types';
import { LIVE_LOG_LIMIT, logEntryFromEvent } from './log';

export interface Tracked<T> {
  value: T;
  seq: number;
}

export interface ScopeNote {
  phase: string;
  from: number | null;
  to: number | null;
  seq: number;
  at: string;
  reason?: string;
}

export type RefreshKey = 'case' | 'jobs' | 'plan' | 'features' | 'candidates' | 'previews' | 'feedback' | 'comparisons' | 'evidence' | 'budgets' | 'aiCalls' | 'knowledge' | 'connections' | 'modules';

export interface CaseState {
  caseId: string;
  case: Tracked<Case> | null;
  jobs: Record<string, Tracked<Job>>;
  planItems: Record<string, Tracked<PlanItem>>;
  planLoaded: boolean;
  planRevision: number | null;
  planRevisionReason: string | null;
  progress: Plan['progress'];
  progressSeq: number;
  eta: Eta | null;
  unknownScope: UnknownScopeEntry[];
  /** controller-derived outcome (pipeline vs verified behaviour); null until a case/plan snapshot carries it */
  outcome: Outcome | null;
  scopeNotes: ScopeNote[];
  features: Record<string, Tracked<Feature>>;
  candidates: Record<string, Tracked<Candidate>>;
  previews: Record<string, Tracked<Preview>>;
  feedback: Record<string, Tracked<Feedback>>;
  budget: Budget | null;
  /** live `ai.activity` lines (newest last), capped */
  aiActivity: AiActivity[];
  /** live log rows derived from job.log / job.* / ai.activity events (oldest first), capped */
  liveLog: LogEntry[];
  latestEvent: ControllerEvent | null;
  latestMeaningful: ControllerEvent | null;
  /** last N non-heartbeat events for this case (raw log view) */
  log: ControllerEvent[];
  /** bumped whenever comparisons/evidence change so views can refetch */
  versions: Partial<Record<RefreshKey, number>>;
}

export interface StudioState {
  cases: Record<string, CaseState>;
  workersActive: number | null;
  lastHeartbeat: ControllerEvent | null;
  /** bumped for global resources (knowledge, connections, budgets) */
  versions: Partial<Record<RefreshKey, number>>;
}

export const LOG_LIMIT = 500;
export const ACTIVITY_LIMIT = 500;

export function emptyStudio(): StudioState {
  return { cases: {}, workersActive: null, lastHeartbeat: null, versions: {} };
}

export function emptyCase(caseId: string): CaseState {
  return {
    caseId,
    case: null,
    jobs: {},
    planItems: {},
    planLoaded: false,
    planRevision: null,
    planRevisionReason: null,
    progress: {},
    progressSeq: -1,
    eta: null,
    unknownScope: [],
    outcome: null,
    scopeNotes: [],
    features: {},
    candidates: {},
    previews: {},
    feedback: {},
    budget: null,
    aiActivity: [],
    liveLog: [],
    latestEvent: null,
    latestMeaningful: null,
    log: [],
    versions: {},
  };
}

const NOT_MEANINGFUL = new Set(['controller.heartbeat', 'job.progress', 'budget.updated']);

export interface ApplyResult {
  state: StudioState;
  /** snapshots that should be refetched because the event did not carry enough data */
  refresh: { caseId: string | null; key: RefreshKey }[];
}

function obj(x: unknown): Record<string, unknown> | null {
  return x && typeof x === 'object' && !Array.isArray(x) ? (x as Record<string, unknown>) : null;
}

function upsert<T>(map: Record<string, Tracked<T>>, id: string, seq: number, f: (prev: T | undefined) => T): Record<string, Tracked<T>> {
  const prev = map[id];
  if (prev && prev.seq > seq) return map; // older event: ignore
  return { ...map, [id]: { value: f(prev?.value), seq } };
}

function bump(v: Partial<Record<RefreshKey, number>>, k: RefreshKey) {
  return { ...v, [k]: (v[k] ?? 0) + 1 };
}

/** Compares phase totals; any change of a known denominator produces a human-readable note. */
export function scopeChanges(prev: Plan['progress'], next: Plan['progress'], seq: number, at: string, reason?: string): ScopeNote[] {
  const notes: ScopeNote[] = [];
  const keys = new Set([...Object.keys(prev ?? {}), ...Object.keys(next ?? {})]);
  for (const k of keys) {
    const p = (prev as Record<string, PhaseProgress | undefined>)[k];
    const n = (next as Record<string, PhaseProgress | undefined>)[k];
    if (!p || !n) continue; // first observation is not a change
    const from = p.total ?? null;
    const to = n.total ?? null;
    if (from !== to) notes.push({ phase: k, from, to, seq, at, reason });
  }
  return notes;
}

export function describeScopeNote(n: ScopeNote): string {
  const label = phaseLabel(n.phase);
  let what: string;
  if (n.from == null && n.to != null) what = `${label}: scope became known — ${n.to} items.`;
  else if (n.from != null && n.to == null) what = `${label}: scope became unknown again (was ${n.from}).`;
  else what = `${label}: scope changed from ${n.from} to ${n.to} items${n.to! > n.from! ? ' (new work discovered)' : ' (work removed or merged)'}.`;
  return n.reason ? `${what} Reason: ${n.reason}` : what;
}

export function phaseLabel(p: string): string {
  switch (p) {
    case 'analysis':
    case 'discovery':
      return 'Discovery';
    case 'recovery':
      return 'Recovery';
    case 'implementation':
      return 'Implementation';
    case 'build':
      return 'Build';
    case 'verification':
      return 'Verification';
    default:
      return p;
  }
}

/**
 * docs/API.md: progress = {analysis:{done,total}, recovery, implementation, build, verification}.
 * The controller returns {jobs:{..}, groups:{discovery, recovery, implementation, build, verification}, features:{..}}.
 * Flatten the second shape into the first (groups.discovery → analysis); other keys are kept.
 */
export function normalizeProgress(progress: Plan['progress'] | undefined): Plan['progress'] | undefined {
  if (!progress || !obj(progress)) return progress;
  const groups = (progress as Record<string, unknown>).groups;
  if (!obj(groups)) return progress;
  const g = groups as Record<string, PhaseProgress>;
  const flat: Record<string, unknown> = { ...(progress as Record<string, unknown>) };
  for (const [k, v] of Object.entries(g)) if (obj(v)) flat[k === 'discovery' ? 'analysis' : k] = v;
  return flat as Plan['progress'];
}

function applyProgress(cs: CaseState, rawProgress: Plan['progress'] | undefined, eta: Eta | null | undefined, seq: number, at: string, reason?: string): CaseState {
  const progress = normalizeProgress(rawProgress);
  let next = cs;
  if (progress && obj(progress) && seq >= cs.progressSeq) {
    const notes = cs.progressSeq >= 0 ? scopeChanges(cs.progress, progress, seq, at, reason) : [];
    next = { ...next, progress, progressSeq: seq, scopeNotes: notes.length ? [...cs.scopeNotes, ...notes].slice(-50) : cs.scopeNotes };
  }
  if (eta !== undefined && seq >= cs.progressSeq) next = { ...next, eta: eta ?? null };
  return next;
}

const JOB_TERMINAL: Record<string, JobState> = {
  'job.completed': 'completed',
  'job.failed': 'failed',
  'job.cancelled': 'cancelled',
  'job.needs_retest': 'needs_retest',
  'job.blocked': 'blocked',
};

function stubJob(ev: ControllerEvent, id: string): Job {
  const p = ev.payload;
  return {
    job_id: id,
    case_id: ev.case_id ?? '',
    stage: String(p.stage ?? 'unknown'),
    title: String(p.title ?? p.stage ?? id),
    state: 'queued',
    attempt: 0,
    progress: {},
    blocker: null,
    heartbeat_at: null,
  };
}

function reduceJob(cs: CaseState, ev: ControllerEvent): { cs: CaseState; needJobs: boolean } {
  const p = ev.payload;
  if (ev.kind === 'job.created' && obj(p.job)) {
    const job = p.job as unknown as Job;
    return { cs: { ...cs, jobs: upsert(cs.jobs, job.job_id, ev.seq, () => ({ ...job, progress: job.progress ?? {} })) }, needJobs: false };
  }
  if (ev.kind === 'job.log') return { cs, needJobs: false };
  const id = String(p.job_id ?? ev.job_id ?? '');
  if (!id) return { cs, needJobs: false };
  const known = !!cs.jobs[id];
  const jobs = upsert(cs.jobs, id, ev.seq, (prev) => {
    const j: Job = { ...(prev ?? stubJob(ev, id)) };
    switch (ev.kind) {
      case 'job.started':
        j.state = 'running';
        if (typeof p.attempt === 'number') j.attempt = p.attempt;
        j.started_at = ev.ts;
        j.heartbeat_at = ev.ts;
        j.blocker = null;
        break;
      case 'job.progress':
        if (obj(p.progress)) j.progress = p.progress as Job['progress'];
        j.heartbeat_at = ev.ts;
        break;
      case 'job.retry':
        j.state = 'queued';
        if (typeof p.attempt === 'number') j.attempt = p.attempt;
        if (typeof p.error === 'string') j.error = p.error;
        break;
      case 'job.unblocked':
        j.state = 'queued';
        j.blocker = null;
        break;
      case 'job.resumed':
        j.state = (typeof p.state === 'string' ? p.state : 'queued') as JobState;
        j.blocker = null;
        break;
      case 'job.cancel_requested':
        (j as Job & { cancel_requested?: boolean }).cancel_requested = true;
        break;
      case 'job.lease_expired':
        (j as Job & { lease_expired?: boolean }).lease_expired = true;
        break;
      default: {
        const st = JOB_TERMINAL[ev.kind];
        if (st) {
          j.state = st;
          if (st === 'blocked') j.blocker = typeof p.blocker === 'string' ? p.blocker : j.blocker;
          if (st === 'needs_retest' && typeof p.reason === 'string') j.blocker = p.reason;
          if (st === 'failed' && typeof p.error === 'string') j.error = p.error;
          if (st === 'completed' || st === 'failed' || st === 'cancelled') j.finished_at = ev.ts;
        }
      }
    }
    return j;
  });
  return { cs: { ...cs, jobs }, needJobs: !known };
}

const str = (x: unknown) => (typeof x === 'string' && x ? x : undefined);
const num = (x: unknown) => (typeof x === 'number' && Number.isFinite(x) ? x : undefined);

/** Whitelist of `ai.activity` fields (docs/AI_LADDER.md section 5). Anything else, notably prompt text, is dropped. */
export function pickActivity(p: Record<string, unknown>, ev?: ControllerEvent): AiActivity | null {
  const text = str(p.text);
  if (!text) return null;
  const loc = p.locality === 'local' || p.locality === 'cloud' ? p.locality : undefined;
  return {
    at: str(p.at) ?? ev?.ts ?? '',
    kind: str(p.kind),
    text,
    plan_item_id: str(p.plan_item_id) ?? null,
    job_id: str(p.job_id) ?? ev?.job_id ?? null,
    candidate_id: str(p.candidate_id) ?? null,
    evidence_ids: Array.isArray(p.evidence_ids) ? p.evidence_ids.filter((x): x is string => typeof x === 'string') : [],
    task: str(p.task) ?? null,
    provider: str(p.provider) ?? null,
    model: str(p.model) ?? null,
    locality: loc ?? null,
    outcome: str(p.outcome) ?? null,
    tokens_in: num(p.tokens_in) ?? null,
    tokens_out: num(p.tokens_out) ?? null,
    cost_usd: num(p.cost_usd) ?? null,
    cost_known: typeof p.cost_known === 'boolean' ? p.cost_known : null,
    fallback_reason: str(p.fallback_reason) ?? null,
    config_revision: num(p.config_revision) ?? null,
    origin: p.origin === 'deterministic' || p.origin === 'model_proposed' || p.origin === 'verifier_decided' ? p.origin : null,
    seq: ev?.seq,
  };
}

export function applyEvent(state: StudioState, ev: ControllerEvent): ApplyResult {
  const refresh: ApplyResult['refresh'] = [];
  const p = ev.payload ?? {};
  let s = state;

  if (ev.kind === 'controller.heartbeat') {
    s = { ...s, lastHeartbeat: ev, workersActive: typeof p.active === 'number' ? p.active : s.workersActive };
    return { state: s, refresh };
  }
  if (ev.kind === 'knowledge.updated') {
    s = { ...s, versions: bump(s.versions, 'knowledge') };
    refresh.push({ caseId: null, key: 'knowledge' });
  }
  if (ev.kind === 'ai.call' || (ev.kind === 'budget.updated' && !ev.case_id)) {
    s = { ...s, versions: bump(bump(s.versions, 'budgets'), 'aiCalls') };
  }

  const caseId = ev.case_id ?? (obj(p.case) ? String((p.case as Record<string, unknown>).case_id) : null);
  if (!caseId) return { state: s, refresh };

  let cs = s.cases[caseId] ?? emptyCase(caseId);
  cs = {
    ...cs,
    latestEvent: ev,
    latestMeaningful: NOT_MEANINGFUL.has(ev.kind) ? cs.latestMeaningful : ev,
    log: ev.kind === 'job.progress' ? cs.log : [...cs.log, ev].slice(-LOG_LIMIT),
  };

  const le = logEntryFromEvent(ev, (id) => cs.jobs[id]?.value);
  if (le && !cs.liveLog.some((x) => x.seq === le.seq)) cs = { ...cs, liveLog: [...cs.liveLog, le].slice(-LIVE_LOG_LIMIT) };

  const k = ev.kind;
  if (k === 'case.created' && obj(p.case)) {
    if (!cs.case || cs.case.seq <= ev.seq) cs = { ...cs, case: { value: p.case as unknown as Case, seq: ev.seq } };
  } else if (k === 'case.status') {
    if (cs.case) {
      if (cs.case.seq <= ev.seq) cs = { ...cs, case: { value: { ...cs.case.value, status: String(p.status) }, seq: ev.seq } };
    } else refresh.push({ caseId, key: 'case' });
    refresh.push({ caseId, key: 'plan' }); // the derived outcome (delivered vs verified) changes with the status
  } else if (k.startsWith('job.')) {
    const r = reduceJob(cs, ev);
    cs = r.cs;
    if (r.needJobs) refresh.push({ caseId, key: 'jobs' });
    // job transitions usually move plan items/phase counters; refresh the plan snapshot (never on job.progress)
    if (['job.started', 'job.completed', 'job.failed', 'job.cancelled', 'job.blocked', 'job.needs_retest'].includes(k)) refresh.push({ caseId, key: 'plan' });
  } else if (k === 'plan.revised') {
    const rev = typeof p.revision === 'number' ? p.revision : cs.planRevision;
    const reason = typeof p.reason === 'string' ? p.reason : null;
    cs = { ...cs, planRevision: rev, planRevisionReason: reason ?? cs.planRevisionReason };
    cs = applyProgress(cs, obj(p.progress) ? (p.progress as Plan['progress']) : undefined, p.eta === undefined ? undefined : (p.eta as Eta | null), ev.seq, ev.ts, reason ?? undefined);
    if (Array.isArray(p.unknown_scope)) cs = { ...cs, unknownScope: p.unknown_scope as UnknownScopeEntry[] };
    if (Array.isArray(p.items)) {
      let items = cs.planItems;
      for (const it of p.items as PlanItem[]) items = upsert(items, it.item_id, ev.seq, () => it);
      cs = { ...cs, planItems: items };
    } else refresh.push({ caseId, key: 'plan' });
  } else if (k === 'plan.item') {
    const it = obj(p.item) as PlanItem | null;
    if (it && it.item_id) cs = { ...cs, planItems: upsert(cs.planItems, it.item_id, ev.seq, (prev) => ({ ...(prev ?? {}), ...it }) as PlanItem) };
    else refresh.push({ caseId, key: 'plan' });
    if (obj(p.progress)) cs = applyProgress(cs, p.progress as Plan['progress'], p.eta === undefined ? undefined : (p.eta as Eta | null), ev.seq, ev.ts);
  } else if (k === 'feature.updated') {
    const f = obj(p.feature) as Feature | null;
    if (f && f.feature_id) cs = { ...cs, features: upsert(cs.features, f.feature_id, ev.seq, (prev) => ({ ...(prev ?? {}), ...f }) as Feature) };
    else refresh.push({ caseId, key: 'features' });
  } else if (k.startsWith('candidate.')) {
    const c = obj(p.candidate) as Candidate | null;
    const id = c?.candidate_id ?? (typeof p.candidate_id === 'string' ? p.candidate_id : null);
    if (c && c.candidate_id) cs = { ...cs, candidates: upsert(cs.candidates, c.candidate_id, ev.seq, (prev) => ({ ...(prev ?? {}), ...c }) as Candidate) };
    else if (id && cs.candidates[id]) {
      const status = k === 'candidate.built' ? 'built' : k === 'candidate.failed' ? 'failed' : undefined;
      cs = { ...cs, candidates: upsert(cs.candidates, id, ev.seq, (prev) => ({ ...(prev as Candidate), ...(status ? { build_status: status } : {}) })) };
    } else refresh.push({ caseId, key: 'candidates' });
  } else if (k === 'comparison.recorded') {
    cs = { ...cs, versions: bump(cs.versions, 'comparisons') };
    refresh.push({ caseId, key: 'comparisons' });
  } else if (k === 'evidence.added' || k === 'evidence.invalidated') {
    cs = { ...cs, versions: bump(cs.versions, 'evidence') };
  } else if (k === 'preview.published') {
    const pv = obj(p.preview) as Preview | null;
    if (pv && pv.preview_id) cs = { ...cs, previews: upsert(cs.previews, pv.preview_id, ev.seq, () => pv) };
    else refresh.push({ caseId, key: 'previews' });
  } else if (k === 'preview.stale') {
    const id = String(p.preview_id ?? '');
    if (id && cs.previews[id]) {
      cs = {
        ...cs,
        previews: upsert(cs.previews, id, ev.seq, (prev) => ({ ...(prev as Preview), stale: true, stale_reason: typeof p.reason === 'string' ? p.reason : (prev as Preview).stale_reason ?? null })),
      };
    } else refresh.push({ caseId, key: 'previews' });
  } else if (k === 'feedback.created' || k === 'feedback.updated') {
    const fb = obj(p.feedback) as Feedback | null;
    if (fb && fb.feedback_id) cs = { ...cs, feedback: upsert(cs.feedback, fb.feedback_id, ev.seq, () => fb) };
    else refresh.push({ caseId, key: 'feedback' });
  } else if (k === 'budget.updated') {
    const b = obj(p.budget) as Budget | null;
    if (b) cs = { ...cs, budget: b };
    s = { ...s, versions: bump(s.versions, 'budgets') };
  } else if (k === 'ai.call') {
    cs = { ...cs, versions: bump(cs.versions, 'aiCalls') };
  } else if (k === 'ai.activity') {
    // only the documented, whitelisted fields are kept; a raw prompt can never reach the UI state
    const a = pickActivity(p, ev);
    if (a && !cs.aiActivity.some((x) => x.seq === ev.seq)) cs = { ...cs, aiActivity: [...cs.aiActivity, a].slice(-ACTIVITY_LIMIT) };
  } else if (k === 'verification.completed' || k === 'verification.invalidated') {
    // not in docs/API.md; emitted by the controller's verifier — verdicts live on candidates, features and previews
    cs = { ...cs, versions: bump(cs.versions, 'comparisons') };
    refresh.push({ caseId, key: 'candidates' }, { caseId, key: 'features' }, { caseId, key: 'previews' }, { caseId, key: 'plan' });
  } else if (k === 'feature.stale') {
    refresh.push({ caseId, key: 'features' });
  }

  s = { ...s, cases: { ...s.cases, [caseId]: cs } };
  return { state: s, refresh };
}

// ---- snapshots -----------------------------------------------------------------------------------------------------

export type Snapshot =
  | { kind: 'case'; data: Case }
  | { kind: 'jobs'; data: Job[] }
  | { kind: 'plan'; data: Plan }
  | { kind: 'features'; data: Feature[] }
  | { kind: 'candidates'; data: Candidate[] }
  | { kind: 'previews'; data: Preview[] }
  | { kind: 'feedback'; data: Feedback[] };

function mergeList<T>(map: Record<string, Tracked<T>>, rows: T[], idOf: (t: T) => string, seq: number): Record<string, Tracked<T>> {
  const out: Record<string, Tracked<T>> = {};
  for (const r of rows) {
    const id = idOf(r);
    const prev = map[id];
    // keep event-derived state that is newer than the snapshot request
    out[id] = prev && prev.seq > seq ? prev : { value: r, seq };
  }
  // keep rows created by events after the snapshot was requested
  for (const [id, t] of Object.entries(map)) if (!(id in out) && t.seq > seq) out[id] = t;
  return out;
}

/** Merge a REST snapshot that was requested when the client had delivered events up to `asOfSeq`. */
export function applySnapshot(state: StudioState, caseId: string, snap: Snapshot, asOfSeq: number, at = new Date(0).toISOString()): StudioState {
  let cs = state.cases[caseId] ?? emptyCase(caseId);
  switch (snap.kind) {
    case 'case':
      if (snap.data.outcome) cs = { ...cs, outcome: snap.data.outcome };
      if (!cs.case || cs.case.seq <= asOfSeq) cs = { ...cs, case: { value: snap.data, seq: asOfSeq } };
      else cs = { ...cs, case: { value: { ...snap.data, status: cs.case.value.status }, seq: cs.case.seq } };
      break;
    case 'jobs':
      cs = { ...cs, jobs: mergeList(cs.jobs, snap.data, (j) => j.job_id, asOfSeq) };
      break;
    case 'plan': {
      const d = snap.data;
      cs = {
        ...cs,
        planLoaded: true,
        planItems: mergeList(cs.planItems, d.items ?? [], (i) => i.item_id, asOfSeq),
        planRevision: cs.planRevision != null && cs.planRevision > d.revision ? cs.planRevision : d.revision,
        unknownScope: d.unknown_scope ?? [],
        outcome: d.outcome ?? cs.outcome,
      };
      cs = applyProgress(cs, d.progress ?? {}, d.eta ?? null, Math.max(asOfSeq, cs.progressSeq), at);
      break;
    }
    case 'features':
      cs = { ...cs, features: mergeList(cs.features, snap.data, (f) => f.feature_id, asOfSeq) };
      break;
    case 'candidates':
      cs = { ...cs, candidates: mergeList(cs.candidates, snap.data, (c) => c.candidate_id, asOfSeq) };
      break;
    case 'previews':
      cs = { ...cs, previews: mergeList(cs.previews, snap.data, (p) => p.preview_id, asOfSeq) };
      break;
    case 'feedback':
      cs = { ...cs, feedback: mergeList(cs.feedback, snap.data, (f) => f.feedback_id, asOfSeq) };
      break;
  }
  return { ...state, cases: { ...state.cases, [caseId]: cs } };
}

/** Seed the per-case log / latest-event fields from past events (REST /events) without re-running reducers. */
export function applyHistory(state: StudioState, caseId: string, events: ControllerEvent[]): StudioState {
  const mine = events.filter((e) => e.case_id === caseId);
  if (!mine.length) return state;
  const cs = state.cases[caseId] ?? emptyCase(caseId);
  const bySeq = new Map<number, ControllerEvent>();
  for (const e of [...mine.filter((e) => e.kind !== 'job.progress'), ...cs.log]) bySeq.set(e.seq, e);
  const log = [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-LOG_LIMIT);
  const lastAll = mine.reduce((a, e) => (e.seq > a.seq ? e : a));
  const meaningful = mine.filter((e) => !NOT_MEANINGFUL.has(e.kind));
  const lastMeaningful = meaningful.length ? meaningful.reduce((a, e) => (e.seq > a.seq ? e : a)) : null;
  const next: CaseState = {
    ...cs,
    log,
    latestEvent: !cs.latestEvent || lastAll.seq > cs.latestEvent.seq ? lastAll : cs.latestEvent,
    latestMeaningful: lastMeaningful && (!cs.latestMeaningful || lastMeaningful.seq > cs.latestMeaningful.seq) ? lastMeaningful : cs.latestMeaningful,
  };
  return { ...state, cases: { ...state.cases, [caseId]: next } };
}

// ---- selectors -----------------------------------------------------------------------------------------------------

export function values<T>(m: Record<string, Tracked<T>>): T[] {
  return Object.values(m).map((t) => t.value);
}

export interface PhaseView {
  phase: Phase | 'discovery';
  label: string;
  done: number | null;
  total: number | null;
  unit?: string;
  /** null whenever the denominator is unknown — never synthesized */
  percent: number | null;
  scopeKnown: boolean;
  reported: boolean;
}

export function phaseViews(progress: Plan['progress']): PhaseView[] {
  const order: (Phase | 'discovery')[] = ['analysis', 'recovery', 'implementation', 'build', 'verification'];
  return order.map((ph) => {
    const raw = (progress as Record<string, PhaseProgress | undefined>)[ph] ?? (ph === 'analysis' ? (progress as Record<string, PhaseProgress | undefined>).discovery : undefined);
    return toPhaseView(ph, raw);
  });
}

export function toPhaseView(phase: Phase | 'discovery', raw: PhaseProgress | undefined): PhaseView {
  const done = raw && typeof raw.done === 'number' ? raw.done : null;
  const total = raw && typeof raw.total === 'number' ? raw.total : null;
  // an empty denominator (0 of 0) is not "100% done": nothing was measured, so there is no percentage
  const percent = done != null && total != null && total > 0 ? Math.max(0, Math.min(100, (done / total) * 100)) : null;
  return { phase, label: phaseLabel(phase), done, total, unit: raw?.unit, percent, scopeKnown: total != null, reported: !!raw };
}

/** Percent for a job's raw progress counts, or null when the total is unknown. */
export function progressPercent(p: { done?: number | null; total?: number | null } | null | undefined): number | null {
  if (!p || typeof p.done !== 'number' || typeof p.total !== 'number' || p.total <= 0) return null;
  return Math.max(0, Math.min(100, (p.done / p.total) * 100));
}

export function describeEvent(ev: ControllerEvent): string {
  const p = ev.payload ?? {};
  const job = typeof p.job_id === 'string' ? p.job_id : ev.job_id;
  switch (ev.kind) {
    case 'case.created':
      return 'Project created';
    case 'case.status':
      return `Project status → ${String(p.status)}`;
    case 'job.created':
      return `Job queued: ${String((p.job as Record<string, unknown> | undefined)?.title ?? job)}`;
    case 'job.started':
      return `Job started: ${String(p.title ?? p.stage ?? job)} (attempt ${String(p.attempt ?? '?')})`;
    case 'job.completed':
      return `Job completed: ${String(p.title ?? job)}`;
    case 'job.failed':
      return `Job failed: ${String(p.title ?? job)}${p.error ? ` — ${String(p.error).slice(0, 160)}` : ''}`;
    case 'job.blocked':
      return `Job blocked: ${String(p.blocker ?? job)}`;
    case 'job.log':
      return String(p.message ?? '').slice(0, 200);
    case 'job.progress':
      return `Progress on ${String(p.stage ?? job)}`;
    case 'plan.revised':
      return `Plan revised to r${String(p.revision ?? '?')}${p.reason ? `: ${String(p.reason)}` : ''}`;
    case 'plan.item': {
      const it = p.item as Record<string, unknown> | undefined;
      return it ? `${String(it.item_id)} ${String(it.title ?? '')} → ${String(it.status ?? '')}` : 'Plan item updated';
    }
    case 'feature.updated':
      return `Feature updated: ${String((p.feature as Record<string, unknown> | undefined)?.title ?? '')}`;
    case 'candidate.created':
      return 'New build candidate created';
    case 'candidate.built':
      return 'Candidate built';
    case 'candidate.failed':
      return 'Candidate build failed';
    case 'comparison.recorded':
      return `Comparison recorded${p.verdict ? `: ${String(p.verdict)}` : ''}`;
    case 'preview.published':
      return `Preview published: ${String((p.preview as Record<string, unknown> | undefined)?.title ?? '')}`;
    case 'preview.stale':
      return `Preview marked stale${p.reason ? `: ${String(p.reason)}` : ''}`;
    case 'feedback.created':
      return 'Feedback received';
    case 'feedback.updated':
      return `Feedback → ${String((p.feedback as Record<string, unknown> | undefined)?.status ?? 'updated')}`;
    case 'evidence.added':
      return `Evidence added: ${String(p.title ?? p.kind ?? '')}`;
    case 'evidence.invalidated':
      return `Evidence invalidated${p.reason ? `: ${String(p.reason)}` : ''}`;
    case 'ai.call':
      return `AI call (${String(p.task ?? '')})`;
    case 'preview.opened':
      return `Preview opened${p.url ? ` at ${String(p.url)}` : ''}`;
    case 'preview.stopped':
      return 'Preview stopped';
    case 'verification.completed': {
      const sm = (p.summary ?? {}) as Record<string, unknown>;
      return typeof sm.scenarios === 'number' ? `Verification finished: ${String(sm.passed ?? 0)} passed, ${String(sm.failed ?? 0)} failed, ${String(sm.errors ?? 0)} errors of ${sm.scenarios} scenarios` : 'Verification finished';
    }
    case 'verification.invalidated':
      return `Verification invalidated${p.reason ? `: ${String(p.reason)}` : ''}`;
    case 'feature.stale':
      return `${String(p.count ?? '')} feature result(s) marked stale${p.reason ? `: ${String(p.reason)}` : ''}`.trim();
    default:
      return ev.kind;
  }
}

/**
 * Raw job counts in the documented form {done, total, unit}. The controller reports free-form measured fields instead,
 * e.g. {scenarios_done, scenarios_total}, {functions_decompiled, functions_total}, {files_scanned, modules}. Pairs a
 * `<unit>_total` with `<unit>_done` (or the only other `<unit>_*` count); otherwise the first count has no denominator.
 * Never invents a total.
 */
export function jobCounts(p: Record<string, unknown> | null | undefined): { done: number | null; total: number | null; unit?: string; current?: string } {
  if (!p || typeof p !== 'object') return { done: null, total: null };
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const current = typeof p.current === 'string' ? p.current : typeof p.module === 'string' ? p.module : typeof p.target === 'string' ? p.target : undefined;
  if (num(p.done) != null) return { done: num(p.done), total: num(p.total), unit: typeof p.unit === 'string' ? p.unit : undefined, current };
  const keys = Object.keys(p).filter((k) => num(p[k]) != null);
  for (const k of keys) {
    const m = k.match(/^(.+)_total$/);
    if (!m) continue;
    const unit = m[1];
    const doneKey = keys.find((x) => x === `${unit}_done`) ?? keys.filter((x) => x !== k && x.startsWith(`${unit}_`) && !/error|failed/.test(x))[0];
    if (doneKey) return { done: num(p[doneKey]), total: num(p[k]), unit: unit.replace(/_/g, ' '), current };
  }
  const first = keys.find((k) => !/_total$/.test(k));
  if (first) {
    const unit = first.replace(/_(scanned|done|processed|count)$/, '').replace(/_/g, ' ');
    return { done: num(p[first]), total: null, unit, current };
  }
  return { done: null, total: null, current };
}
