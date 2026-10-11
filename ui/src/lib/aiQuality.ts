// R5 AI-assisted RE quality (docs/AI_LADDER.md section 10): the model scoreboard, AI naming of decompiled functions and the review of
// model-proposed annotations. Kept in its own module so the shared api.ts / types.ts stay untouched.
import type { Api } from './api';

export interface TaskScore {
  runs: number;
  ok: number;
  compiled: number;
  verified: number;
  truth_checked?: number;
  truth_match?: number;
  cost_usd?: number;
  cost_known?: boolean;
  sources?: string[];
  median_latency_ms?: number | null;
  ok_rate?: number | null;
  compile_rate?: number | null;
  verify_rate?: number | null;
  truth_rate?: number | null;
  score?: number;
}
export interface ModelScore {
  model: string;
  provider?: string | null;
  locality?: string | null;
  tasks: Record<string, TaskScore>;
  ever_compiled: boolean;
  code_runs: number;
}
export interface Scoreboard {
  models: ModelScore[];
  rows: number;
  recent?: Record<string, unknown>[];
}

export type ProposalStatus = 'proposed' | 'accepted' | 'confirmed';
export interface Proposal {
  kind: 'function' | 'global' | 'local' | 'comment';
  key: string;
  local?: string | null;
  value?: string | null;
  status: ProposalStatus;
  label: string;
  model?: string | null;
  task?: string | null;
  at?: string | null;
  analysis_name?: string | null;
  replaced?: { name?: string; text?: string } | null;
  confirmed_by?: string | null;
  rationale?: string | null;
}
export interface AnnotationHistoryItem {
  evidence_id: string;
  revision: number;
  change?: { action?: string; decision?: string; kind?: string; new?: string; addr?: string; author?: string; at?: string; label?: string; to_revision?: number } | null;
}
export interface AnnotationsView {
  revision: number;
  proposals: Proposal[];
  open_proposals: number;
  history?: AnnotationHistoryItem[];
  label?: string;
}
export interface NamingStart {
  job: { job_id: string; state?: string };
  route?: { model: string; label?: string; locality?: string }[];
  note?: string;
}

const enc = encodeURIComponent;
const mod = (c: string, m: string) => `/cases/${enc(c)}/modules/${enc(m)}`;

export const aiQuality = (api: Api) => ({
  scoreboard: () => api.get<Scoreboard>('/ai/scoreboard'),
  annotations: (caseId: string, moduleId: string) => api.get<AnnotationsView>(`${mod(caseId, moduleId)}/annotations?history=20`),
  review: (caseId: string, moduleId: string, p: Pick<Proposal, 'kind' | 'key' | 'local'>, decision: 'accept' | 'reject') =>
    api.post<{ revision: number; proposals: Proposal[] }>(`${mod(caseId, moduleId)}/annotations/review`, { kind: p.kind, key: p.key, local: p.local ?? null, decision }),
  rollback: (caseId: string, moduleId: string, revision: number) => api.post<{ revision: number }>(`${mod(caseId, moduleId)}/annotations/rollback`, { revision }),
  startNaming: (caseId: string, moduleId: string, limit: number) => api.post<NamingStart>(`${mod(caseId, moduleId)}/ai/naming`, { limit }),
});

export const CODE_TASKS = ['implementation', 'repair'];

/** One plain line per task: "3 attempts, 1 compiled, 0 verified, median 412 s". */
export function scoreLine(task: string, t: TaskScore): string {
  const lat = t.median_latency_ms != null ? `, median ${(t.median_latency_ms / 1000).toFixed(t.median_latency_ms < 10_000 ? 1 : 0)} s` : '';
  if (CODE_TASKS.includes(task)) return `${t.runs} attempt${t.runs === 1 ? '' : 's'}, ${t.compiled} compiled, ${t.verified} verified${lat}`;
  const truth = t.truth_checked ? `, ${t.truth_match ?? 0}/${t.truth_checked} matched the real names` : '';
  return `${t.runs} run${t.runs === 1 ? '' : 's'}, ${t.ok} usable${truth}${lat}`;
}

export function proposalTitle(p: Proposal): string {
  if (p.kind === 'function') return `Function ${p.key}`;
  if (p.kind === 'local') return `Local ${p.local} in ${p.key}`;
  if (p.kind === 'global') return `Global ${p.key}`;
  return `Comment at ${p.key}`;
}
