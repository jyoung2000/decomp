import { useMemo, useState } from 'react';
import { ConfirmDialog } from '../components/Dialog';
import { Empty, Loading } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toasts';
import { dateTime, humanize } from '../lib/format';
import { useApi, useResource, useStoreSelector } from '../lib/store';
import type { KnowledgeEntry } from '../lib/types';

const STATES = ['proposed', 'validating', 'promoted', 'quarantined', 'rolled_back'];

export function KnowledgeView() {
  const api = useApi();
  const toast = useToast();
  const version = useStoreSelector((s) => s.state.versions.knowledge ?? 0);
  const list = useResource(() => api.knowledge(), [api], version);
  const [state, setState] = useState('');
  const [kind, setKind] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [rollback, setRollback] = useState<KnowledgeEntry | null>(null);
  const kinds = useMemo(() => [...new Set((list.data ?? []).map((k) => k.kind))].sort(), [list.data]);
  const rows = (list.data ?? []).filter((k) => (!state || k.state === state) && (!kind || k.kind === kind));
  const detail = useResource(() => (selected ? api.knowledgeItem(selected) : Promise.resolve(null)), [api, selected], version);

  const validate = async (k: KnowledgeEntry) => {
    try {
      await api.validateKnowledge(k.knowledge_id);
      toast.success('Validation started', `${k.name} runs against isolated regression fixtures.`);
      list.reload();
    } catch (e) {
      toast.error('Validation could not start', e);
    }
  };

  return (
    <div className="page" data-testid="knowledge">
      <div className="page-head">
        <div>
          <h1>Knowledge</h1>
          <p className="lead">Reusable signatures, adapters and rules. New knowledge is proposed, validated in isolation against regression fixtures, and only then promoted. Anything that regresses is quarantined; promoted entries can be rolled back.</p>
        </div>
      </div>
      <div className="filters" role="search" aria-label="Filter knowledge">
        <div className="field">
          <label htmlFor="kn-state">State</label>
          <select id="kn-state" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">Any</option>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="kn-kind">Kind</label>
          <select id="kn-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">Any</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {humanize(k)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {list.error ? (
        <ErrorCallout error={list.error} title="Knowledge could not be loaded" onRetry={list.reload} />
      ) : list.loading && !list.data ? (
        <Loading what="knowledge" />
      ) : rows.length === 0 ? (
        <Empty title={list.data?.length ? 'Nothing matches the filters' : 'No knowledge yet'}>
          {list.data?.length ? 'Clear a filter to see more.' : 'Knowledge is proposed during rebuilds (by tools or AI) and appears here for validation.'}
        </Empty>
      ) : (
        <div className="grid grid-2" style={{ alignItems: 'start' }}>
          <div className="table-wrap">
            <table className="table" aria-label="Knowledge entries">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">State</th>
                  <th scope="col">Regression</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((k) => (
                  <tr key={k.knowledge_id} aria-selected={selected === k.knowledge_id}>
                    <td>
                      <button type="button" className="btn ghost sm" style={{ height: 'auto', whiteSpace: 'normal', textAlign: 'left', justifyContent: 'flex-start' }} onClick={() => setSelected(k.knowledge_id)} aria-pressed={selected === k.knowledge_id}>
                        <span>
                          <strong>{k.name}</strong> <span className="xs muted">v{k.version}</span>
                          <br />
                          <span className="xs muted">{humanize(k.kind)}</span>
                        </span>
                      </button>
                    </td>
                    <td>
                      <StatusChip status={k.state} />
                    </td>
                    <td className="small">{regressionText(k)}</td>
                    <td>
                      <div className="btn-group">
                        <button type="button" className="btn sm" disabled={!['proposed', 'quarantined'].includes(k.state)} onClick={() => validate(k)} aria-label={`Validate ${k.name}`}>
                          Validate
                        </button>
                        <button type="button" className="btn sm danger" disabled={k.state !== 'promoted'} onClick={() => setRollback(k)} aria-label={`Roll back ${k.name}`}>
                          Roll back
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <section className="card" aria-live="polite">
            {!selected ? (
              <Empty title="Select an entry">Provenance, applicability and regression results appear here.</Empty>
            ) : detail.error ? (
              <ErrorCallout error={detail.error} onRetry={detail.reload} />
            ) : !detail.data ? (
              <Loading what="entry" />
            ) : (
              <KnowledgeDetail k={detail.data} />
            )}
          </section>
        </div>
      )}
      <ConfirmDialog
        open={!!rollback}
        title={`Roll back “${rollback?.name ?? ''}”?`}
        body={<p>The entry stops being used by future rebuilds and the previous version (if any) is restored. Builds that already used it are marked “needs retest”.</p>}
        confirmLabel="Roll back"
        danger
        onClose={() => setRollback(null)}
        onConfirm={async () => {
          if (!rollback) return;
          try {
            await api.rollbackKnowledge(rollback.knowledge_id);
            toast.success('Rolled back', rollback.name);
            list.reload();
          } catch (e) {
            toast.error('Rollback failed', e);
          }
        }}
      />
    </div>
  );
}

function regressionText(k: KnowledgeEntry) {
  const r = k.regression ?? {};
  if (typeof r.passed === 'number' || typeof r.failed === 'number') return `${r.passed ?? 0} passed · ${r.failed ?? 0} failed`;
  if (Array.isArray(r.cases)) return `${r.cases.filter((c) => c.verdict === 'pass').length}/${r.cases.length} passed`;
  return 'not run';
}

function KnowledgeDetail({ k }: { k: KnowledgeEntry }) {
  return (
    <div className="stack" data-testid="knowledge-detail">
      <div className="row">
        <h3>{k.name}</h3>
        <StatusChip status={k.state} />
      </div>
      <dl className="kv small">
        <dt>ID</dt>
        <dd className="mono">{k.knowledge_id}</dd>
        <dt>Kind / version</dt>
        <dd>
          {humanize(k.kind)} · v{k.version}
        </dd>
        <dt>Author</dt>
        <dd>{k.author}</dd>
        <dt>Source</dt>
        <dd className="wrap-any">{k.source}</dd>
        <dt>Confidence</dt>
        <dd>{Number.isFinite(k.confidence) ? k.confidence.toFixed(2) : '—'}</dd>
        <dt>Lineage</dt>
        <dd className="mono">{k.lineage ?? 'original'}</dd>
        <dt>Evidence</dt>
        <dd className="mono wrap-any">{k.evidence?.length ? k.evidence.join(', ') : 'none'}</dd>
        <dt>Updated</dt>
        <dd>{dateTime(k.updated_at)}</dd>
      </dl>
      <h4>Applies when</h4>
      {Object.keys(k.constraints ?? {}).length ? (
        <dl className="kv small">
          {Object.entries(k.constraints).map(([c, v]) => (
            <FragKV key={c} k={humanize(c)} v={Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v)} />
          ))}
        </dl>
      ) : (
        <p className="small muted">No applicability constraints recorded — treat as unrestricted.</p>
      )}
      <h4>Regression results</h4>
      {Array.isArray(k.regression?.cases) && k.regression.cases.length ? (
        <ul className="list">
          {k.regression.cases.map((c) => (
            <li key={c.id} className="row small">
              <StatusChip status={c.verdict} /> <span className="mono">{c.id}</span> <span className="muted">{c.detail}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="small muted">{regressionText(k)}</p>
      )}
    </div>
  );
}

function FragKV({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </>
  );
}
