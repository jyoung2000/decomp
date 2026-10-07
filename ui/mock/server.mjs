#!/usr/bin/env node
// Rebuild Studio — MOCK controller for UI development and Playwright tests.
// Implements enough of docs/API.md (REST + WebSocket events) with synthetic, clearly-labelled demo data and a scripted
// scenario: plan → running → preview → feedback → stale/disconnect. State lives in memory for the life of the process.
//
// Usage: node mock/server.mjs [--port 8799] [--token mock-token] [--heartbeat 5] [--serve dist] [--auto 4000] [--preload N]
//   --auto <ms>  advance the scenario automatically every <ms> after Start is pressed (0 = manual via /__mock/step)
//   --serve dir  also serve the built UI (index.html etc.) from dir
// Control endpoints (no auth, loopback only): POST /__mock/step, /__mock/stall {seconds}, /__mock/disconnect {seconds},
//   /__mock/emit {kind,payload}, /__mock/burst {events,order}, /__mock/fail {method,path,status,body,times},
//   /__mock/reset {preload}; GET /__mock/state
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { WebSocketServer } from 'ws';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => {
    if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1] && !arr[i + 1].startsWith('--') ? arr[i + 1] : 'true']);
    return acc;
  }, []),
);
const PORT = Number(args.port ?? process.env.MOCK_PORT ?? 8799);
const TOKEN = String(args.token ?? process.env.MOCK_TOKEN ?? 'mock-token');
const HEARTBEAT = Number(args.heartbeat ?? 5);
const SERVE = args.serve ? path.resolve(String(args.serve)) : null;
const AUTO_MS = Number(args.auto ?? 0);
const PRELOAD = Number(args.preload ?? 0);
const STARTED = new Date().toISOString();

const now = () => new Date().toISOString();
const id = (p) => `${p}_${crypto.randomBytes(5).toString('hex')}`;
const sha = (s) => crypto.createHash('sha256').update(String(s)).digest('hex');

// ------------------------------------------------------------------------------------------------ state
let S;
function freshState() {
  return {
    seq: 0,
    events: [],
    cases: new Map(),
    jobs: new Map(),
    plans: new Map(), // case_id -> {revision, items: Map, unknown_scope, progress, eta, revisions: []}
    features: new Map(),
    candidates: new Map(),
    previews: new Map(),
    comparisons: [],
    feedback: new Map(),
    modules: new Map(),
    evidence: new Map(),
    connections: new Map(),
    routes: new Map(),
    ladderRev: 7,
    ladder: {},
    activity: new Map(),
    budgets: new Map(),
    aiCalls: [],
    knowledge: new Map(),
    settings: null,
    hermes: null,
    step: 0,
    stalledUntil: 0,
    faults: [],
    disconnectedUntil: 0,
    instances: new Map(),
    autoTimer: null,
  };
}

const sockets = new Set();

function emit(kind, payload = {}, case_id = null, job_id = null) {
  const ev = { seq: ++S.seq, ts: now(), case_id, job_id, kind, payload };
  S.events.push(ev);
  if (S.events.length > 20000) S.events.splice(0, 5000);
  const msg = JSON.stringify(ev);
  for (const ws of sockets) if (ws.readyState === 1) ws.send(msg);
  return ev;
}

// ------------------------------------------------------------------------------------------------ AI ladder (docs/AI_LADDER.md)
const MODELS = [
  { connection_id: 'conn_local', model: 'qwen2.5-coder:14b', capabilities: { vision: false, tools: true, context_window: 32768 }, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true },
  { connection_id: 'conn_local', model: 'deepseek-coder-v2:16b', capabilities: { vision: false, tools: false, context_window: 16384 }, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true },
  { connection_id: 'conn_local', model: 'llava:13b', capabilities: { vision: true, tools: false, context_window: 4096 }, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true },
  { connection_id: 'conn_openai', model: 'gpt-demo-large', capabilities: { vision: true, tools: true, context_window: 128000 }, price: { known: true, input_per_mtok: 2.5, output_per_mtok: 10, source: 'provider price list (demo)' }, free: false },
  { connection_id: 'conn_openai', model: 'gpt-demo-small', capabilities: { vision: true, tools: true, context_window: 128000 }, price: { known: true, input_per_mtok: 0.15, output_per_mtok: 0.6, source: 'provider price list (demo)' }, free: false },
  { connection_id: 'conn_handoff', model: 'claude-demo', capabilities: { vision: true, tools: true, context_window: 200000 }, price: { known: false }, free: false },
];
const AI_TASK_IDS = ['interpretation', 'repair', 'visual_review', 'verification_assist', 'knowledge'];
function catalogEntry(m, position) {
  const c = S.connections.get(m.connection_id);
  const local = c?.provider === 'local' || c?.provider === 'local_openai';
  return {
    ...(position ? { position } : {}), connection_id: m.connection_id, connection_label: c?.label ?? m.connection_id, provider: c?.provider ?? 'unknown', model: m.model,
    locality: local ? 'local' : 'cloud',
    availability: { state: c?.state === 'ok' || c?.state === 'usable' ? 'available' : c?.state ?? 'unprobed', probed_at: c?.last_probe ?? null, detail: null },
    capabilities: m.capabilities ?? null, price: m.price ?? { known: false }, free: !!m.free,
  };
}
function findModel(connection_id, model) {
  return MODELS.find((m) => m.connection_id === connection_id && m.model === model) ?? { connection_id, model, capabilities: null, price: { known: false }, free: S.connections.get(connection_id)?.provider === 'local_openai' };
}
function ladderEntries(pairs) {
  return pairs.map((p, i) => catalogEntry(findModel(p.connection_id, p.model), i + 1));
}
function currentLadder() {
  return { config_revision: S.ladderRev, tasks: Object.fromEntries(AI_TASK_IDS.map((t) => [t, { entries: ladderEntries(S.ladder[t]?.entries ?? []), rationale: S.ladder[t]?.rationale ?? 'auto' }])) };
}
function presetPairs(preset) {
  const local = MODELS.filter((m) => S.connections.get(m.connection_id)?.provider === 'local_openai');
  const cloud = MODELS.filter((m) => S.connections.get(m.connection_id)?.provider !== 'local_openai');
  const warnings = [];
  const out = {};
  for (const t of AI_TASK_IDS) {
    const need = t === 'visual_review';
    const pick = (list) => list.filter((m) => !need || m.capabilities?.vision).map((m) => ({ connection_id: m.connection_id, model: m.model }));
    let l = pick(local), c = pick(cloud);
    if (need && local.length && !l.length) warnings.push('No local model supports images; visual review has no local route.');
    if (preset === 'local_first') out[t] = [...l, ...c];
    else if (preset === 'cloud_first') out[t] = [...c, ...l];
    else if (preset === 'all_local') out[t] = l;
    else if (preset === 'all_cloud') out[t] = c;
    else out[t] = [];
    if (preset === 'all_local' && need && !l.length) warnings.push('All local: visual review has no route.');
  }
  return { out, warnings: [...new Set(warnings)] };
}
function activity(cid, a) {
  const rec = { at: now(), kind: 'attempt', evidence_ids: [], config_revision: S.ladderRev, origin: 'model_proposed', ...a };
  if (!S.activity.has(cid)) S.activity.set(cid, []);
  S.activity.get(cid).push(rec);
  emit('ai.activity', rec, cid, rec.job_id ?? null);
  return rec;
}

// ------------------------------------------------------------------------------------------------ seed
const DEMO = 'case_demo';
const OUT = process.platform === 'win32' ? 'C:\\Rebuilds\\inventory-tool' : '/home/demo/rebuilds/inventory-tool';
const SRC = process.platform === 'win32' ? 'C:\\Originals\\InventoryTool' : '/home/demo/originals/InventoryTool';

function planItem(o) {
  return {
    parent_id: null, outcome: '', kind: 'milestone', status: 'queued', owner: null, depends_on: [], acceptance: [], evidence_ids: [], files: [],
    preview_id: null, blockers: [], feature_id: null, job_ids: [], sort_order: 0, updated_at: now(), ...o,
  };
}

function seed() {
  S = freshState();
  const c = {
    case_id: DEMO, name: 'Inventory Tool (demo)', source_root: SRC, output_root: OUT, target_language: 'web', output_type: 'pwa',
    ai_policy: { mode: 'assist_on_failure', budget_usd: 0.5 }, launch_profile: { execute_original: true, command: ['InventoryTool.exe', '--demo'], scenarios: [{ name: 'list items', args: ['list'] }] },
    status: 'created', created_at: now(), updated_at: now(), app_version: '0.1.0-mock', settings: {}, started_at: null, resources: null,
  };
  S.cases.set(DEMO, c);
  emit('case.created', { case: c }, DEMO);
  const items = [
    planItem({ item_id: 'M1', title: 'Inventory the original', outcome: 'Every file classified with format, profile and hash', sort_order: 1, owner: 'discovery', acceptance: [{ id: 'A1', command: 'rebuildctl inventory --check', status: 'untested' }] }),
    planItem({ item_id: 'M1.1', parent_id: 'M1', kind: 'deliverable', title: 'Module list with formats', outcome: 'modules table populated', sort_order: 1 }),
    planItem({ item_id: 'M2', title: 'Recover program logic', outcome: 'Functions and data structures recovered with evidence', depends_on: ['M1'], sort_order: 2, owner: 'recovery', origin: 'deterministic', ai: { task: 'interpretation', primary: { provider: 'local_openai', model: 'qwen2.5-coder:14b', locality: 'local' }, fallbacks: [{ provider: 'openai', model: 'gpt-demo-small', locality: 'cloud' }], rationale: 'Local first: free and private', expected_cost: { min_usd: 0, max_usd: 0.02, known: true }, budget_usd: 0.5, runs_without_ai: true, without_ai: 'Functions are still recovered deterministically; AI only adds explanations.' } }),
    planItem({ item_id: 'M2.1', parent_id: 'M2', kind: 'deliverable', title: 'Item list command', outcome: '`list` prints the same rows as the original', sort_order: 1, feature_id: 'F1' }),
    planItem({ item_id: 'M2.2', parent_id: 'M2', kind: 'deliverable', title: 'Add item dialog', outcome: 'Adding an item updates the saved file identically', sort_order: 2, feature_id: 'F2' }),
    planItem({ item_id: 'M3', title: 'Implement the web rebuild', outcome: 'HTML/CSS/JS app with matching behaviour', depends_on: ['M2'], sort_order: 3, owner: 'implementation', origin: 'model_proposed', ai: { task: 'repair', primary: { provider: 'anthropic', model: 'claude-demo', locality: 'cloud' }, fallbacks: [{ provider: 'openai', model: 'gpt-demo-large', locality: 'cloud' }, { provider: 'local_openai', model: 'deepseek-coder-v2:16b', locality: 'local' }], rationale: null, expected_cost: { unknown_price: true }, budget_usd: 0.5, runs_without_ai: false, without_ai: 'A deterministic web port is attempted first; otherwise this becomes a scaffold.' } }),
    planItem({ item_id: 'M4', title: 'Build & package as PWA', outcome: 'Installable PWA in dist/', depends_on: ['M3'], sort_order: 4, owner: 'build' }),
    planItem({ item_id: 'M5', title: 'Verify against the original', outcome: 'All critical features pass comparisons', depends_on: ['M4'], sort_order: 5, owner: 'verifier', origin: 'verifier_decided', acceptance: ['exit code equal', 'stdout equal', 'screens within declared tolerance'] }),
    planItem({ item_id: 'D1', kind: 'discovery', title: 'Unknown plugin format in data/plugins', outcome: 'Decide whether plugins are needed', sort_order: 10 }),
    planItem({ item_id: 'X1', kind: 'deferred', title: 'Printing support', outcome: 'Deferred until core features verify', sort_order: 20 }),
    planItem({ item_id: 'U1', kind: 'unsupported', title: 'Windows registry integration', outcome: 'Not available to a web target', sort_order: 30, blockers: ['web target has no registry'] }),
  ];
  const plan = {
    revision: 1, items: new Map(items.map((i) => [i.item_id, i])), unknown_scope: [{ id: 'D1', title: 'data/plugins/*.plg', reason: 'format not recognised yet' }],
    progress: { analysis: { done: 0, total: null, unit: 'files' } }, eta: null,
    revisions: [{ revision: 1, reason: 'Initial plan from project settings', created_at: now(), changes: ['Created milestones M1–M5'] }],
  };
  S.plans.set(DEMO, plan);
  emit('plan.revised', { revision: 1, reason: 'Initial plan from project settings', progress: plan.progress, eta: null, unknown_scope: plan.unknown_scope, items }, DEMO);

  for (const f of [
    { feature_id: 'F1', title: 'List items', critical: true },
    { feature_id: 'F2', title: 'Add item', critical: true },
    { feature_id: 'F3', title: 'Main window layout', critical: false },
  ]) S.features.set(f.feature_id, { case_id: DEMO, description: '', origin: 'static', impl_status: 'planned', verify_status: 'untested', verify_candidate: null, user_review: null, ...f });

  S.connections.set('conn_local', { connection_id: 'conn_local', provider: 'local_openai', label: 'Ollama (this PC)', endpoint: 'http://127.0.0.1:11434/v1', auth_mode: 'local', models: ['qwen2.5-coder:14b', 'deepseek-coder-v2:16b', 'llava:13b'], capabilities: {}, limits: {}, state: 'ok', last_probe: now(), created_at: now() });
  S.connections.set('conn_openai', { connection_id: 'conn_openai', provider: 'openai', label: 'OpenAI (demo key)', endpoint: 'https://api.openai.com/v1', auth_mode: 'api_key', models: ['gpt-demo-large', 'gpt-demo-small'], capabilities: {}, limits: {}, state: 'ok', last_probe: now(), created_at: now() });
  S.connections.set('conn_handoff', { connection_id: 'conn_handoff', provider: 'anthropic', label: 'Claude desktop (handoff)', endpoint: '', auth_mode: 'subscription_handoff', models: ['claude-demo'], capabilities: {}, limits: { text: 'Uses the external client’s subscription limits; about 40 requests per 5 hours (demo value).' }, state: 'unprobed', last_probe: null, created_at: now() });
  for (const t of ['interpretation', 'repair', 'visual_review', 'verification_assist', 'knowledge'])
    S.routes.set(t, { task: t, primary_connection: t === 'visual_review' ? 'conn_handoff' : 'conn_openai', primary_model: t === 'visual_review' ? 'claude-demo' : 'gpt-demo-small', fallbacks: t === 'repair' ? [{ connection: 'conn_handoff', model: 'claude-demo' }] : [], updated_at: now() });
  {
    const lp = (t) => presetPairs('local_first').out[t];
    for (const t of AI_TASK_IDS) S.ladder[t] = { entries: lp(t), rationale: 'preset:local_first' };
  }
  S.budgets.set('b_case', { budget_id: 'b_case', scope: `case:${DEMO}`, limit_usd: 5, reserved_usd: 0, spent_usd: 0, currency: 'USD', updated_at: now() });
  S.budgets.set('b_jev', { budget_id: 'b_jev', scope: 'jev:monthly:2026-10', limit_usd: 1, reserved_usd: 0, spent_usd: 0.012, currency: 'USD', updated_at: now() });

  const k = (o) => ({ version: 1, constraints: {}, author: 'tool', source: 'demo', confidence: 0.9, evidence: [], regression: {}, lineage: null, created_at: now(), updated_at: now(), ...o });
  for (const e of [
    k({ knowledge_id: 'kn_1', kind: 'signature', name: 'MSVC 2019 CRT startup', state: 'promoted', constraints: { arch: 'x86_64', compiler: 'msvc 19.2x' }, regression: { cases: [{ id: 'fixture-pe-cli', verdict: 'pass' }, { id: 'fixture-pe-gui', verdict: 'pass' }] } }),
    k({ knowledge_id: 'kn_2', kind: 'recipe', name: 'INI settings → localStorage', state: 'proposed', author: 'model:gpt-demo-small', confidence: 0.6, constraints: { target: 'web' } }),
    k({ knowledge_id: 'kn_3', kind: 'parser', name: 'Legacy .plg header', state: 'quarantined', regression: { passed: 1, failed: 2, cases: [{ id: 'fixture-plg-a', verdict: 'pass' }, { id: 'fixture-plg-b', verdict: 'fail', detail: 'length field misread' }, { id: 'fixture-plg-c', verdict: 'fail', detail: 'checksum mismatch' }] } }),
    k({ knowledge_id: 'kn_4', kind: 'type_lib', name: 'Win32 common controls', state: 'rolled_back', lineage: 'kn_0' }),
  ]) S.knowledge.set(e.knowledge_id, e);

  S.settings = {
    storage: { data_dir: process.platform === 'win32' ? 'C:\\Users\\demo\\AppData\\Local\\RebuildStudio' : '/home/demo/.local/share/rebuild-studio', cache_max_gb: 20 },
    concurrency: { max_concurrent_jobs: 3, worker_heartbeat_seconds: HEARTBEAT, lease_timeout_seconds: HEARTBEAT },
    capture: { screenshots: true, record_video: false, capture_network: false },
    network: { allow_downloads: true, allow_original_network: false },
    output: { atomic_publish: true, keep_candidates: 5 },
  };
  S.hermes = { paired: false, state: 'not_paired', profile_path: null, mcp_registered: false, mode: 'agent session (not cua-driver)', diagnostics: [{ check: 'Hermes installation', ok: false, detail: 'Not found on this host (demo).' }, { check: 'MCP server binary', ok: true, detail: 'rebuild-studio-mcp available' }] };
}

// ------------------------------------------------------------------------------------------------ scenario
function setCase(cid, status) {
  const c = S.cases.get(cid);
  c.status = status;
  c.updated_at = now();
  emit('case.status', { case_id: cid, status }, cid);
}

function newJob(cid, stage, title, extra = {}) {
  const j = { job_id: id('job'), case_id: cid, stage, title, state: 'queued', attempt: 0, max_attempts: 3, priority: 100, progress: {}, blocker: null, heartbeat_at: null, started_at: null, finished_at: null, error: null, milestone_id: extra.milestone_id ?? null, created_at: now(), updated_at: now() };
  S.jobs.set(j.job_id, j);
  emit('job.created', { job: j, depends_on: [] }, cid, j.job_id);
  return j;
}
function startJob(j) {
  j.state = 'running';
  j.attempt += 1;
  j.started_at = now();
  j.heartbeat_at = Date.now() / 1000;
  emit('job.started', { job_id: j.job_id, attempt: j.attempt, worker: 'mock-1', stage: j.stage, title: j.title }, j.case_id, j.job_id);
}
const STAGE_PHASE = { inventory: 'analysis', recover: 'recovery', build: 'build', verify: 'verification' };
function progressJob(j, progress) {
  j.progress = progress;
  j.heartbeat_at = Date.now() / 1000;
  emit('job.progress', { job_id: j.job_id, progress, stage: j.stage }, j.case_id, j.job_id);
  // the plan's per-phase counters follow measured job progress; the plan.item event carries the new totals
  const plan = S.plans.get(j.case_id);
  const phase = STAGE_PHASE[j.stage];
  if (plan && phase && typeof progress.done === 'number') {
    plan.progress = { ...plan.progress, [phase]: { done: progress.done, total: progress.total ?? null, unit: progress.unit } };
    const item = j.milestone_id ? plan.items.get(j.milestone_id) : [...plan.items.values()][0];
    emit('plan.item', { item, progress: plan.progress }, j.case_id);
  }
}
function finishJob(j, state = 'completed', extra = {}) {
  j.state = state;
  j.finished_at = now();
  Object.assign(j, extra);
  emit(`job.${state}`, { job_id: j.job_id, title: j.title, ...extra }, j.case_id, j.job_id);
}
function setItem(cid, itemId, patch) {
  const plan = S.plans.get(cid);
  const it = plan.items.get(itemId);
  Object.assign(it, patch, { updated_at: now() });
  emit('plan.item', { item: it }, cid);
}
function revise(cid, reason, patch, changes = []) {
  const plan = S.plans.get(cid);
  plan.revision += 1;
  Object.assign(plan, patch);
  plan.revisions.push({ revision: plan.revision, reason, created_at: now(), changes });
  emit('plan.revised', { revision: plan.revision, reason, progress: plan.progress, eta: plan.eta, unknown_scope: plan.unknown_scope, items: [...plan.items.values()] }, cid);
}
function log(j, text, level = 'info', detail = null) {
  // same shape as the controller's ctx.log(): {job_id, stage, milestone, plan_item_id, level, text, detail, at} (+ legacy `message`)
  emit('job.log', { job_id: j.job_id, stage: j.stage, milestone: j.milestone_id ?? null, plan_item_id: null, level, text, detail, at: now(), message: text }, j.case_id, j.job_id);
}
// GET /cases/{id}/log: job.log / ai.activity rows merged with job state transitions, rendered as text (mirrors controller livelog.py)
const LOG_KINDS = new Set(['job.log', 'job.started', 'job.completed', 'job.failed', 'job.blocked', 'job.cancelled', 'job.retry', 'ai.activity']);
const first = (x) => String(x ?? '').split('\n').find((l) => l.trim())?.trim().slice(0, 300) ?? '';
function logRow(e) {
  const p = e.payload ?? {};
  const jid = p.job_id ?? e.job_id ?? null;
  const job = jid ? S.jobs.get(jid) : null;
  const stage = p.stage ?? job?.stage ?? null;
  const title = p.title ?? job?.title ?? stage ?? 'job';
  const base = { seq: e.seq, at: p.at ?? e.ts, job_id: jid, stage, milestone: p.milestone ?? job?.milestone_id ?? null, plan_item_id: p.plan_item_id ?? null, detail: null };
  switch (e.kind) {
    case 'job.log': return (p.text ?? p.message) ? { ...base, kind: 'log', level: ['warn', 'error'].includes(p.level) ? p.level : 'info', text: String(p.text ?? p.message), detail: p.detail ?? null } : null;
    case 'ai.activity': return p.text ? { ...base, kind: 'ai', level: ['failed', 'error', 'refused', 'timeout'].includes(p.outcome) ? 'warn' : 'info', text: String(p.text), provider: p.provider ?? null, model: p.model ?? null, outcome: p.outcome ?? null } : null;
    case 'job.started': return { ...base, kind: 'job', level: 'info', text: `Started: ${title}${p.attempt > 1 ? ` (attempt ${p.attempt})` : ''}` };
    case 'job.completed': return { ...base, kind: 'job', level: 'info', text: `Finished: ${title}` };
    case 'job.failed': return { ...base, kind: 'job', level: 'error', text: `Failed: ${title}${p.error ? ` — ${first(p.error)}` : ''}` };
    case 'job.cancelled': return { ...base, kind: 'job', level: 'info', text: `Cancelled: ${title}` };
    case 'job.blocked': return { ...base, kind: 'job', level: 'warn', text: `Waiting: ${title} — ${first(p.blocker ?? p.error ?? 'blocked')}` };
    case 'job.retry': return { ...base, kind: 'job', level: 'warn', text: `Retrying ${title}: ${first(p.error)}` };
    default: return null;
  }
}
function caseLog(cid, q) {
  const since = Number(q.get('since') ?? 0) || 0;
  const limit = Math.min(Math.max(Number(q.get('limit') ?? 300) || 300, 1), 2000);
  const rank = { info: 0, warn: 1, error: 2 }[q.get('level') ?? ''] ?? 0;
  const stage = q.get('stage') || null;
  const rows = S.events.filter((e) => e.case_id === cid && e.seq > since && LOG_KINDS.has(e.kind)).map(logRow)
    .filter((r) => r && ({ info: 0, warn: 1, error: 2 }[r.level] >= rank) && (!stage || r.stage === stage));
  return { entries: since > 0 ? rows.slice(0, limit) : rows.slice(-limit), latest_seq: S.seq, limit };
}
function setFeature(fid, patch) {
  const f = S.features.get(fid);
  Object.assign(f, patch);
  emit('feature.updated', { feature: f }, f.case_id);
}
function svg(label, color, shift = 0) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200"><rect width="320" height="200" fill="#f4f4f6"/><rect x="0" y="0" width="320" height="28" fill="${color}"/><text x="12" y="19" font-family="sans-serif" font-size="13" fill="#fff">Inventory Tool</text><rect x="${16 + shift}" y="44" width="180" height="16" fill="#d0d0d8"/><rect x="16" y="68" width="220" height="16" fill="#d0d0d8"/><rect x="16" y="92" width="150" height="16" fill="#d0d0d8"/><text x="16" y="180" font-family="sans-serif" font-size="12" fill="#555">${label}</text></svg>`;
}
const ART = {
  'original.svg': svg('original', '#2b5797'),
  'candidate-v1.svg': svg('candidate v1', '#2b5797', 6),
  'diff-v1.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="#111"/><rect x="16" y="44" width="186" height="16" fill="#ff3b30"/><text x="16" y="180" font-family="sans-serif" font-size="12" fill="#eee">diff: 1.9% pixels differ</text></svg>`,
  'candidate-v2.svg': svg('candidate v2', '#2b5797'),
};

function makeCandidate(cid, revision, lkg = false) {
  const plan = S.plans.get(cid);
  const c = { candidate_id: `cand_v${revision}`, case_id: cid, revision, target_language: 'web', output_type: 'pwa', source_dir: `${OUT}${path.sep}source`, dist_dir: null, build_hash: null, plan_revision: plan.revision, build_status: 'building', verification: 'untested', last_known_good: lkg, created_at: now(), meta: {} };
  S.candidates.set(c.candidate_id, c);
  emit('candidate.created', { candidate: c }, cid);
  return c;
}
function buildCandidate(c) {
  c.build_status = 'built';
  c.dist_dir = `${OUT}${path.sep}dist${path.sep}v${c.revision}`;
  c.build_hash = 'sha256:' + sha(`build-${c.revision}`);
  emit('candidate.built', { candidate: c }, c.case_id);
}
function publishPreview(c, kind, title, extra = {}) {
  const p = {
    preview_id: `pv_${c.revision}_${kind}`, case_id: c.case_id, candidate_id: c.candidate_id, plan_revision: c.plan_revision, kind, title,
    available: ['List items', 'Main window layout'], incomplete: ['Add item dialog (saves are not written yet)', 'Printing (deferred)'],
    requirements: ['A modern browser (Chromium/Edge/Firefox)'], steps: ['Open the preview', 'Click “List”', 'Compare the rows with the original screenshot'],
    launch: { kind: 'browser', url: `http://127.0.0.1:${PORT}/__mock/preview/${c.revision}` }, build_hash: c.build_hash, verification: c.verification, stale: false, stale_reason: null, feature_ids: ['F1', 'F2', 'F3'], created_at: now(), ...extra,
  };
  S.previews.set(p.preview_id, p);
  emit('preview.published', { preview: p }, c.case_id);
  return p;
}
function syncPreviews(c) {
  for (const p of S.previews.values()) {
    if (p.candidate_id === c.candidate_id && p.kind === 'real' && p.verification !== c.verification) {
      p.verification = c.verification;
      emit('preview.published', { preview: p }, c.case_id);
    }
  }
}
function compare(c, feature_id, channel, rule, tolerance, verdict, artifacts = [], details = '') {
  const r = { comparison_id: id('cmp'), case_id: c.case_id, candidate_id: c.candidate_id, feature_id, channel, rule, tolerance, verdict, original_hash: 'sha256:' + sha(`orig-${channel}-${feature_id}`), candidate_hash: 'sha256:' + sha(verdict === 'pass' ? `orig-${channel}-${feature_id}` : `cand-${c.revision}-${channel}`), details, command: channel === 'ui' || channel === 'screens' ? 'playwright screenshot /' : 'InventoryTool.exe list', environment: { host: 'mock' }, artifacts, created_at: now(), writer: 'verifier' };
  S.comparisons.push(r);
  emit('comparison.recorded', { comparison_id: r.comparison_id, candidate_id: c.candidate_id, verdict }, c.case_id);
}

const jobsOf = (k) => [...S.jobs.values()].find((j) => j.case_id === DEMO && j.stage === k);

const STEPS = [
  // 1: start — discovery running with unknown scope
  () => {
    setCase(DEMO, 'running');
    S.cases.get(DEMO).started_at = now();
    const j = newJob(DEMO, 'inventory', 'Inventory the original', { milestone_id: 'M1' });
    startJob(j);
    setItem(DEMO, 'M1', { status: 'running', job_ids: [j.job_id] });
    progressJob(j, { done: 12, total: null, unit: 'files', current: 'Scanning InventoryTool.exe' });
    log(j, 'Scanning the installation folder C:\\Demo\\InventoryTool…');
  },
  // 2: more discovery, evidence
  () => {
    const j = jobsOf('inventory');
    progressJob(j, { done: 48, total: null, unit: 'files', current: 'Scanning data/plugins' });
    log(j, 'Scanning the installation folder… 48 files so far');
    log(j, 'Not analysed: data/plugins/a.plg — unknown binary format (kept as evidence only)', 'warn');
    for (const [rel, fmt, prof, size] of [['InventoryTool.exe', 'pe', 'native', 1843200], ['core.dll', 'pe', 'native', 512000], ['data/items.ini', 'ini', 'data', 2048]]) {
      const m = { module_id: 'mod_' + sha(rel).slice(0, 12), case_id: DEMO, rel_path: rel, sha256: sha(rel), size, format: fmt, profile: prof, arch: fmt === 'pe' ? 'x86_64' : null };
      S.modules.set(m.module_id, m);
    }
    const ev = { evidence_id: id('ev'), case_id: DEMO, kind: 'inventory', module_id: null, title: 'Inventory of source folder', revision: 1, producer: 'inventory', created_at: now(), stale: false, body: 'InventoryTool.exe  PE32+ x86-64  1.8 MB\ncore.dll           PE32+ x86-64  500 KB\ndata/items.ini     INI           2 KB\ndata/plugins/a.plg unknown       12 KB' };
    S.evidence.set(ev.evidence_id, ev);
    emit('evidence.added', { evidence_id: ev.evidence_id, kind: ev.kind, title: ev.title, module_id: null, revision: 1 }, DEMO);
    setItem(DEMO, 'M1', { evidence_ids: [ev.evidence_id] });
  },
  // 3: discovery done → scope known
  () => {
    const j = jobsOf('inventory');
    progressJob(j, { done: 52, total: 52, unit: 'files' });
    log(j, 'Scanned the installation folder: 52 files, 3 modules (native_pe)');
    finishJob(j);
    setItem(DEMO, 'M1', { status: 'completed' });
    setItem(DEMO, 'M1.1', { status: 'completed', files: ['reports/inventory.json'] });
    const r = newJob(DEMO, 'recover', 'Recover functions (rizin)', { milestone_id: 'M2' });
    startJob(r);
    setItem(DEMO, 'M2', { status: 'running', job_ids: [r.job_id] });
    revise(DEMO, 'Discovery finished: 3 modules, 8 functions to recover', { progress: { analysis: { done: 52, total: 52, unit: 'files' }, recovery: { done: 0, total: 8, unit: 'functions' }, implementation: { done: 0, total: null }, build: { done: 0, total: null }, verification: { done: 0, total: null } } }, ['M1 completed', 'Recovery scope set to 8 functions']);
    progressJob(r, { done: 0, total: 8, unit: 'functions', current: 'sub_401000' });
    log(r, 'Analysing InventoryTool.exe with Rizin 0.7 + Ghidra: 0 of 8 functions');
  },
  // 4: recovery progress
  () => {
    const r = jobsOf('recover');
    progressJob(r, { done: 4, total: 8, unit: 'functions', current: 'list_items @ 0x401a20' });
    const plan = S.plans.get(DEMO);
    plan.progress = { ...plan.progress, recovery: { done: 4, total: 8, unit: 'functions' } };
    emit('plan.item', { item: plan.items.get('M2.1'), progress: plan.progress }, DEMO);
    setItem(DEMO, 'M2.1', { status: 'running' });
    log(r, 'Analysing InventoryTool.exe with Rizin 0.7 + Ghidra: 4 of 8 functions');
  },
  // 5: scope grows (denominator change) + ETA
  () => {
    const r = jobsOf('recover');
    progressJob(r, { done: 6, total: 11, unit: 'functions', current: 'plugin_load @ 0x402300' });
    log(r, 'Found 3 more functions referenced from data/plugins; now 6 of 11', 'warn');
    revise(DEMO, 'Found 3 more functions referenced from data/plugins', {
      progress: { ...S.plans.get(DEMO).progress, recovery: { done: 6, total: 11, unit: 'functions' } },
      eta: { seconds: 1800, uncertainty: 900, updated_at: now() },
    }, ['Recovery scope 8 → 11 functions', 'D1 linked to M2']);
  },
  // 6: candidate v1 built, previews, comparisons
  () => {
    const r = jobsOf('recover');
    progressJob(r, { done: 11, total: 11, unit: 'functions' });
    finishJob(r);
    setItem(DEMO, 'M2', { status: 'completed' });
    setItem(DEMO, 'M2.1', { status: 'completed' });
    setItem(DEMO, 'M2.2', { status: 'blocked', blockers: ['save format of items.ini not fully decoded'] });
    setItem(DEMO, 'M3', { status: 'completed', files: ['source/index.html', 'source/app.js', 'source/styles.css'] });
    const b = newJob(DEMO, 'build', 'Build PWA', { milestone_id: 'M4' });
    startJob(b);
    log(b, 'Building the web candidate…');
    const c = makeCandidate(DEMO, 1);
    buildCandidate(c);
    progressJob(b, { done: 1, total: 1, unit: 'builds' });
    log(b, 'Built the web candidate: 6 files copied');
    log(b, 'Running the original in an isolated process: scenario 3 of 8');
    log(b, 'Comparing: 6 of 8 scenarios match', 'warn', 'screens: 1.9% of pixels differ (limit 1%)\nfiles: skipped, saving is not implemented in this candidate');
    finishJob(b);
    setItem(DEMO, 'M4', { status: 'completed', files: ['dist/v1/index.html', 'dist/v1/manifest.webmanifest'] });
    const real = publishPreview(c, 'real', 'Inventory Tool — web build v1');
    publishPreview(c, 'mockup', 'Add item dialog (mockup)', { available: ['Dialog layout'], incomplete: ['Saving'], steps: ['Open the mockup', 'Check the field order'], feature_ids: ['F2'], launch: { kind: 'browser', url: `http://127.0.0.1:${PORT}/__mock/preview/mockup` } });
    setItem(DEMO, 'M4', { preview_id: real.preview_id });
    setItem(DEMO, 'M5', { status: 'running' });
    compare(c, 'F1', 'exit_code', 'equal', {}, 'pass', [], 'both exited 0');
    compare(c, 'F1', 'stdout', 'equal after newline normalisation', { normalize_newlines: true }, 'pass', [], '12 lines identical');
    compare(c, 'F3', 'screens', 'pixel diff', { max_diff_ratio: 0.01, ignore_regions: 1 }, 'fail', [
      { role: 'original', name: 'original.png', media_type: 'image/svg+xml', url: '/__mock/artifacts/original.svg', path: `${OUT}/evidence/original.png` },
      { role: 'candidate', name: 'candidate.png', media_type: 'image/svg+xml', url: '/__mock/artifacts/candidate-v1.svg', path: `${OUT}/evidence/candidate-v1.png` },
      { role: 'diff', name: 'diff.png', media_type: 'image/svg+xml', url: '/__mock/artifacts/diff-v1.svg' },
    ], { diff_ratio: 0.019, threshold: 0.01 });
    compare(c, 'F2', 'files', 'byte equal', {}, 'skipped', [], 'saving not implemented in this candidate');
    c.verification = 'partial';
    emit('candidate.built', { candidate: c }, DEMO);
    syncPreviews(c);
    setFeature('F1', { impl_status: 'runnable', verify_status: 'verified', verify_candidate: c.candidate_id });
    setFeature('F3', { impl_status: 'runnable', verify_status: 'failed', verify_candidate: c.candidate_id });
    const plan = S.plans.get(DEMO);
    revise(DEMO, 'Candidate v1 built and compared', { progress: { ...plan.progress, implementation: { done: 2, total: 3, unit: 'features' }, build: { done: 1, total: 1, unit: 'builds' }, verification: { done: 3, total: 4, unit: 'checks' } }, eta: { seconds: 900, uncertainty: 600, updated_at: now() } }, ['v1 built', 'M2.2 blocked on save format']);
    const b2 = S.budgets.get('b_case');
    b2.spent_usd = 0.084;
    b2.reserved_usd = 0.12;
    emit('budget.updated', { budget: b2 }, DEMO);
    S.aiCalls.push({ call_id: id('call'), case_id: DEMO, job_id: r.job_id, provider: 'openai', model: 'gpt-demo-small', task: 'interpretation', input_tokens: 5400, output_tokens: 800, cached_tokens: 0, cost_usd: 0.084, cost_known: true, outcome: 'ok', latency_ms: 2300, created_at: now() });
    emit('ai.call', { task: 'interpretation', cost_usd: 0.084 }, DEMO);
    activity(DEMO, { text: 'Interpreting list_items with local model qwen2.5-coder:14b', plan_item_id: 'M2.1', job_id: r.job_id, task: 'interpretation', provider: 'local_openai', model: 'qwen2.5-coder:14b', locality: 'local', outcome: 'model_unavailable', fallback_reason: 'qwen2.5-coder:14b is not available (model not found); trying gpt-demo-small', origin: 'model_proposed' });
    activity(DEMO, { text: 'gpt-demo-small explained 3 functions', plan_item_id: 'M2.1', job_id: r.job_id, task: 'interpretation', provider: 'openai', model: 'gpt-demo-small', locality: 'cloud', outcome: 'ok', tokens_in: 5400, tokens_out: 800, cost_usd: 0.084, cost_known: true, evidence_ids: ['ev_demo'], origin: 'model_proposed' });
    activity(DEMO, { kind: 'note', text: 'Next: repair attempt 1 of 3', plan_item_id: 'M3', origin: 'deterministic' });
  },
  // 7: triage open feedback
  () => {
    for (const f of S.feedback.values()) {
      if (f.status === 'received' || f.status === 'reopened') {
        f.status = 'in_progress';
        f.linked_items = [...new Set([...(f.linked_items ?? []), 'M3'])];
        f.history.push({ ts: now(), status: 'triaged', note: 'Linked to M3', actor: 'controller' }, { ts: now(), status: 'in_progress', note: 'Repair job queued', actor: 'controller' });
        f.updated_at = now();
        emit('feedback.updated', { feedback: f }, f.case_id);
      }
    }
  },
  // 8: candidate v2 fixes screens; v1 previews go stale; feedback ready to retest
  () => {
    const v1 = S.candidates.get('cand_v1');
    v1.last_known_good = true;
    emit('candidate.built', { candidate: v1 }, DEMO);
    const c = makeCandidate(DEMO, 2);
    buildCandidate(c);
    for (const p of S.previews.values()) {
      if (p.candidate_id === v1.candidate_id && !p.stale) {
        p.stale = true;
        p.stale_reason = 'superseded by version 2';
        emit('preview.stale', { preview_id: p.preview_id, reason: p.stale_reason }, DEMO);
      }
    }
    publishPreview(c, 'real', 'Inventory Tool — web build v2');
    compare(c, 'F1', 'exit_code', 'equal', {}, 'pass');
    compare(c, 'F1', 'stdout', 'equal after newline normalisation', { normalize_newlines: true }, 'pass');
    compare(c, 'F3', 'screens', 'pixel diff', { max_diff_ratio: 0.01, ignore_regions: 1 }, 'pass', [
      { role: 'original', name: 'original.png', media_type: 'image/svg+xml', url: '/__mock/artifacts/original.svg' },
      { role: 'candidate', name: 'candidate.png', media_type: 'image/svg+xml', url: '/__mock/artifacts/candidate-v2.svg' },
    ], { diff_ratio: 0.004, threshold: 0.01 });
    c.verification = 'partial';
    emit('candidate.built', { candidate: c }, DEMO);
    syncPreviews(c);
    setFeature('F3', { verify_status: 'verified', verify_candidate: c.candidate_id });
    for (const f of S.feedback.values()) {
      if (f.status === 'in_progress') {
        f.status = 'ready_to_retest';
        f.context = { ...f.context, fixed_candidate_id: c.candidate_id };
        f.history.push({ ts: now(), status: 'ready_to_retest', note: `Fixed in version ${c.revision}`, actor: 'controller' });
        f.updated_at = now();
        emit('feedback.updated', { feedback: f }, f.case_id);
      }
    }
    const plan = S.plans.get(DEMO);
    revise(DEMO, 'Candidate v2 fixes the window layout', { progress: { ...plan.progress, build: { done: 2, total: 2, unit: 'builds' }, verification: { done: 6, total: 8, unit: 'checks' } }, eta: { seconds: 600, uncertainty: 600, updated_at: now() } }, ['v2 built', 'v1 previews stale']);
  },
  // 9: stall (no heartbeats) → UI shows stale
  () => {
    S.stalledUntil = Date.now() + HEARTBEAT * 3 * 1000;
  },
  // 10: disconnect briefly → UI reconnects and replays
  () => {
    const j = newJob(DEMO, 'verify', 'Verify remaining features', { milestone_id: 'M5' });
    disconnect(Math.max(3, HEARTBEAT));
    setTimeout(() => {
      startJob(j);
      progressJob(j, { done: 6, total: 8, unit: 'checks', current: 'files: items.ini round-trip' });
    }, 500);
  },
];

function step() {
  if (S.step >= STEPS.length) return false;
  STEPS[S.step]();
  S.step += 1;
  return true;
}

function scheduleAuto() {
  if (!AUTO_MS || S.autoTimer) return;
  S.autoTimer = setInterval(() => {
    const c = S.cases.get(DEMO);
    if (c.status !== 'running') return;
    if (!step()) {
      clearInterval(S.autoTimer);
      S.autoTimer = null;
    }
  }, AUTO_MS);
}

function disconnect(seconds) {
  S.disconnectedUntil = Date.now() + seconds * 1000;
  for (const ws of sockets) ws.terminate();
}

// ------------------------------------------------------------------------------------------------ http helpers
function send(res, status, body, headers = {}) {
  const data = body === undefined ? '' : typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': typeof body === 'object' && !Buffer.isBuffer(body) ? 'application/json' : 'text/plain', 'Cache-Control': 'no-store', ...headers });
  res.end(data);
}
const err = (res, status, code, message, affected, next_action) => send(res, status, { error: { code, message, affected, next_action } });
const notFound = (res, what) => err(res, 404, 'not_found', `${what} was not found.`, 'Only this request.', 'Refresh the view; the item may have been removed.');

function originAllowed(origin) {
  if (!origin) return true;
  return /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin) || origin === 'tauri://localhost' || origin === 'http://tauri.localhost';
}

async function readBody(req) {
  const chunks = [];
  let n = 0;
  for await (const c of req) {
    n += c.length;
    if (n > 40 * 1024 * 1024) throw Object.assign(new Error('body too large'), { status: 413 });
    chunks.push(c);
  }
  const s = Buffer.concat(chunks).toString('utf8');
  if (!s) return {};
  try {
    return JSON.parse(s);
  } catch {
    throw Object.assign(new Error('invalid JSON'), { status: 400 });
  }
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json', '.map': 'application/json', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2' };

function serveStatic(req, res, pathname) {
  if (!SERVE) return false;
  let rel = decodeURIComponent(pathname);
  if (rel === '/' || rel === '') rel = '/index.html';
  const file = path.resolve(SERVE, '.' + rel);
  if (!file.startsWith(SERVE)) return false;
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return false;
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' });
  fs.createReadStream(file).pipe(res);
  return true;
}

const casePlan = (cid) => {
  const p = S.plans.get(cid);
  return { revision: p.revision, items: [...p.items.values()], unknown_scope: p.unknown_scope, progress: p.progress, eta: p.eta };
};
const caseWithCounts = (c) => {
  const jobs = [...S.jobs.values()].filter((j) => j.case_id === c.case_id);
  const by = (arr, k) => arr.reduce((a, x) => ((a[x[k]] = (a[x[k]] ?? 0) + 1), a), {});
  return {
    ...c,
    counts: {
      jobs: by(jobs, 'state'),
      features: by([...S.features.values()].filter((f) => f.case_id === c.case_id), 'verify_status'),
      candidates: [...S.candidates.values()].filter((x) => x.case_id === c.case_id).length,
      previews: [...S.previews.values()].filter((x) => x.case_id === c.case_id).length,
      open_feedback: [...S.feedback.values()].filter((x) => x.case_id === c.case_id && x.status !== 'resolved').length,
    },
  };
};

// ------------------------------------------------------------------------------------------------ routes
async function handle(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const p = url.pathname;
  const m = req.method;
  const origin = req.headers.origin;
  if (origin && originAllowed(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  }
  if (m === 'OPTIONS') return send(res, 204, '');

  // mock-only control + static demo pages (no auth; loopback dev tool)
  if (p.startsWith('/__mock/')) return mockControl(req, res, url);

  const isApi = /^\/(health|events|doctor|capabilities|cases|jobs|evidence|candidates|previews|feedback|connections|routes|budgets|ai|knowledge|settings|hermes)(\/|$)/.test(p);
  if (!isApi) {
    if (m === 'GET' && serveStatic(req, res, p)) return;
    return send(res, 404, 'not found');
  }
  if (!originAllowed(origin)) return err(res, 403, 'origin_refused', `Requests from ${origin} are refused.`, 'This request.', 'Open Rebuild Studio from the desktop app.');
  if (req.headers.authorization !== `Bearer ${TOKEN}`) return err(res, 401, 'unauthorized', 'Missing or wrong controller token.', 'All controller requests.', 'Restart the app so a fresh token is injected.');
  // fault injection (POST /__mock/fail): answer the next matching request(s) with a canned error
  const fault = S.faults.find((f) => f.method === m && f.path === p && f.times > 0);
  if (fault) {
    fault.times -= 1;
    return send(res, fault.status, fault.body);
  }

  let body = {};
  if (m === 'POST' || m === 'PUT') {
    try {
      body = await readBody(req);
    } catch (e) {
      return err(res, e.status ?? 400, 'bad_request', e.message, 'This request.', 'Fix the request body.');
    }
  }
  const seg = p.split('/').filter(Boolean).map(decodeURIComponent);

  if (p === '/health') return send(res, 200, { ok: true, version: '0.1.0-mock', pid: process.pid, heartbeat_seconds: HEARTBEAT, latest_seq: S.seq, started_at: STARTED, mode: 'mock' });
  if (p === '/events') {
    const since = Number(url.searchParams.get('since') ?? 0);
    const cid = url.searchParams.get('case_id');
    return send(res, 200, S.events.filter((e) => e.seq > since && (!cid || e.case_id === cid || e.case_id == null)).slice(0, 5000));
  }
  if (p === '/doctor') return send(res, 200, doctor(url.searchParams.get('smoke') === '1'));
  if (p === '/capabilities') return send(res, 200, capabilities());
  if (p === '/cases' && m === 'GET') return send(res, 200, [...S.cases.values()].map(caseWithCounts).sort((a, b) => b.created_at.localeCompare(a.created_at)));
  if (p === '/cases' && m === 'POST') return createCase(res, body);

  if (seg[0] === 'cases' && seg[1]) {
    const c = S.cases.get(seg[1]);
    if (!c) return notFound(res, `Project ${seg[1]}`);
    const cid = c.case_id;
    const sub = seg.slice(2).join('/');
    if (!sub && m === 'GET') return send(res, 200, caseWithCounts(c));
    if (sub === 'start' && m === 'POST') {
      if (c.status === 'running') return err(res, 409, 'already_running', 'This project is already running.', 'Nothing changed.', 'Use Pause or Stop instead.');
      if (cid === DEMO) {
        if (S.step === 0) step();
        else setCase(cid, 'running');
        scheduleAuto();
      } else {
        setCase(cid, 'running');
        c.started_at = now();
        const j = newJob(cid, 'inventory', 'Inventory the original');
        startJob(j);
        progressJob(j, { done: 3, total: null, unit: 'files', current: 'Scanning source folder' });
        return send(res, 200, { job_ids: [j.job_id] });
      }
      return send(res, 200, { job_ids: [...S.jobs.values()].filter((j) => j.case_id === cid).map((j) => j.job_id) });
    }
    if (sub === 'ai-policy') {
      if (m === 'GET') return send(res, 200, { locality: 'any', approve_unknown_pricing: false, ladder_overrides: {}, ...c.ai_policy });
      if (m === 'PUT') {
        if (!['no_ai', 'inherit', 'custom', 'assist_on_failure', 'assisted'].includes(body.mode)) return err(res, 400, 'invalid_policy', 'Unknown AI mode.', 'The policy was not saved.', 'Choose No AI, the app ladder or a custom ladder.');
        c.ai_policy = body;
        c.updated_at = now();
        emit('case.updated', { case: c }, cid);
        return send(res, 200, c.ai_policy);
      }
    }
    if (sub === 'log' && m === 'GET') return send(res, 200, caseLog(cid, url.searchParams));
    if (sub === 'ai/activity' && m === 'GET') return send(res, 200, S.activity.get(cid) ?? []);
    if (sub === 'pause' && m === 'POST') {
      setCase(cid, 'paused');
      return send(res, 200, { ok: true });
    }
    if (sub === 'resume' && m === 'POST') {
      setCase(cid, 'running');
      if (cid === DEMO) scheduleAuto();
      return send(res, 200, { job_ids: [] });
    }
    if (sub === 'cancel' && m === 'POST') {
      const ids = [];
      for (const j of S.jobs.values()) if (j.case_id === cid && ['queued', 'running', 'blocked'].includes(j.state)) (finishJob(j, 'cancelled'), ids.push(j.job_id));
      setCase(cid, 'cancelled');
      return send(res, 200, { job_ids: ids });
    }
    if (sub === 'jobs') return send(res, 200, [...S.jobs.values()].filter((j) => j.case_id === cid));
    if (sub === 'modules') return send(res, 200, [...S.modules.values()].filter((x) => x.case_id === cid));
    if (sub === 'evidence') return send(res, 200, [...S.evidence.values()].filter((x) => x.case_id === cid && (!url.searchParams.get('kind') || x.kind === url.searchParams.get('kind'))).map(({ body: _b, ...e }) => e));
    if (sub === 'evidence/search') {
      const q = (url.searchParams.get('q') ?? '').toLowerCase();
      return send(res, 200, [...S.evidence.values()].filter((x) => x.case_id === cid && (x.title.toLowerCase().includes(q) || String(x.body).toLowerCase().includes(q))).map(({ body: _b, ...e }) => e));
    }
    if (sub === 'features') return send(res, 200, [...S.features.values()].filter((x) => x.case_id === cid));
    if (sub === 'consent/original-execution') {
      const cc = (MOCK_CONSENT[cid] ??= { allowed: !!S.cases.get(cid)?.launch_profile?.execute_original, at: S.cases.get(cid)?.launch_profile?.execute_original ? now() : null, via: S.cases.get(cid)?.launch_profile?.execute_original ? 'create_case' : null, history: [] });
      if (m === 'PUT') Object.assign(cc, { allowed: !!body.allow, at: now(), via: 'api' }, { history: [...cc.history, { allowed: !!body.allow, at: now(), via: 'api', note: body.note ?? '' }] });
      return send(res, 200, cc);
    }
    if (sub === 'scenarios' || sub.startsWith('scenarios/')) {
      const list = (MOCK_SCENARIOS[cid] ??= []);
      const cc = (MOCK_CONSENT[cid] ??= { allowed: false, at: null, via: null, history: [] });
      const view = () => ({ scenarios: list, consent: cc, counts: { declared: list.length, no_baseline: list.filter((x) => x.status === 'no_baseline').length, changed: 0, recorded: list.filter((x) => x.status !== 'no_baseline').length, passed: 0, failed: 0, not_run: list.filter((x) => x.status === 'not_run').length } });
      if (sub === 'scenarios/record' && m === 'POST') {
        if (!cc.allowed) return err(res, 409, 'original_execution_not_permitted', 'Original execution needs your permission: Rebuild Studio wants to run the original program to record its behaviour. The network is NOT blocked and the program can still read your files.', 'Nothing was run.', 'Allow running the original for this project, or supply a baseline file.');
        const rec = list.filter((x) => x.status === 'no_baseline' || x.status === 'changed');
        rec.forEach((x) => Object.assign(x, { status: 'not_run', recorded_at: now() }));
        return send(res, 200, { ...view(), evidence_id: id('ev'), baseline_revision: 1, recorded: rec.map((x) => x.scenario_id), scenarios_in_baseline: list.length });
      }
      const sid = sub.split('/')[1];
      if (m === 'GET' && !sid) return send(res, 200, view());
      if (m === 'POST' && !sid) {
        if (!String(body.title ?? '').trim()) return err(res, 400, 'scenario_title', 'The scenario has no title.', 'title', 'Give it a short name.');
        const sc = { ...body, scenario_id: id('usc'), case_id: cid, status: 'no_baseline', recorded_at: null, recorded_evidence: null, updated_at: now() };
        list.push(sc);
        return send(res, 200, sc);
      }
      const sc = list.find((x) => x.scenario_id === sid);
      if (!sc) return err(res, 404, 'scenario_not_found', 'There is no such scenario.', sid, 'Refresh the list.');
      if (m === 'PUT') return send(res, 200, Object.assign(sc, body, { status: sc.status === 'no_baseline' ? 'no_baseline' : 'changed', updated_at: now() }));
      if (m === 'DELETE') {
        list.splice(list.indexOf(sc), 1);
        return send(res, 200, { deleted: sid });
      }
      return send(res, 200, sc);
    }
    if (sub === 'plan') return send(res, 200, casePlan(cid));
    if (sub === 'plan/revisions') return send(res, 200, S.plans.get(cid).revisions);
    if (sub === 'plan/prioritize' && m === 'POST') {
      const plan = S.plans.get(cid);
      const it = plan.items.get(body.item_id);
      if (!it) return notFound(res, `Plan item ${body.item_id}`);
      setItem(cid, it.item_id, { sort_order: -1 });
      revise(cid, `Prioritized ${it.item_id} at user request`, {}, [`${it.item_id} moved to the front`]);
      return send(res, 200, { ok: true });
    }
    if (sub === 'plan/change' && m === 'POST') {
      const plan = S.plans.get(cid);
      const it = plan.items.get(body.item_id);
      if (!it) return notFound(res, `Plan item ${body.item_id}`);
      if (!body.request) return err(res, 400, 'missing_request', 'The change request is empty.', 'Nothing was changed.', 'Describe the change and send again.');
      const affected = [it.item_id, ...[...plan.items.values()].filter((x) => x.depends_on.includes(it.item_id) || x.parent_id === it.item_id).map((x) => x.item_id)];
      revise(cid, `Change requested on ${it.item_id}: ${body.request}${body.reason ? ` (${body.reason})` : ''}`, {}, affected.map((a) => `${a} needs review`));
      return send(res, 200, { affected_items: affected, affected_tests: ['stdout:F1', 'screens:F3'].filter(() => it.item_id.startsWith('M')) });
    }
    if (sub === 'plan/export') {
      const sep = OUT.includes('\\') ? '\\' : '/';
      return send(res, 200, { json: `${c.output_root}${sep}reports${sep}project-plan.json`, html: `${c.output_root}${sep}reports${sep}project-plan.html` });
    }
    if (sub === 'candidates') return send(res, 200, [...S.candidates.values()].filter((x) => x.case_id === cid));
    if (sub === 'comparisons') {
      const cand = url.searchParams.get('candidate_id');
      return send(res, 200, S.comparisons.filter((x) => x.case_id === cid && (!cand || x.candidate_id === cand)));
    }
    if (sub === 'previews') return send(res, 200, [...S.previews.values()].filter((x) => x.case_id === cid));
    if (sub === 'feedback' && m === 'GET') return send(res, 200, [...S.feedback.values()].filter((x) => x.case_id === cid));
    if (sub === 'feedback' && m === 'POST') return createFeedback(res, cid, body);
    return notFound(res, `Endpoint ${m} ${p}`);
  }

  if (seg[0] === 'jobs' && seg[1]) {
    const j = S.jobs.get(seg[1]);
    if (!j) return notFound(res, `Job ${seg[1]}`);
    if (seg[2] === 'cancel') {
      emit('job.cancel_requested', { job_id: j.job_id }, j.case_id, j.job_id);
      finishJob(j, 'cancelled');
      return send(res, 200, [j.job_id]);
    }
    if (seg[2] === 'resume') {
      j.state = 'queued';
      emit('job.resumed', { job_id: j.job_id, state: 'queued' }, j.case_id, j.job_id);
      return send(res, 200, { job_id: j.job_id });
    }
  }
  if (seg[0] === 'evidence' && seg[1]) {
    const e = S.evidence.get(seg[1]);
    if (!e) return notFound(res, `Evidence ${seg[1]}`);
    const max = Number(url.searchParams.get('max_bytes') ?? 65536);
    return send(res, 200, { ...e, body: String(e.body).slice(0, max) });
  }
  if (seg[0] === 'candidates' && seg[1]) {
    const c = S.candidates.get(seg[1]);
    return c ? send(res, 200, c) : notFound(res, `Candidate ${seg[1]}`);
  }
  if (seg[0] === 'previews' && seg[1]) {
    const pv = S.previews.get(seg[1]);
    if (!pv) return notFound(res, `Preview ${seg[1]}`);
    if (seg[2] === 'open') {
      const inst = id('inst');
      S.instances.set(pv.preview_id, inst);
      pv.instance_id = inst;
      return send(res, 200, { kind: 'browser', url: pv.launch.url, instance_id: inst });
    }
    if (seg[2] === 'stop') {
      S.instances.delete(pv.preview_id);
      pv.instance_id = null;
      return send(res, 200, { ok: true });
    }
  }
  if (seg[0] === 'feedback' && seg[1]) {
    const f = S.feedback.get(seg[1]);
    if (!f) return notFound(res, `Feedback ${seg[1]}`);
    if (!seg[2]) return send(res, 200, f);
    if (seg[2] === 'reopen') {
      f.status = 'reopened';
      f.history.push({ ts: now(), status: 'reopened', note: 'Reopened by user', actor: 'user' });
      f.updated_at = now();
      emit('feedback.updated', { feedback: f }, f.case_id);
      return send(res, 200, f);
    }
    if (seg[2] === 'triage') {
      const ALLOWED = ['received', 'triaged', 'queued', 'in_progress', 'ready_to_retest', 'resolved', 'reopened'];
      let status = body.status ?? 'triaged';
      if (!ALLOWED.includes(status)) return err(res, 400, 'invalid_status', `“${status}” is not a feedback status.`, 'This feedback was not changed.', `Choose one of: ${ALLOWED.join(', ')}.`);
      if (body.create_work) {
        // mirrors the controller: a linked deliverable is added to the plan, the plan is revised, the feedback is queued
        const plan = S.plans.get(f.case_id);
        const n = [...plan.items.values()].filter((x) => x.item_id.startsWith('FB')).length + 1;
        const it = planItem({ item_id: `FB${n}`, kind: 'deliverable', title: (f.classification === 'bug' ? 'Fix: ' : 'Change: ') + f.comment.slice(0, 70), outcome: f.comment, sort_order: 50 + n, acceptance: [{ id: `FB${n}.A`, command: `comparison for ${f.target_kind} ${f.target_id} passes on a new candidate`, status: 'untested' }] });
        plan.items.set(it.item_id, it);
        emit('plan.item', { item: it }, f.case_id);
        f.linked_items = [...(f.linked_items ?? []), it.item_id];
        revise(f.case_id, `feedback ${f.feedback_id} converted to plan work (${f.classification})`, {}, [`${it.item_id} added`]);
        status = 'queued';
      }
      f.status = status;
      f.history.push({ ts: now(), status: f.status, note: body.note ?? '', actor: 'user' });
      f.updated_at = now();
      emit('feedback.updated', { feedback: f }, f.case_id);
      return send(res, 200, f);
    }
  }
  if (p === '/connections' && m === 'GET') return send(res, 200, [...S.connections.values()]);
  if (p === '/connections' && m === 'POST') {
    if (!body.provider || !body.label) return err(res, 400, 'invalid_connection', 'Provider and name are required.', 'The connection was not saved.', 'Fill in provider and name.');
    const { api_key: key, ...rest } = body;
    const c = { connection_id: id('conn'), endpoint: '', models: [], capabilities: {}, limits: body.auth_mode === 'subscription_handoff' ? { text: 'Limits are set by the external client (demo).' } : {}, state: 'unprobed', last_probe: null, created_at: now(), ...rest, secret_ref: key ? 'keyring:demo' : null };
    S.connections.set(c.connection_id, c);
    return send(res, 200, c);
  }
  if (seg[0] === 'connections' && seg[1]) {
    const c = S.connections.get(seg[1]);
    if (!c) return notFound(res, `Connection ${seg[1]}`);
    if (m === 'DELETE') {
      S.connections.delete(seg[1]);
      return send(res, 200, { ok: true });
    }
    if (seg[2] === 'probe') {
      c.state = c.auth_mode === 'subscription_handoff' ? 'usable' : c.endpoint.includes('127.0.0.1') ? 'unreachable' : 'ok';
      c.last_probe = now();
      if (!c.models.length) c.models = ['demo-model'];
      return send(res, 200, { ...c, probe: { latency_ms: 120 } });
    }
  }
  if (p === '/ai/ladder' && m === 'GET') return send(res, 200, currentLadder());
  if (seg[0] === 'ai' && seg[1] === 'ladder' && seg[2] === 'preset' && m === 'POST') {
    const ok = ['local_first', 'cloud_first', 'all_local', 'all_cloud', 'no_ai'];
    if (!ok.includes(body.preset)) return err(res, 400, 'unknown_preset', `Unknown preset ${body.preset}.`, 'No ladder changed.', 'Choose one of the listed presets.');
    const { out, warnings } = presetPairs(body.preset);
    if (body.apply) {
      for (const t of AI_TASK_IDS) S.ladder[t] = { entries: out[t], rationale: `preset:${body.preset}` };
      S.ladderRev += 1;
      emit('settings.updated', { ladder: S.ladderRev });
    }
    return send(res, 200, { preset: body.preset, applied: !!body.apply, config_revision: S.ladderRev, tasks: Object.fromEntries(AI_TASK_IDS.map((t) => [t, { entries: ladderEntries(out[t]) }])), warnings });
  }
  if (seg[0] === 'ai' && seg[1] === 'ladder' && seg[2] && m === 'PUT') {
    if (!AI_TASK_IDS.includes(seg[2])) return notFound(res, `Task ${seg[2]}`);
    const entries = Array.isArray(body.entries) ? body.entries.filter((e) => e && e.connection_id && e.model).map((e) => ({ connection_id: e.connection_id, model: e.model })) : [];
    for (const e of entries) if (!S.connections.has(e.connection_id)) return err(res, 400, 'unknown_connection', `Connection ${e.connection_id} does not exist.`, 'The ladder was not saved.', 'Pick a listed connection.');
    S.ladder[seg[2]] = { entries, rationale: 'user' };
    S.ladderRev += 1;
    return send(res, 200, { config_revision: S.ladderRev, entries: ladderEntries(entries) });
  }
  if (p === '/ai/models' && m === 'GET') {
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const task = url.searchParams.get('task');
    return send(res, 200, MODELS.filter((x) => (!q || `${x.model} ${S.connections.get(x.connection_id)?.label ?? ''}`.toLowerCase().includes(q)) && (task !== 'visual_review' || x.capabilities?.vision)).map((x) => catalogEntry(x)));
  }
  if (p === '/routes') return send(res, 200, [...S.routes.values()]);
  if (seg[0] === 'routes' && seg[1] && m === 'PUT') {
    const r = { task: seg[1], primary_connection: body.primary_connection ?? null, primary_model: body.primary_model ?? null, fallbacks: body.fallbacks ?? [], updated_at: now() };
    S.routes.set(seg[1], r);
    return send(res, 200, r);
  }
  if (p === '/budgets') return send(res, 200, [...S.budgets.values()]);
  if (p === '/ai/calls') {
    const cid = url.searchParams.get('case_id');
    return send(res, 200, S.aiCalls.filter((x) => !cid || x.case_id === cid));
  }
  if (p === '/knowledge') return send(res, 200, [...S.knowledge.values()]);
  if (seg[0] === 'knowledge' && seg[1]) {
    const k = S.knowledge.get(seg[1]);
    if (!k) return notFound(res, `Knowledge ${seg[1]}`);
    if (!seg[2]) return send(res, 200, k);
    if (seg[2] === 'validate') {
      k.state = 'validating';
      k.updated_at = now();
      emit('knowledge.updated', { knowledge_id: k.knowledge_id, state: k.state });
      setTimeout(() => {
        k.state = 'promoted';
        k.regression = { cases: [{ id: 'fixture-pe-cli', verdict: 'pass' }, { id: 'fixture-web', verdict: 'pass' }] };
        k.updated_at = now();
        emit('knowledge.updated', { knowledge_id: k.knowledge_id, state: k.state });
      }, 1500);
      return send(res, 200, k);
    }
    if (seg[2] === 'rollback') {
      if (k.state !== 'promoted') return err(res, 409, 'not_promoted', 'Only promoted knowledge can be rolled back.', 'Nothing changed.', 'Pick a promoted entry.');
      k.state = 'rolled_back';
      k.updated_at = now();
      emit('knowledge.updated', { knowledge_id: k.knowledge_id, state: k.state });
      return send(res, 200, k);
    }
  }
  if (p === '/settings' && m === 'GET') return send(res, 200, S.settings);
  if (p === '/settings' && m === 'PUT') {
    const conc = body?.concurrency?.max_concurrent_jobs;
    if (conc != null && (!Number.isInteger(conc) || conc < 1 || conc > 64)) return err(res, 400, 'invalid_setting', 'Max concurrent jobs must be 1–64.', 'Settings were not saved.', 'Enter a value from 1 to 64.');
    S.settings = body;
    return send(res, 200, S.settings);
  }
  if (p === '/isolation') return send(res, 200, { platform: 'win32', network_default: 'open: not blocked', default_mode: 'low', network_blocking: 'not available without administrator rights', modes: { low: { available: true } } });
  if (p === '/hermes/status') return send(res, 200, S.hermes);
  if (p === '/hermes/pair' && m === 'POST') {
    S.hermes = { ...S.hermes, paired: true, state: 'paired', profile_path: body.profile_path ?? '~/.hermes/profiles/default (demo)' };
    S.hermes.diagnostics = [{ check: 'Hermes profile', ok: true, detail: 'paired (demo)' }, ...S.hermes.diagnostics.filter((d) => d.check !== 'Hermes profile')];
    return send(res, 200, S.hermes);
  }
  if (p === '/hermes/register_mcp' && m === 'POST') {
    if (!body.dry_run) S.hermes = { ...S.hermes, mcp_registered: true };
    return send(res, 200, { dry_run: !!body.dry_run, would_write: '~/.hermes/config.yaml (demo)', entry: { name: 'rebuild-studio', command: 'rebuild-studio-mcp', args: ['--stdio'] } });
  }
  return notFound(res, `Endpoint ${m} ${p}`);
}

const MOCK_CONSENT = {};
const MOCK_SCENARIOS = {};

function createCase(res, b) {
  const missing = ['name', 'source_root', 'output_root', 'target_language', 'output_type'].filter((k) => !b[k]);
  if (missing.length) return err(res, 400, 'invalid_case', `Missing fields: ${missing.join(', ')}.`, 'The project was not created.', 'Fill in the missing fields.');
  if (/missing|does-not-exist/i.test(b.source_root)) return err(res, 400, 'source_not_found', `The source folder ${b.source_root} does not exist.`, 'The project was not created; nothing was written.', 'Choose an existing folder that contains the original program.');
  const combo = capabilities().output_combinations.find((x) => x.target_language === b.target_language && x.output_type === b.output_type);
  if (combo?.state === 'unsupported') return err(res, 400, 'unsupported_combination', `${b.output_type} output is not supported for ${b.target_language}.`, 'The project was not created.', 'Choose a supported output type.');
  const cid = id('case');
  const c = { case_id: cid, name: b.name, source_root: b.source_root, output_root: b.output_root, target_language: b.target_language, output_type: b.output_type, ai_policy: b.ai_policy ?? { mode: 'no_ai' }, launch_profile: b.launch_profile ?? { execute_original: false }, status: 'created', created_at: now(), updated_at: now(), app_version: '0.1.0-mock', settings: b.settings ?? {}, started_at: null };
  S.cases.set(cid, c);
  emit('case.created', { case: c }, cid);
  const items = [planItem({ item_id: 'M1', title: 'Inventory the original', outcome: 'Every file classified', sort_order: 1 })];
  S.plans.set(cid, { revision: 1, items: new Map(items.map((i) => [i.item_id, i])), unknown_scope: ['Everything until discovery runs'], progress: { analysis: { done: 0, total: null } }, eta: null, revisions: [{ revision: 1, reason: 'Initial plan', created_at: now() }] });
  emit('plan.revised', { revision: 1, reason: 'Initial plan', progress: { analysis: { done: 0, total: null } }, eta: null, items }, cid);
  return send(res, 200, c);
}

function createFeedback(res, cid, b) {
  if (!b.comment || !String(b.comment).trim()) return err(res, 400, 'empty_comment', 'Feedback needs a comment.', 'The feedback was not saved.', 'Describe what you saw and submit again.');
  if (!b.target_id) return err(res, 400, 'missing_target', 'Feedback needs a target.', 'The feedback was not saved.', 'Choose what the feedback is about.');
  // same constraint as the controller's FeedbackCreate model (FastAPI 422 with a `detail` list)
  if (b.priority !== undefined && !['low', 'medium', 'high', 'critical'].includes(b.priority)) return send(res, 422, { detail: [{ loc: ['body', 'priority'], msg: "String should match pattern '^(low|medium|high|critical)$'", type: 'string_pattern_mismatch' }] });
  const cand = b.candidate_id ? S.candidates.get(b.candidate_id) : null;
  const attachments = (b.attachments ?? []).map((a) => {
    const buf = Buffer.from(String(a.bytes_b64 ?? ''), 'base64');
    return { name: a.name, size: buf.length, sha256: sha(buf) };
  });
  const f = {
    feedback_id: id('fb'), case_id: cid, target_kind: b.target_kind, target_id: b.target_id, candidate_id: b.candidate_id ?? null, plan_revision: S.plans.get(cid).revision,
    classification: b.classification ?? 'bug', priority: b.priority ?? 'medium', comment: b.comment, expected: b.expected ?? '', actual: b.actual ?? '', attachments,
    context: { build_hash: cand?.build_hash ?? null }, status: 'received', linked_items: [], history: [{ ts: now(), status: 'received', note: 'Saved', actor: 'user' }], created_at: now(), updated_at: now(),
  };
  S.feedback.set(f.feedback_id, f);
  emit('feedback.created', { feedback: f }, cid);
  return send(res, 200, f);
}

function capabilities() {
  const out = [];
  const T = ['rust', 'rust_bevy', 'web', 'auto'];
  const O = ['exe', 'installer', 'portable', 'web', 'pwa'];
  for (const t of T)
    for (const o of O) {
      let state = 'supported';
      let reason;
      if ((t === 'rust' || t === 'rust_bevy') && (o === 'web' || o === 'pwa')) {
        state = t === 'rust_bevy' && o === 'web' ? 'experimental' : 'unsupported';
        reason = state === 'unsupported' ? 'native Rust targets are not packaged as web apps' : 'Bevy WebAssembly builds are experimental';
      }
      if (t === 'web' && (o === 'exe' || o === 'installer')) {
        state = 'unsupported';
        reason = 'web targets produce a site or PWA, not a Windows executable';
      }
      if (o === 'installer' && state === 'supported') {
        state = 'handoff';
        reason = 'NSIS installer is built on the Windows CI runner';
      }
      out.push({ target_language: t, output_type: o, state, ...(reason ? { reason } : {}) });
    }
  return { output_combinations: out };
}

function doctor(smoke) {
  const t = (name, availability, o = {}) => ({ name, availability, path: null, version: null, detail: '', prerequisites: [], license: '', source: '', pinned: '', integrity: '', integration: 'cli', ...o });
  const backends = [
    { backend_id: 'rizin', title: 'Rizin (native code)', formats: ['pe', 'elf'], platforms: ['windows', 'linux'], profiles: ['native'], tools: [t('rizin', smoke ? 'usable' : 'installed', { version: '0.9.1', license: 'LGPL-3.0', pinned: 'v0.9.1', path: '/opt/rebuild-tools/rizin/bin/rizin', size_bytes: 42_000_000 })], resources: {}, tested_support: [], experimental: false, availability: smoke ? 'usable' : 'installed' },
    { backend_id: 'rz_ghidra', title: 'rz-ghidra decompiler', formats: ['pe', 'elf'], platforms: ['windows', 'linux'], profiles: ['native'], tools: [t('rz-ghidra', 'missing', { license: 'LGPL-3.0', prerequisites: ['rizin dev headers', 'cmake'], detail: 'plugin not built on this host', pinned: 'v0.9.0' })], resources: {}, tested_support: [], experimental: false, availability: 'missing' },
    { backend_id: 'ilspy', title: 'ILSpy (.NET)', formats: ['dotnet'], platforms: ['windows', 'linux'], profiles: ['managed'], tools: [t('ilspycmd', 'verified', { version: '9.1.0.7988', license: 'MIT', prerequisites: ['.NET 8 runtime'], size_bytes: 18_500_000 })], resources: {}, tested_support: ['fixture-dotnet'], experimental: false, availability: 'verified' },
    { backend_id: 'gdre', title: 'GDRE tools (Godot)', formats: ['godot_pck'], platforms: ['windows', 'linux'], profiles: ['godot'], tools: [t('gdre_tools', 'detected', { version: '2.7.0', license: 'MIT', size_bytes: 95_000_000 })], resources: {}, tested_support: [], experimental: false, availability: 'detected' },
    { backend_id: 'il2cpp', title: 'Cpp2IL (Unity IL2CPP)', formats: ['il2cpp'], platforms: ['windows'], profiles: ['unity'], tools: [t('cpp2il', 'missing', { license: 'MIT', detail: 'experimental/unverified' })], resources: {}, tested_support: [], experimental: true, availability: 'missing' },
  ];
  const summary = {};
  for (const b of backends) summary[b.availability] = (summary[b.availability] ?? 0) + 1;
  return { backends, summary };
}

function mockControl(req, res, url) {
  const p = url.pathname;
  const remote = req.socket.remoteAddress ?? '';
  if (!/^(::1|127\.|::ffff:127\.)/.test(remote)) return send(res, 403, 'loopback only');
  if (p.startsWith('/__mock/artifacts/')) {
    if (req.headers.authorization !== `Bearer ${TOKEN}`) return err(res, 401, 'unauthorized', 'Missing token', 'artifact', 'retry');
    const name = p.slice('/__mock/artifacts/'.length);
    return ART[name] ? send(res, 200, ART[name], { 'Content-Type': 'image/svg+xml' }) : send(res, 404, 'no artifact');
  }
  if (p.startsWith('/__mock/preview/')) {
    const v = p.split('/').pop();
    return send(res, 200, `<!doctype html><meta charset="utf-8"><title>Preview ${v} (mock)</title><body style="font-family:system-ui;padding:24px"><p style="background:#efe7fb;color:#6b3fb0;padding:8px 12px;border-radius:6px;font-weight:600">Demo data – mock controller</p><h1>Inventory Tool — preview ${v}</h1><button>List</button><ul><li>Widget A — 3</li><li>Widget B — 12</li></ul></body>`, { 'Content-Type': 'text/html; charset=utf-8' });
  }
  if (req.method === 'GET' && p === '/__mock/state') return send(res, 200, { step: S.step, steps: STEPS.length, seq: S.seq, sockets: sockets.size, stalled: Date.now() < S.stalledUntil, disconnected: Date.now() < S.disconnectedUntil, feedback: S.feedback.size });
  if (req.method !== 'POST') return send(res, 405, 'POST');
  return readBody(req).then((b) => {
    if (p === '/__mock/step') {
      const n = Math.max(1, Number(b.count ?? 1));
      let done = 0;
      for (let i = 0; i < n; i++) if (step()) done++;
      return send(res, 200, { step: S.step, advanced: done });
    }
    if (p === '/__mock/stall') {
      S.stalledUntil = Date.now() + Number(b.seconds ?? HEARTBEAT * 3) * 1000;
      return send(res, 200, { stalledUntil: S.stalledUntil });
    }
    if (p === '/__mock/disconnect') {
      disconnect(Number(b.seconds ?? 3));
      return send(res, 200, { disconnectedUntil: S.disconnectedUntil });
    }
    if (p === '/__mock/emit') {
      const ev = emit(String(b.kind ?? 'job.log'), b.payload ?? {}, b.case_id ?? DEMO, b.job_id ?? null);
      return send(res, 200, ev);
    }
    if (p === '/__mock/fail') {
      S.faults.push({ method: String(b.method ?? 'POST'), path: String(b.path), status: Number(b.status ?? 500), body: b.body ?? { error: { code: 'injected', message: 'Injected failure' } }, times: Number(b.times ?? 1) });
      return send(res, 200, { faults: S.faults.length });
    }
    if (p === '/__mock/burst') {
      // Emits events (assigning seqs, stored for replay) without broadcasting, then sends frames to every socket in
      // `order` (indices into `events`, repeats allowed) to exercise duplicate and out-of-order delivery.
      const evs = (b.events ?? []).map((e) => {
        const ev = { seq: ++S.seq, ts: now(), case_id: e.case_id ?? DEMO, job_id: e.job_id ?? null, kind: String(e.kind), payload: e.payload ?? {} };
        S.events.push(ev);
        // keep REST snapshots consistent with the burst so refreshes agree with the event stream
        if (ev.kind === 'job.created' && ev.payload.job) S.jobs.set(ev.payload.job.job_id, { ...ev.payload.job });
        const j = S.jobs.get(ev.payload.job_id ?? ev.job_id);
        if (j && ev.kind === 'job.started') j.state = 'running';
        if (j && ['job.completed', 'job.failed', 'job.cancelled'].includes(ev.kind)) j.state = ev.kind.slice(4);
        return ev;
      });
      const order = Array.isArray(b.order) ? b.order : evs.map((_, i) => i);
      for (const i of order) for (const ws of sockets) if (ws.readyState === 1 && evs[i]) ws.send(JSON.stringify(evs[i]));
      return send(res, 200, { seqs: evs.map((e) => e.seq), sent: order.length });
    }
    if (p === '/__mock/reset') {
      if (S.autoTimer) clearInterval(S.autoTimer);
      seed();
      for (const ws of sockets) ws.terminate();
      for (let i = 0; i < Number(b.preload ?? 0); i++) step();
      return send(res, 200, { ok: true, step: S.step });
    }
    return send(res, 404, 'unknown control');
  });
}

// ------------------------------------------------------------------------------------------------ server
seed();
for (let i = 0; i < PRELOAD; i++) step();

const server = http.createServer((req, res) => {
  handle(req, res).catch((e) => {
    console.error(e);
    if (!res.headersSent) err(res, 500, 'internal', String(e?.message ?? e), 'This request.', 'Retry; check the mock server console.');
  });
});
const wss = new WebSocketServer({ noServer: true });
server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  if (url.pathname !== '/ws' || url.searchParams.get('token') !== TOKEN || !originAllowed(req.headers.origin) || Date.now() < S.disconnectedUntil) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    sockets.add(ws);
    ws.on('close', () => sockets.delete(ws));
    ws.on('error', () => sockets.delete(ws));
    const since = Number(url.searchParams.get('since') ?? 0);
    for (const ev of S.events) if (ev.seq > since) ws.send(JSON.stringify(ev));
  });
});

setInterval(() => {
  if (Date.now() < S.stalledUntil) return;
  const active = [...S.jobs.values()].filter((j) => j.state === 'running').length;
  emit('controller.heartbeat', { worker: 'mock', active });
  for (const j of S.jobs.values()) if (j.state === 'running') j.heartbeat_at = Date.now() / 1000;
}, HEARTBEAT * 1000).unref?.();

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock-controller] http://127.0.0.1:${PORT}  token=${TOKEN}  heartbeat=${HEARTBEAT}s  auto=${AUTO_MS || 'manual'}${SERVE ? `  serving ${SERVE}` : ''}`);
});
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => process.exit(0));
