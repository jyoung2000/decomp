// R9 granular AI control (docs/AI_LADDER.md section 9): per-rung failure rules, provider cooldowns, dry-run route test and the
// JeV advisor card. Kept in its own module so the shared api.ts / types.ts stay untouched.
import type { Api } from './api';
import type { LadderEntry } from './types';

export type RuleAction = 'next' | 'wait' | 'stop';
export interface RungRule {
  action: RuleAction;
  wait_minutes?: number;
  max_tries?: number;
}
export type RungRules = Record<string, RungRule>;
export interface FailureKind {
  kind: string;
  label: string;
  wait_allowed: boolean;
}
export interface RulesMeta {
  failure_kinds: FailureKind[];
  actions: RuleAction[];
  max_wait_minutes?: number;
  max_tries?: number;
}
export interface Cooldown {
  connection_id: string;
  connection_label?: string;
  provider?: string;
  outcome: string;
  reason?: string;
  set_at?: string;
  until?: string;
  scope?: 'connection' | 'free_models';
  text?: string;
}
export interface CooldownsView {
  settings: { cooldown_minutes: Record<string, number> };
  cooldowns: Cooldown[];
}
/** A ladder entry as GET /ai/ladder returns it since R9. */
export type RuleEntry = LadderEntry & { rules?: RungRules; cooldown?: (Cooldown & { text?: string }) | null };

export interface RouteTestRung extends RuleEntry {
  status: 'would_answer' | 'standby' | 'skipped';
  outcome: string | null;
  reason: string;
  rules_text?: string[];
}
export interface RouteTestResult {
  task: string;
  config_revision: number;
  answer: number | null;
  rungs: RouteTestRung[];
  chain: string;
  summary: string;
  tokens_spent: number;
  sent: boolean;
  advisor?: string;
}

export interface JevDecision {
  kind: 'order' | 'reassess';
  task?: string;
  choice?: string;
  confidence?: number;
  model?: string;
  source: string;
  reason?: string;
  suggested_first?: string;
  at?: string;
}
export interface JevStatus {
  enabled: boolean;
  has_key: boolean;
  key_source: 'entered' | 'jev_install' | null;
  model: string;
  endpoint: string;
  monthly_cap_usd: number;
  setup_cap_usd?: number;
  price?: { input_per_mtok: number; source?: string };
  month?: { spent_usd: number; limit_usd: number; reserved_usd?: number } | null;
  breaker?: { state: 'closed' | 'open' | 'half_open'; failures: number; open_until?: number | null };
  offline_reason: null | 'off' | 'no_key' | 'breaker_open';
  min_confidence?: number;
  jev_install?: { key_file_found: boolean; path: string };
  last_decisions: JevDecision[];
}
export interface JevTest {
  ok: boolean;
  reason: string;
  message?: string;
  model?: string;
  spent_usd?: number | null;
}

/** Default kinds (used until GET /ai/ladder's rules_meta arrives, and by the mock). */
export const DEFAULT_FAILURE_KINDS: FailureKind[] = [
  { kind: 'credits_exhausted', label: 'runs out of credits', wait_allowed: true },
  { kind: 'usage_limit', label: 'hits its usage limit', wait_allowed: true },
  { kind: 'rate_limit', label: 'is rate-limited', wait_allowed: true },
  { kind: 'auth_failed', label: 'rejects the key', wait_allowed: false },
  { kind: 'model_unavailable', label: 'does not have the model', wait_allowed: false },
  { kind: 'capability_unsupported', label: 'cannot take this input', wait_allowed: false },
  { kind: 'context_exceeded', label: 'the request does not fit its context window', wait_allowed: false },
  { kind: 'unreachable', label: 'cannot be reached', wait_allowed: true },
  { kind: 'unavailable', label: 'has a server error', wait_allowed: true },
];

export const ACTION_LABEL: Record<RuleAction, string> = { next: 'Use the next rung', wait: 'Wait and retry', stop: 'Stop and ask me' };

/** Rules without the defaults ("next"), so an untouched rung compares equal to {}. */
export function cleanRules(r: RungRules | undefined | null): RungRules {
  const out: RungRules = {};
  for (const [k, v] of Object.entries(r ?? {})) {
    if (!v || v.action === 'next') continue;
    out[k] = v.action === 'wait' ? { action: 'wait', wait_minutes: v.wait_minutes ?? 5, max_tries: v.max_tries ?? 3 } : { action: 'stop' };
  }
  return out;
}
export const rulesSig = (r: RungRules | undefined | null) => JSON.stringify(Object.entries(cleanRules(r)).sort(([a], [b]) => a.localeCompare(b)));

export function ruleSummary(r: RungRules | undefined | null, kinds: FailureKind[] = DEFAULT_FAILURE_KINDS): string {
  const c = cleanRules(r);
  const parts = Object.entries(c).map(([k, v]) => {
    const what = kinds.find((x) => x.kind === k)?.label ?? k.replace(/_/g, ' ');
    return v.action === 'stop' ? `if it ${what}: stop and ask me` : `if it ${what}: wait ${v.wait_minutes} min, up to ${v.max_tries}×`;
  });
  return parts.length ? parts.join('; ') : 'On any failure: use the next rung';
}

export const aiControl = (api: Api) => ({
  putLadder: (task: string, entries: { connection_id: string; model: string; rules?: RungRules }[]) => api.put<unknown>(`/ai/ladder/${encodeURIComponent(task)}`, { entries }),
  routeTest: (task: string, caseId?: string) => api.post<RouteTestResult>('/ai/route/test', { task, ...(caseId ? { case_id: caseId } : {}) }),
  cooldowns: () => api.get<CooldownsView>('/ai/cooldowns'),
  clearCooldown: (connectionId: string) => api.del<{ cleared: boolean }>(`/ai/cooldowns/${encodeURIComponent(connectionId)}`),
  jev: () => api.get<JevStatus>('/ai/jev'),
  putJev: (b: { enabled?: boolean; monthly_cap_usd?: number }) => api.put<JevStatus>('/ai/jev', b),
  putJevKey: (key: string | null) => api.put<JevStatus>('/ai/jev/key', { key }),
  importJevKey: () => api.post<JevStatus & { imported?: boolean; path?: string }>('/ai/jev/key/import'),
  testJev: () => api.post<JevTest>('/ai/jev/test'),
});

export function offlineText(s: Pick<JevStatus, 'offline_reason' | 'breaker'>): string {
  switch (s.offline_reason) {
    case 'off':
      return 'Off: your ladder order is used as is.';
    case 'no_key':
      return 'No key yet: your ladder order is used as is.';
    case 'breaker_open':
      return 'Paused after repeated JeV failures (retries in about a minute): your ladder order is used meanwhile.';
    default:
      return 'On: JeV may re-order your usable rungs and advise retry / switch / stop between repair attempts. It never adds a model.';
  }
}
