import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { LocalityBadge, OriginBadge } from '../../components/ai/Badges';
import { AiPolicyEditor, budgetError, formSig, formToPolicy, policyToForm, type PolicyForm } from '../../components/ai/AiPolicyEditor';
import { Empty } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { useToast } from '../../components/Toasts';
import { activityCost, activityKey, activityTokens, outcomeInfo } from '../../lib/ai';
import { values } from '../../lib/derive';
import { clockTime, itemLabel, parseTime } from '../../lib/format';
import { useApi, useCaseState, useResource, useStore } from '../../lib/store';
import type { AiActivity, AiPolicy } from '../../lib/types';

/** AI tab: policy ("AI settings"), the pause / change / resume flow and the live activity feed. */
export function AiTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const ladder = useResource(() => api.ladder(), [api]);
  return (
    <div className="stack-lg" data-testid="ai-tab">
      <div>
        <h2>AI</h2>
        <p className="small muted">What AI is allowed to do in this project, and what it has done so far. AI only proposes; builds and the verifier decide.</p>
      </div>
      <AiSettingsPanel caseId={caseId} currentRevision={ladder.data?.config_revision ?? null} />
      <ActivityFeed caseId={caseId} currentRevision={ladder.data?.config_revision ?? null} />
    </div>
  );
}

export function AiSettingsPanel({ caseId, currentRevision }: { caseId: string; currentRevision: number | null }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const policy = useResource<AiPolicy>(() => api.aiPolicy(caseId), [api, caseId]);
  const conns = useResource(() => api.connections(), [api]);
  const [form, setForm] = useState<PolicyForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (policy.data) {
      setForm(policyToForm(policy.data, conns.data ?? []));
      setTouched(false);
    }
  }, [policy.data, conns.data]);
  const status = cs?.case?.value.status ?? '';
  const paused = status === 'paused';
  const running = status === 'running' || status === 'queued';

  if (policy.error) return <ErrorCallout error={policy.error} title="AI settings could not be loaded" onRetry={policy.reload} />;
  if (!form) return <Empty title="Loading AI settings…" />;
  const err = touched ? budgetError(form) : null;
  const dirty = formSig(form) !== formSig(policyToForm(policy.data, conns.data ?? []));

  const save = async () => {
    setTouched(true);
    if (budgetError(form)) return;
    setSaving(true);
    try {
      await api.putAiPolicy(caseId, formToPolicy(form, policy.data));
      toast.success('AI settings saved', paused || running ? 'They apply to work that has not started yet.' : undefined);
      policy.reload();
      void store.refresh(caseId, 'case');
    } catch (e) {
      toast.error('AI settings were not saved', e);
    } finally {
      setSaving(false);
    }
  };
  const control = async (name: 'Pause' | 'Resume') => {
    setBusy(name);
    try {
      if (name === 'Pause') await api.pauseCase(caseId);
      else await api.resumeCase(caseId);
      toast.success(name === 'Pause' ? 'Pause requested' : 'Resumed');
      void store.refresh(caseId, 'case');
    } catch (e) {
      toast.error(`${name} failed`, e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="card" aria-labelledby="ai-settings-h" data-testid="ai-settings">
      <div className="card-head">
        <h3 id="ai-settings-h">AI settings</h3>
        {currentRevision != null && <span className="chip outline">Ladder version {currentRevision}</span>}
      </div>
      <div className="stack-lg">
        {paused ? (
          <div className="callout info" role="status" data-testid="paused-note">
            <div className="ttl">This project is paused</div>
            <div>Changes apply to work that has not started yet. Attempts already made keep the ladder version they used.</div>
            <div className="row">
              <button type="button" className="btn primary" onClick={() => control('Resume')} disabled={busy != null} aria-describedby={busy != null ? 'ai-ctl-busy' : undefined}>
                ↻ Resume
              </button>
              <Link className="btn" to="/connections">
                Change the app ladder
              </Link>
            </div>
          </div>
        ) : running ? (
          <div className="callout info" role="status">
            <div className="ttl">Work is running</div>
            <div>You can pause first, change the ladder or budget, then resume. Work that already started keeps its current settings; changes only affect work that has not started yet.</div>
            <div>
              <button type="button" className="btn" onClick={() => control('Pause')} disabled={busy != null} aria-describedby={busy != null ? 'ai-ctl-busy' : undefined}>
                ❚❚ Pause to change settings
              </button>
            </div>
          </div>
        ) : null}
        {busy != null && (
          <span className="sr-only" id="ai-ctl-busy">
            Wait for the current request to finish.
          </span>
        )}
        <AiPolicyEditor idp="ws-ai" value={form} onChange={(v) => setForm(v)} connections={conns.data ?? []} budgetErr={err} />
        <div className="row">
          <button type="button" className="btn primary" onClick={save} disabled={!dirty || saving} aria-describedby={!dirty ? 'ai-nosave' : undefined}>
            {saving ? 'Saving…' : 'Save AI settings'}
          </button>
          {!dirty && (
            <span className="small muted" id="ai-nosave">
              No changes to save.
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

export function ActivityFeed({ caseId, currentRevision }: { caseId: string; currentRevision: number | null }) {
  const api = useApi();
  const cs = useCaseState(caseId);
  const initial = useResource<AiActivity[]>(() => api.aiActivity(caseId), [api, caseId]);
  const live = cs?.aiActivity ?? [];
  const items = useMemo(() => {
    const seen = new Set<string>();
    const out: AiActivity[] = [];
    for (const a of [...(initial.data ?? []), ...live]) {
      const k = activityKey(a);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(a);
    }
    return out.sort((a, b) => (parseTime(b.at) ?? 0) - (parseTime(a.at) ?? 0));
  }, [initial.data, live]);
  const planItems = cs ? values(cs.planItems) : [];
  const groups = useMemo(() => {
    const m = new Map<string, AiActivity[]>();
    for (const a of items) {
      const k = a.plan_item_id ?? '';
      m.set(k, [...(m.get(k) ?? []), a]);
    }
    return [...m.entries()];
  }, [items]);

  return (
    <section className="card" aria-labelledby="ai-activity-h" data-testid="ai-activity">
      <div className="card-head">
        <h3 id="ai-activity-h">AI activity</h3>
        <span className="small muted" role="status">
          {items.length} {items.length === 1 ? 'entry' : 'entries'} · updates live
        </span>
      </div>
      {initial.error && items.length === 0 ? (
        <ErrorCallout error={initial.error} title="AI activity could not be loaded" onRetry={initial.reload} tone="warn" />
      ) : items.length === 0 ? (
        <Empty title="No AI activity yet">Nothing has been sent to a model in this project. When AI is used, each step appears here in plain language, newest first. Prompts are never shown.</Empty>
      ) : (
        <div className="stack-lg" aria-live="polite">
          {groups.map(([pid, rows]) => {
            const it = planItems.find((p) => p.item_id === pid);
            const title = pid ? `${itemLabel(pid)}${it ? ` · ${it.title}` : ''}` : 'General';
            return (
              <section key={pid || 'general'} aria-label={`Activity for ${title}`} data-testid={`activity-group-${pid || 'general'}`}>
                <h4 style={{ marginBottom: 4 }}>{title}</h4>
                <ul className="list">
                  {rows.map((a) => (
                    <ActivityRow key={activityKey(a)} a={a} caseId={caseId} currentRevision={currentRevision} />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}

function ActivityRow({ a, caseId, currentRevision }: { a: AiActivity; caseId: string; currentRevision: number | null }) {
  const base = `/projects/${encodeURIComponent(caseId)}`;
  const o = outcomeInfo(a.outcome);
  const cost = activityCost(a);
  const tokens = activityTokens(a);
  return (
    <li className="activity-row" data-testid="activity-row">
      <span className="small muted mono activity-time">{clockTime(a.at)}</span>
      <span className="activity-body">
        <span className="row">
          {o && (
            <span className={`chip ${o.tone}`} title={o.label}>
              <span className="glyph" aria-hidden="true">
                {o.icon}
              </span>
              <span className="sr-only">{o.label}: </span>
              <span aria-hidden="true">{o.label}</span>
            </span>
          )}
          <span>{a.text}</span>
        </span>
        <span className="row small muted">
          {a.model && (
            <>
              <span className="mono">{a.model}</span>
              <LocalityBadge locality={a.locality} />
            </>
          )}
          <OriginBadge origin={a.origin} />
          {tokens && <span>{tokens}</span>}
          {cost && <span>{cost}</span>}
          {a.config_revision != null && (
            <span title="The ladder version this attempt used">
              Ladder version {a.config_revision}
              {currentRevision != null && currentRevision !== a.config_revision ? ` (now ${currentRevision})` : ''}
            </span>
          )}
          {a.candidate_id && <Link to={`${base}/comparisons?candidate=${encodeURIComponent(a.candidate_id)}`}>Candidate {a.candidate_id}</Link>}
          {(a.evidence_ids ?? []).map((e) => (
            <Link key={e} className="mono" to={`${base}/advanced?evidence=${encodeURIComponent(e)}`}>
              {e}
            </Link>
          ))}
        </span>
        {a.fallback_reason && (
          <span className="small activity-fallback" data-testid="fallback-reason">
            Fell back to the next model: {a.fallback_reason}
          </span>
        )}
      </span>
    </li>
  );
}
