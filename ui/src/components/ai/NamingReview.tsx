import { useEffect, useMemo, useState } from 'react';
import { aiQuality, proposalTitle, type Proposal } from '../../lib/aiQuality';
import { useApi, useResource } from '../../lib/store';
import { ErrorCallout } from '../ErrorCallout';
import { useToast } from '../Toasts';

const NATIVE = /^native/;

/** R5: AI names and comments for decompiled functions, labelled model-proposed until you accept them; reject or roll back any time. */
export function NamingReview({ caseId }: { caseId: string }) {
  const api = useApi();
  const toast = useToast();
  const q = aiQuality(api);
  const modules = useResource(() => api.modules(caseId), [api, caseId]);
  const native = useMemo(() => (modules.data ?? []).filter((m) => NATIVE.test(m.profile ?? '')), [modules.data]);
  const [moduleId, setModuleId] = useState<string>('');
  useEffect(() => {
    if (!moduleId && native.length) setModuleId(native[0].module_id);
  }, [native, moduleId]);
  const view = useResource(moduleId ? () => q.annotations(caseId, moduleId) : null, [api, caseId, moduleId]);
  const [count, setCount] = useState(8);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  if (modules.error || (modules.data && native.length === 0)) return null; // nothing to name in this project
  if (!modules.data) return null;

  const act = async (label: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      toast.success(done);
      view.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const proposals = view.data?.proposals ?? [];
  const open = proposals.filter((p) => p.status === 'proposed');
  const history = (view.data?.history ?? []).slice().reverse();
  return (
    <section className="card" aria-labelledby="naming-h" data-testid="naming-review">
      <div className="card-head">
        <h3 id="naming-h">AI names and comments</h3>
        {view.data && (
          <span className="chip outline" data-testid="naming-open-count">
            {open.length} waiting for review
          </span>
        )}
      </div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        A model proposes a name, a one-line comment and local names for each function, one function at a time. They are written as annotations labelled
        “model-proposed” (also in decompiled code and over MCP) until you accept them. Reject removes a proposal; roll back restores any earlier state.
      </p>
      <div className="row" style={{ marginBottom: 12 }}>
        <label className="small">
          Module{' '}
          <select value={moduleId} onChange={(e) => setModuleId(e.target.value)} aria-label="Module to name">
            {native.map((m) => (
              <option key={m.module_id} value={m.module_id}>
                {m.rel_path || m.module_id}
              </option>
            ))}
          </select>
        </label>
        <label className="small">
          Functions{' '}
          <select value={count} onChange={(e) => setCount(Number(e.target.value))} aria-label="How many functions to name">
            {[4, 8, 16].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn sm"
          disabled={!moduleId || busy != null}
          aria-describedby={!moduleId ? 'naming-why' : undefined}
          onClick={() => act('name', () => q.startNaming(caseId, moduleId, count), 'AI naming started: proposals appear here as each function is done.')}
        >
          {busy === 'name' ? 'Starting…' : 'Name functions with AI'}
        </button>
        {!moduleId && (
          <span className="sr-only" id="naming-why">
            Choose a module first.
          </span>
        )}
        <button type="button" className="btn sm" onClick={view.reload} disabled={!moduleId}>
          Refresh
        </button>
      </div>
      {error ? <ErrorCallout error={error} title="That did not work" /> : null}
      {view.error ? <ErrorCallout error={view.error} title="Annotations could not be loaded" onRetry={view.reload} /> : null}
      {view.data && proposals.length === 0 && <p className="small muted">No model-proposed names or comments in this module yet.</p>}
      {proposals.length > 0 && (
        <ul className="list" aria-label="Model-proposed annotations" data-testid="proposal-list">
          {proposals.map((p) => (
            <ProposalItem
              key={`${p.kind}:${p.key}:${p.local ?? ''}`}
              p={p}
              busy={busy != null}
              onAccept={() => act(`a:${p.kind}:${p.key}`, () => q.review(caseId, moduleId, p, 'accept'), `Accepted: ${p.value}`)}
              onReject={() => act(`r:${p.kind}:${p.key}`, () => q.review(caseId, moduleId, p, 'reject'), `Rejected: ${p.value}`)}
            />
          ))}
        </ul>
      )}
      {history.length > 1 && (
        <details style={{ marginTop: 12 }}>
          <summary className="small">History ({history.length} revisions)</summary>
          <ul className="list small" aria-label="Annotation history">
            {history.map((h, i) => (
              <li key={h.evidence_id} className="row">
                <span>
                  Revision {h.revision}: {h.change?.action ?? 'change'}
                  {h.change?.decision ? ` (${h.change.decision})` : ''}
                  {h.change?.new ? ` → ${h.change.new}` : ''}
                  {h.change?.label === 'model-proposed' ? ' · model-proposed' : ''}
                </span>
                {i > 0 && (
                  <button
                    type="button"
                    className="btn sm"
                    disabled={busy != null}
                    onClick={() => act(`rb:${h.revision}`, () => q.rollback(caseId, moduleId, h.revision), `Rolled back to revision ${h.revision}`)}
                  >
                    Roll back to revision {h.revision}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function ProposalItem({ p, busy, onAccept, onReject }: { p: Proposal; busy: boolean; onAccept: () => void; onReject: () => void }) {
  const was = p.replaced?.name ?? p.replaced?.text ?? p.analysis_name;
  const tone = p.status === 'proposed' ? 'warn' : 'ok';
  return (
    <li className="row" data-testid={`proposal-${p.kind}-${p.key}${p.local ? `-${p.local}` : ''}`}>
      <span style={{ flex: 1 }}>
        <strong>{proposalTitle(p)}:</strong> <code>{p.value}</code>
        {was ? <span className="small muted"> (was {was})</span> : null}
        <span className={`chip ${tone}`} style={{ marginLeft: 6 }}>
          {p.label}
        </span>
        {p.model ? <span className="small muted"> · {p.model}</span> : null}
        {p.confirmed_by ? <span className="small muted"> · {p.confirmed_by}</span> : null}
      </span>
      {p.status === 'proposed' && (
        <button type="button" className="btn sm" disabled={busy} onClick={onAccept} aria-label={`Accept ${proposalTitle(p)} ${p.value}`}>
          Accept
        </button>
      )}
      <button type="button" className="btn sm" disabled={busy} onClick={onReject} aria-label={`Reject ${proposalTitle(p)} ${p.value}`}>
        Reject
      </button>
    </li>
  );
}
