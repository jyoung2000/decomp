import { useState } from 'react';
import { AI_TASKS, entryFromManual, isLocalProvider, rungKey, taskLabel } from '../../lib/ai';
import type { AiMode, AiPolicy, Connection, LadderEntry, PolicyLocality } from '../../lib/types';
import { LadderList } from './LadderList';
import { ModelPicker } from './ModelPicker';

export interface PolicyForm {
  mode: AiMode;
  locality: PolicyLocality;
  budget_usd: string;
  approve_unknown_pricing: boolean;
  overrides: Record<string, LadderEntry[]>;
}

export const emptyPolicyForm = (): PolicyForm => ({ mode: 'no_ai', locality: 'any', budget_usd: '', approve_unknown_pricing: false, overrides: {} });

export function policyToForm(p: AiPolicy | null | undefined, connections: Connection[] = []): PolicyForm {
  const overrides: Record<string, LadderEntry[]> = {};
  for (const [task, es] of Object.entries(p?.ladder_overrides ?? {}))
    overrides[task] = es.map((e) => {
      const c = connections.find((x) => x.connection_id === e.connection_id);
      return c ? entryFromManual(c, e.model) : { connection_id: e.connection_id, model: e.model };
    });
  return {
    mode: p?.mode ?? 'no_ai',
    locality: p?.locality ?? 'any',
    budget_usd: p?.budget_usd != null ? String(p.budget_usd) : '',
    approve_unknown_pricing: !!p?.approve_unknown_pricing,
    overrides,
  };
}

/** Comparable signature of what would be saved (ignores display-only fields of ladder rows). */
export const formSig = (f: PolicyForm) =>
  JSON.stringify({ m: f.mode === 'no_ai' ? 'no_ai' : f.mode, l: f.mode === 'no_ai' ? '' : f.locality, b: f.mode === 'no_ai' ? '' : f.budget_usd.trim(), a: f.mode === 'no_ai' ? false : f.approve_unknown_pricing, o: f.mode === 'custom' ? Object.entries(f.overrides).filter(([, v]) => v.length).map(([k, v]) => [k, v.map(rungKey)]) : [] });

export const isInherit = (m: AiMode) => m === 'inherit' || m === 'assist_on_failure' || m === 'assisted';

export function budgetError(f: PolicyForm): string | null {
  if (f.mode === 'no_ai' || f.locality === 'local_only') return null;
  const b = Number(f.budget_usd);
  if (f.budget_usd.trim() === '' || !Number.isFinite(b) || b <= 0) return 'Enter a budget in USD (for example 0.50) so spending stays bounded, or choose “Local only”, which costs nothing.';
  if (b > 1000) return 'The budget is above $1000. Enter a smaller amount.';
  return null;
}

export function formToPolicy(f: PolicyForm, base?: AiPolicy | null): AiPolicy {
  if (f.mode === 'no_ai') return { ...(base ?? {}), mode: 'no_ai' };
  const budget = f.budget_usd.trim() === '' ? null : Number(f.budget_usd);
  const p: AiPolicy = { ...(base ?? {}), mode: f.mode, locality: f.locality, budget_usd: budget, approve_unknown_pricing: f.approve_unknown_pricing };
  if (f.mode === 'custom') p.ladder_overrides = Object.fromEntries(Object.entries(f.overrides).filter(([, es]) => es.length).map(([t, es]) => [t, es.map((e) => ({ connection_id: e.connection_id, model: e.model }))]));
  else p.ladder_overrides = {}; // the controller merges keys: an empty object clears stale overrides
  return p;
}

/** What "No AI" means, in plain language. Shown wherever the choice is made. */
export function NoAiExplainer({ id }: { id?: string }) {
  return (
    <div className="callout info" id={id} role="note" data-testid="no-ai-explainer">
      <div className="ttl">No AI never contacts any AI service</div>
      <p>No request is sent to any AI provider or local model server, even if connections exist.</p>
      <p>
        <strong>Still works without AI:</strong> detecting and inventorying the original, recovering code and resources with deterministic tools, collecting evidence, giving you editable recovered code, and deterministic web ports and comparisons.
      </p>
      <p>
        <strong>Needs AI:</strong> native, managed (.NET/Java) and game remakes. With No AI these become scaffolds or are marked blocked in the plan instead of being rebuilt.
      </p>
    </div>
  );
}

const SOURCES: { id: 'no_ai' | 'inherit' | 'custom'; label: string; sub: string }[] = [
  { id: 'no_ai', label: 'No AI', sub: 'Never contact any AI service' },
  { id: 'inherit', label: 'Use the app ladder', sub: 'The models set up on the Connections page' },
  { id: 'custom', label: 'Custom for this project', sub: 'Pick models just for this project' },
];

export function AiPolicyEditor({ idp, value, onChange, connections, budgetErr }: { idp: string; value: PolicyForm; onChange: (v: PolicyForm) => void; connections: Connection[]; budgetErr?: string | null }) {
  const [picking, setPicking] = useState<string | null>(null);
  const set = (p: Partial<PolicyForm>) => onChange({ ...value, ...p });
  const source = value.mode === 'no_ai' ? 'no_ai' : value.mode === 'custom' ? 'custom' : 'inherit';
  const hasLocal = connections.some(isLocalProvider);
  return (
    <div className="stack-lg" data-testid={`${idp}-policy`}>
      <div className="stack" role="radiogroup" aria-label="AI for this project" aria-describedby={source === 'no_ai' ? `${idp}-noai` : undefined}>
        <span className="field-label">AI for this project</span>
        <div className="choice-grid">
          {SOURCES.map((s) => (
            <label className="choice" key={s.id}>
              <input
                type="radio"
                name={`${idp}-source`}
                value={s.id}
                checked={source === s.id}
                onChange={() => set({ mode: s.id === 'inherit' && isInherit(value.mode) ? value.mode : s.id })}
              />
              <span>
                <span className="choice-title">{s.label}</span>
                <span className="choice-sub">{s.sub}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      {source === 'no_ai' ? (
        <NoAiExplainer id={`${idp}-noai`} />
      ) : (
        <>
          <div className="field-row">
            <div className="field">
              <label htmlFor={`${idp}-locality`}>Where models may run</label>
              <select id={`${idp}-locality`} value={value.locality} onChange={(e) => set({ locality: e.target.value as PolicyLocality })} aria-describedby={`${idp}-locality-h`}>
                <option value="any">Local or cloud</option>
                <option value="local_only">Local only (nothing leaves this PC)</option>
                <option value="cloud_only">Cloud only</option>
              </select>
              <span className="hint" id={`${idp}-locality-h`}>
                {value.locality === 'local_only'
                  ? hasLocal || connections.length === 0
                    ? 'Cloud models in the ladder are skipped and recorded as “skipped by project policy”.'
                    : 'No local connection exists yet, so no AI work can run. Add a local model server on the Connections page.'
                  : value.locality === 'cloud_only'
                    ? 'Local models in the ladder are skipped.'
                    : 'Each task tries its ladder in order, local and cloud alike.'}
              </span>
            </div>
            <div className="field">
              <label htmlFor={`${idp}-budget`}>Budget per job (USD){value.locality === 'local_only' ? '' : ' *'}</label>
              <input id={`${idp}-budget`} type="number" min="0" step="0.01" inputMode="decimal" value={value.budget_usd} onChange={(e) => set({ budget_usd: e.target.value })} aria-invalid={!!budgetErr} aria-describedby={`${idp}-budget-h${budgetErr ? ` ${idp}-budget-e` : ''}`} />
              <span className="hint" id={`${idp}-budget-h`}>
                Local models are free, so this limit only applies to cloud models. A call that could exceed it is skipped, not made.
              </span>
              {budgetErr && (
                <span className="field-error" id={`${idp}-budget-e`}>
                  {budgetErr}
                </span>
              )}
            </div>
          </div>
          <div className="field">
            <label className="check">
              <input type="checkbox" checked={value.approve_unknown_pricing} onChange={(e) => set({ approve_unknown_pricing: e.target.checked })} aria-describedby={`${idp}-unk-h`} />
              Allow models whose price is unknown
            </label>
            <span className="hint" id={`${idp}-unk-h`}>
              Some cloud models do not publish a price, so their cost cannot be predicted. Off (recommended): such a model is skipped and the plan shows “needs approval”. On: it may be used, but only within the budget above and with a cap on its output, and the spend is recorded as unknown.
            </span>
          </div>
          {source === 'custom' && (
            <fieldset>
              <legend>Models for this project</legend>
              <p className="small muted" style={{ marginBottom: 8 }}>A task you leave empty uses the app ladder. Position 1 is tried first; the rest are fallbacks in order.</p>
              <div className="stack-lg">
                {AI_TASKS.map((t) => {
                  const es = value.overrides[t.id] ?? [];
                  return (
                    <div key={t.id} className="stack">
                      <span className="small" style={{ fontWeight: 600 }}>
                        {t.label}
                      </span>
                      <LadderList id={`${idp}-${t.id}`} label={`${t.label} models for this project`} entries={es} onChange={(n) => set({ overrides: { ...value.overrides, [t.id]: n } })} readOnlyReason="This task uses the app ladder." />
                      <div>
                        <button type="button" className="btn sm" onClick={() => setPicking(t.id)}>
                          Add model<span className="sr-only"> to {t.label}</span>
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
              <ModelPicker
                open={picking != null}
                task={picking ?? ''}
                taskLabel={taskLabel(picking)}
                connections={connections}
                existing={new Set((value.overrides[picking ?? ''] ?? []).map(rungKey))}
                onPick={(e) => picking && set({ overrides: { ...value.overrides, [picking]: [...(value.overrides[picking] ?? []), e] } })}
                onClose={() => setPicking(null)}
              />
            </fieldset>
          )}
        </>
      )}
    </div>
  );
}
