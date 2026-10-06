// Plain-language helpers for the AI ladder, plan chips and activity feed (docs/AI_LADDER.md).
import { humanize, parseTime, timeAgo, usd } from './format';
import type {
  AiActivity, Connection, LadderEntry, LadderTask, Locality, ModelAvailability, ModelCapabilities, ModelCatalogItem, ModelPrice, PlanAi, PlanAiModel, PresetResult, WorkOrigin,
} from './types';

export const UNKNOWN_PRICE = 'Unknown price — needs approval';

export const AI_TASKS: { id: string; label: string; sub: string }[] = [
  { id: 'interpretation', label: 'Interpretation', sub: 'Explain decompiled code and data' },
  { id: 'repair', label: 'Repair', sub: 'Propose fixes for failing builds/tests' },
  { id: 'visual_review', label: 'Visual review', sub: 'Describe screenshot differences' },
  { id: 'verification_assist', label: 'Verification assist', sub: 'Suggest scenarios (never verdicts)' },
  { id: 'knowledge', label: 'Knowledge', sub: 'Propose reusable adapters/rules' },
];
export const taskLabel = (id: string | null | undefined) => (id ? AI_TASKS.find((t) => t.id === id)?.label ?? humanize(id) : 'AI');

export const PRESETS: { id: 'local_first' | 'cloud_first' | 'all_local' | 'all_cloud' | 'no_ai'; label: string; sub: string }[] = [
  { id: 'local_first', label: 'Local first', sub: 'Models on this PC first, cloud as fallback' },
  { id: 'cloud_first', label: 'Cloud first', sub: 'Cloud models first, local as fallback' },
  { id: 'all_local', label: 'All local', sub: 'Nothing leaves this PC' },
  { id: 'all_cloud', label: 'All cloud', sub: 'Cloud models only' },
  { id: 'no_ai', label: 'No AI', sub: 'Clear every ladder' },
];

export const isLocalProvider = (c: Pick<Connection, 'provider' | 'endpoint'> | undefined | null) =>
  !!c && (c.provider === 'local' || c.provider === 'local_openai' || /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(c.endpoint ?? ''));

export const localityLabel = (l: Locality | null | undefined) => (l === 'local' ? 'Local' : l === 'cloud' ? 'Cloud' : 'Unknown location');
export const localityHint = (l: Locality | null | undefined) => (l === 'local' ? 'Runs on this PC' : l === 'cloud' ? 'Sent to a cloud provider' : 'Not known where this runs');

export function priceLabel(e: { price?: ModelPrice | null; free?: boolean; locality?: Locality | null }): string {
  if (e.free || (e.locality === 'local' && !e.price?.known) || (e.price?.known && e.price.input_per_mtok === 0 && e.price.output_per_mtok === 0)) return 'Free';
  const p = e.price;
  if (p?.known && (p.input_per_mtok != null || p.output_per_mtok != null)) {
    const f = (n: number | null | undefined) => (n == null ? '?' : `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`);
    return `${f(p.input_per_mtok)} in / ${f(p.output_per_mtok)} out per Mtok`;
  }
  return UNKNOWN_PRICE;
}
export const priceIsUnknown = (e: Parameters<typeof priceLabel>[0]) => priceLabel(e) === UNKNOWN_PRICE;

function contextText(n: number): string {
  if (n < 1000) return String(n);
  const pow2 = (n & (n - 1)) === 0;
  return `${Math.round(pow2 ? n / 1024 : n / 1000)}k`;
}

export function capabilityText(c: ModelCapabilities | null | undefined): string {
  if (!c) return 'Capabilities not reported';
  const parts: string[] = [];
  if (c.vision != null) parts.push(c.vision ? 'Vision' : 'No vision');
  if (c.tools != null) parts.push(c.tools ? 'Tools' : 'No tools');
  if (c.context_window != null) parts.push(`${contextText(c.context_window)} context`);
  return parts.length ? parts.join(' · ') : 'Capabilities not reported';
}

export function availabilityText(a: ModelAvailability | null | undefined, now = Date.now()): { state: string; text: string } {
  if (!a || !a.state || a.state === 'unprobed') return { state: 'unprobed', text: 'Not checked yet' };
  const when = parseTime(a.probed_at ?? null);
  return { state: a.state, text: `${humanize(a.state)}${when != null ? ` · checked ${timeAgo(when, now)}` : ' · never checked'}${a.detail ? ` (${a.detail})` : ''}` };
}

/** `{entries}` or a bare array → entries. */
export function entriesOf(t: LadderTask | LadderEntry[] | undefined | null): LadderEntry[] {
  return Array.isArray(t) ? t : Array.isArray(t?.entries) ? t!.entries : [];
}
export function presetLadders(r: PresetResult): Record<string, LadderEntry[]> {
  const src = r.tasks ?? r.ladders ?? {};
  return Object.fromEntries(Object.entries(src).map(([k, v]) => [k, entriesOf(v)]));
}

export const rungKey = (e: Pick<LadderEntry, 'connection_id' | 'model'>) => `${e.connection_id}::${e.model}`;

export function entryFromCatalog(m: ModelCatalogItem): LadderEntry {
  return { connection_id: m.connection_id, connection_label: m.connection_label, provider: m.provider, model: m.model, locality: m.locality, availability: m.availability, capabilities: m.capabilities, price: m.price, free: m.free };
}

export function entryFromManual(connection: Connection, model: string): LadderEntry {
  const local = isLocalProvider(connection);
  return { connection_id: connection.connection_id, connection_label: connection.label, provider: connection.provider, model, locality: local ? 'local' : 'cloud', availability: null, capabilities: null, price: null, free: local };
}

export const ORIGIN_INFO: Record<WorkOrigin, { label: string; hint: string; tone: string }> = {
  deterministic: { label: 'Deterministic', hint: 'Produced by deterministic tools: the same input always gives the same result, no AI involved.', tone: 'queue' },
  model_proposed: { label: 'Model proposed', hint: 'An AI model proposed this. It is only a proposal until the build and verifier accept it.', tone: 'retest' },
  verifier_decided: { label: 'Verifier decided', hint: 'Decided by the verifier from recorded evidence. AI never writes verdicts.', tone: 'ok' },
};

export function costText(ai: PlanAi): string | null {
  const c = ai.expected_cost;
  if (!c) return null;
  if (c.unknown_price || c.known === false || (c.min_usd == null && c.max_usd == null)) return 'unknown price';
  const lo = c.min_usd ?? c.max_usd ?? 0;
  const hi = c.max_usd ?? lo;
  if (lo === 0 && hi === 0) return 'free';
  if (lo === 0) return `up to ${usd(hi)}`;
  return lo === hi ? `about ${usd(hi)}` : `about ${usd(lo)}–${usd(hi)}`;
}

export const modelText = (m: PlanAiModel | null | undefined) => (m ? `${m.model} (${localityLabel(m.locality)})` : 'no model chosen');

export const OUTCOME_INFO: Record<string, { icon: string; tone: string; label: string }> = {
  ok: { icon: '✓', tone: 'ok', label: 'Succeeded' },
  rate_limit: { icon: '⏱', tone: 'warn', label: 'Rate limited' },
  usage_limit: { icon: '⏱', tone: 'warn', label: 'Usage limit reached' },
  credits_exhausted: { icon: '!', tone: 'warn', label: 'Out of credits' },
  auth_failed: { icon: '✕', tone: 'bad', label: 'Sign-in failed' },
  model_unavailable: { icon: '✕', tone: 'bad', label: 'Model not available' },
  capability_unsupported: { icon: '–', tone: 'muted', label: 'Not supported by this model' },
  unreachable: { icon: '✕', tone: 'bad', label: 'Could not reach it' },
  timeout_not_sent: { icon: '✕', tone: 'bad', label: 'Could not connect' },
  unavailable: { icon: '✕', tone: 'bad', label: 'Service unavailable' },
  timeout: { icon: '?', tone: 'warn', label: 'Timed out; cost assumed spent' },
  ambiguous: { icon: '?', tone: 'warn', label: 'Unclear result; cost assumed spent' },
  approval_required: { icon: '!', tone: 'warn', label: 'Needs your approval (unknown price)' },
  budget_exhausted: { icon: '!', tone: 'warn', label: 'Budget would be exceeded' },
  policy_skipped: { icon: '–', tone: 'muted', label: 'Skipped by project policy' },
};
export const outcomeInfo = (o: string | null | undefined) => (o ? OUTCOME_INFO[o] ?? { icon: '·', tone: 'outline', label: humanize(o) } : null);

export function activityCost(a: AiActivity): string | null {
  if (a.cost_usd == null && a.cost_known == null) return null;
  if (a.locality === 'local' && (a.cost_usd == null || a.cost_usd === 0)) return 'Free (local)';
  if (a.cost_known === false || a.cost_usd == null) return 'cost unknown';
  return usd(a.cost_usd);
}
export function activityTokens(a: AiActivity): string | null {
  if (a.tokens_in == null && a.tokens_out == null) return null;
  return `${(a.tokens_in ?? 0).toLocaleString('en-US')} in / ${(a.tokens_out ?? 0).toLocaleString('en-US')} out tokens`;
}

export function activityKey(a: AiActivity): string {
  return `${a.at}|${a.kind ?? ''}|${a.job_id ?? ''}|${a.text}`;
}
