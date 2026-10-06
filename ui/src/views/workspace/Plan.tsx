import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Dialog } from '../../components/Dialog';
import { Empty } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { StatusChip } from '../../components/StatusChip';
import { useToast } from '../../components/Toasts';
import { values } from '../../lib/derive';
import { dateTime } from '../../lib/format';
import { useApi, useCaseState, useResource, useStore } from '../../lib/store';
import { openPath } from '../../lib/tauri';
import type { AcceptanceCheck, Feedback, PlanItem, UnknownScopeEntry } from '../../lib/types';

const TREE_KINDS = new Set(['milestone', 'deliverable', 'feature']);

function sortItems(a: PlanItem, b: PlanItem) {
  return (a.sort_order ?? 0) - (b.sort_order ?? 0) || a.item_id.localeCompare(b.item_id, undefined, { numeric: true });
}

export function PlanTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [changeFor, setChangeFor] = useState<PlanItem | null>(null);
  const [exported, setExported] = useState<Record<string, unknown> | null>(null);
  const [exportError, setExportError] = useState<unknown>(null);
  const revisions = useResource(() => api.planRevisions(caseId), [api, caseId], cs?.planRevision ?? 0);

  const items = useMemo(() => (cs ? values(cs.planItems).sort(sortItems) : []), [cs]);
  const byParent = useMemo(() => {
    const m = new Map<string | null, PlanItem[]>();
    const ids = new Set(items.map((i) => i.item_id));
    for (const it of items) {
      if (!TREE_KINDS.has(it.kind)) continue;
      const parent = it.parent_id && ids.has(it.parent_id) ? it.parent_id : null;
      m.set(parent, [...(m.get(parent) ?? []), it]);
    }
    return m;
  }, [items]);
  const feedback = cs ? values(cs.feedback) : [];

  if (!cs) return <Empty title="Loading plan…" />;

  const toggle = (id: string, force?: boolean) =>
    setOpen((s) => {
      const n = new Set(s);
      if (force ?? !n.has(id)) n.add(id);
      else n.delete(id);
      return n;
    });
  const reveal = (id: string) => {
    let cur: PlanItem | undefined = items.find((i) => i.item_id === id);
    const chain: string[] = [];
    while (cur) {
      chain.push(cur.item_id);
      cur = cur.parent_id ? items.find((i) => i.item_id === cur!.parent_id) : undefined;
    }
    setOpen((s) => new Set([...s, ...chain]));
    requestAnimationFrame(() => document.getElementById(`plan-${id}`)?.focus());
  };

  const prioritize = async (it: PlanItem) => {
    try {
      await api.prioritize(caseId, it.item_id);
      toast.success('Prioritized', `${it.item_id} moves to the front of the queue.`);
      void store.refresh(caseId, 'plan');
    } catch (e) {
      toast.error('Could not prioritize', e);
    }
  };

  const doExport = async () => {
    setExportError(null);
    try {
      setExported(await api.exportPlan(caseId));
    } catch (e) {
      setExportError(e);
    }
  };

  const renderItem = (it: PlanItem) => {
    const kids = byParent.get(it.item_id) ?? [];
    return (
      <li key={it.item_id}>
        <ItemRow it={it} expanded={open.has(it.item_id)} onToggle={() => toggle(it.item_id)} />
        {open.has(it.item_id) && <ItemDetails caseId={caseId} it={it} feedback={feedback} onReveal={reveal} onPrioritize={() => prioritize(it)} onChange={() => setChangeFor(it)} />}
        {kids.length > 0 && <ul className="tree">{kids.map(renderItem)}</ul>}
      </li>
    );
  };

  const roots = byParent.get(null) ?? [];
  const special = (kind: string) => items.filter((i) => i.kind === kind);

  return (
    <div className="stack-lg" data-testid="plan">
      <div className="row between">
        <div>
          <h2>Plan</h2>
          <p className="small muted">
            Revision {cs.planRevision ?? '—'}
            {cs.planRevisionReason ? ` · ${cs.planRevisionReason}` : ''} · item IDs are stable across revisions
          </p>
        </div>
        <div className="btn-group">
          <button type="button" className="btn sm" onClick={() => setOpen(new Set(items.map((i) => i.item_id)))}>
            Expand all
          </button>
          <button type="button" className="btn sm" onClick={() => setOpen(new Set())}>
            Collapse all
          </button>
          <button type="button" className="btn sm primary" onClick={doExport} data-tooltip="Write project-plan.json and project-plan.html to the output reports folder" data-tooltip-pos="left" data-testid="export-plan">
            Export plan
          </button>
        </div>
      </div>
      {exportError ? <ErrorCallout error={exportError} title="Export failed" /> : null}
      {exported && <ExportResult result={exported} />}

      <section className="card" aria-labelledby="plan-tree-h">
        <h3 id="plan-tree-h">Milestones &amp; deliverables</h3>
        {roots.length ? (
          <ul className="tree" aria-label="Plan items">
            {roots.map(renderItem)}
          </ul>
        ) : (
          <Empty title={cs.planLoaded ? 'No plan items yet' : 'Loading plan…'}>Plan items appear when discovery has inventoried the original. Press Start in the header to begin.</Empty>
        )}
      </section>

      <div className="grid grid-3">
        <section className="card" aria-labelledby="plan-disc" data-testid="plan-discovery">
          <h3 id="plan-disc">Discovery (unknown scope)</h3>
          {special('discovery').length === 0 && cs.unknownScope.length === 0 ? (
            <p className="small muted">No open discovery items.</p>
          ) : (
            <ul className="list">
              {special('discovery').map((it) => (
                <li key={it.item_id}>
                  <ItemRow it={it} expanded={open.has(it.item_id)} onToggle={() => toggle(it.item_id)} />
                  {open.has(it.item_id) && <ItemDetails caseId={caseId} it={it} feedback={feedback} onReveal={reveal} onPrioritize={() => prioritize(it)} onChange={() => setChangeFor(it)} />}
                </li>
              ))}
              {cs.unknownScope.map((u, i) => (
                <li key={`u${i}`} className="small">
                  <UnknownScope u={u} />
                </li>
              ))}
            </ul>
          )}
        </section>
        {(['deferred', 'unsupported'] as const).map((k) => (
          <section className="card" key={k} aria-labelledby={`plan-${k}-h`} data-testid={`plan-${k}`}>
            <h3 id={`plan-${k}-h`}>{k === 'deferred' ? 'Deferred' : 'Unsupported'}</h3>
            {special(k).length === 0 ? (
              <p className="small muted">{k === 'deferred' ? 'Nothing deferred.' : 'Nothing marked unsupported.'}</p>
            ) : (
              <ul className="list">
                {special(k).map((it) => (
                  <li key={it.item_id}>
                    <ItemRow it={it} expanded={open.has(it.item_id)} onToggle={() => toggle(it.item_id)} />
                    {open.has(it.item_id) && <ItemDetails caseId={caseId} it={it} feedback={feedback} onReveal={reveal} onPrioritize={() => prioritize(it)} onChange={() => setChangeFor(it)} />}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>

      <section className="card" aria-labelledby="plan-rev-h" data-testid="plan-revisions">
        <h3 id="plan-rev-h">Revision history</h3>
        {revisions.error ? (
          <ErrorCallout error={revisions.error} onRetry={revisions.reload} />
        ) : !revisions.data?.length ? (
          <p className="small muted">No revisions recorded yet.</p>
        ) : (
          <ol className="list" reversed>
            {[...revisions.data].sort((a, b) => b.revision - a.revision).map((r) => (
              <li key={r.revision}>
                <div className="row">
                  <strong>r{r.revision}</strong>
                  <span className="small muted">{dateTime(r.created_at)}</span>
                </div>
                <div>{r.reason}</div>
                {r.changes && r.changes.length > 0 && (
                  <ul className="bullets small muted">
                    {r.changes.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      <ChangeDialog caseId={caseId} item={changeFor} onClose={() => setChangeFor(null)} />
    </div>
  );
}

function UnknownScope({ u }: { u: UnknownScopeEntry }) {
  if (typeof u === 'string') return <span>{u}</span>;
  return (
    <span>
      {u.id && <span className="mono muted">{u.id} </span>}
      {u.title}
      {u.reason && <span className="muted"> — {u.reason}</span>}
    </span>
  );
}

function ItemRow({ it, expanded, onToggle }: { it: PlanItem; expanded: boolean; onToggle: () => void }) {
  return (
    <div className="tree-row" data-testid={`plan-item-${it.item_id}`}>
      <button type="button" className="disclosure" id={`plan-${it.item_id}`} aria-expanded={expanded} aria-controls={`plan-details-${it.item_id}`} aria-label={`${expanded ? 'Collapse' : 'Expand'} ${it.item_id} ${it.title}`} onClick={onToggle}>
        {expanded ? '▼' : '▶'}
      </button>
      <span className="item-id">{it.item_id}</span>
      <span className="item-title" title={it.title}>
        {it.title}
        {it.kind !== 'milestone' && <span className="xs muted"> · {it.kind}</span>}
      </span>
      <span className="meta">
        {it.owner && <span className="small muted">{it.owner}</span>}
        <StatusChip status={it.status} />
      </span>
    </div>
  );
}

function checkText(a: AcceptanceCheck): { text: string; status?: string } {
  if (typeof a === 'string') return { text: a };
  return { text: [a.id, a.description ?? a.command].filter(Boolean).join(' — '), status: a.status };
}

function ItemDetails({ caseId, it, feedback, onReveal, onPrioritize, onChange }: { caseId: string; it: PlanItem; feedback: Feedback[]; onReveal: (id: string) => void; onPrioritize: () => void; onChange: () => void }) {
  const base = `/projects/${encodeURIComponent(caseId)}`;
  const linked = feedback.filter((f) => f.target_id === it.item_id || f.linked_items?.includes(it.item_id));
  return (
    <div className="item-details" id={`plan-details-${it.item_id}`} data-testid={`plan-details-${it.item_id}`}>
      <dl className="kv">
        <dt>Outcome</dt>
        <dd>{it.outcome || <span className="muted">not stated</span>}</dd>
        <dt>Owner</dt>
        <dd>{it.owner ?? <span className="muted">unassigned</span>}</dd>
        <dt>Depends on</dt>
        <dd>
          {it.depends_on?.length ? (
            <span className="row">
              {it.depends_on.map((d) => (
                <button key={d} type="button" className="btn sm ghost mono" onClick={() => onReveal(d)}>
                  {d}
                </button>
              ))}
            </span>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
        <dt>Acceptance</dt>
        <dd>
          {it.acceptance?.length ? (
            <ul className="bullets">
              {it.acceptance.map((a, i) => {
                const c = checkText(a);
                return (
                  <li key={i}>
                    <span className="mono small">{c.text}</span> {c.status && <StatusChip status={c.status} />}
                  </li>
                );
              })}
            </ul>
          ) : (
            <span className="muted">no checks defined</span>
          )}
        </dd>
        <dt>Evidence</dt>
        <dd>
          {it.evidence_ids?.length ? (
            <span className="row">
              {it.evidence_ids.map((e) => (
                <Link key={e} className="mono small" to={`${base}/advanced?evidence=${encodeURIComponent(e)}`}>
                  {e}
                </Link>
              ))}
            </span>
          ) : (
            <span className="muted">none yet</span>
          )}
        </dd>
        <dt>Produced files</dt>
        <dd>
          {it.files?.length ? (
            <ul className="bullets mono small">
              {it.files.map((f) => (
                <li key={f} className="wrap-any">
                  {f}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">none yet</span>
          )}
        </dd>
        <dt>Blockers</dt>
        <dd>{it.blockers?.length ? it.blockers.join('; ') : <span className="muted">none</span>}</dd>
        <dt>Feedback</dt>
        <dd>
          {linked.length ? (
            <ul className="bullets">
              {linked.map((f) => (
                <li key={f.feedback_id}>
                  <Link to={`${base}/feedback?focus=${encodeURIComponent(f.feedback_id)}`}>{f.comment.slice(0, 80) || f.feedback_id}</Link> <StatusChip status={f.status} />
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
        {it.job_ids?.length > 0 && (
          <>
            <dt>Jobs</dt>
            <dd className="mono small">{it.job_ids.join(', ')}</dd>
          </>
        )}
        {it.updated_at && (
          <>
            <dt>Updated</dt>
            <dd className="small">{dateTime(it.updated_at)}</dd>
          </>
        )}
      </dl>
      <div className="row" style={{ marginTop: 12 }}>
        {it.preview_id && (
          <Link className="btn sm primary" to={`${base}/preview?preview=${encodeURIComponent(it.preview_id)}`}>
            Open preview
          </Link>
        )}
        <button type="button" className="btn sm" onClick={onPrioritize} disabled={it.status === 'completed' || it.status === 'cancelled'} data-tooltip="Move this item ahead of other queued work">
          Prioritize
        </button>
        <button type="button" className="btn sm" onClick={onChange} data-tooltip="Ask for a change; affected items and tests are listed">
          Request change
        </button>
        <Link className="btn sm ghost" to={`${base}/feedback?target_kind=${it.kind}&target_id=${encodeURIComponent(it.item_id)}`}>
          Give feedback
        </Link>
      </div>
    </div>
  );
}

function ChangeDialog({ caseId, item, onClose }: { caseId: string; item: PlanItem | null; onClose: () => void }) {
  const api = useApi();
  const store = useStore();
  const [request, setRequest] = useState('');
  const [reason, setReason] = useState('');
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const close = () => {
    setRequest('');
    setReason('');
    setResult(null);
    setError(null);
    onClose();
  };
  const submit = async () => {
    if (!item || !request.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.requestChange(caseId, item.item_id, request.trim(), reason.trim()));
      void store.refresh(caseId, 'plan');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const list = (k: string) => (Array.isArray(result?.[k]) ? (result![k] as unknown[]).map(String) : []);
  return (
    <Dialog
      open={!!item}
      title={`Request change · ${item?.item_id ?? ''}`}
      onClose={close}
      actions={
        result ? (
          <button type="button" className="btn primary" onClick={close}>
            Done
          </button>
        ) : (
          <>
            <button type="button" className="btn" onClick={close}>
              Cancel
            </button>
            <button type="button" className="btn primary" disabled={!request.trim() || busy} onClick={submit}>
              {busy ? 'Sending…' : 'Send request'}
            </button>
          </>
        )
      }
    >
      <p className="small muted">{item?.title}</p>
      {error ? <ErrorCallout error={error} /> : null}
      {result ? (
        <div className="callout info" role="status">
          <div className="ttl">Change recorded</div>
          <div>Affected items: {list('affected_items').join(', ') || 'none reported'}</div>
          <div>Affected tests: {list('affected_tests').join(', ') || 'none reported'}</div>
        </div>
      ) : (
        <>
          <div className="field">
            <label htmlFor="chg-req">What should change?</label>
            <textarea id="chg-req" value={request} onChange={(e) => setRequest(e.target.value)} data-autofocus />
          </div>
          <div className="field">
            <label htmlFor="chg-reason">Why?</label>
            <textarea id="chg-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
        </>
      )}
    </Dialog>
  );
}

function ExportResult({ result }: { result: Record<string, unknown> }) {
  const toast = useToast();
  const paths: string[] = [];
  for (const [k, v] of Object.entries(result)) {
    if (typeof v === 'string' && /[\\/]/.test(v)) paths.push(v);
    else if (Array.isArray(v) && k === 'paths') paths.push(...v.map(String));
  }
  return (
    <div className="callout info" role="status" data-testid="export-result">
      <div className="ttl">Plan exported</div>
      <ul className="list">
        {paths.map((p) => (
          <li key={p} className="row">
            <span className="mono small wrap-any">{p}</span>
            <button
              type="button"
              className="btn sm"
              onClick={async () => {
                try {
                  if (!(await openPath(p))) {
                    await navigator.clipboard?.writeText(p);
                    toast.push({ kind: 'info', title: 'Path copied', message: 'Opening files works in the desktop app.' });
                  }
                } catch (e) {
                  toast.error('Could not open file', e);
                }
              }}
            >
              Open
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
