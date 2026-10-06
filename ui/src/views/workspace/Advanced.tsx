import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Empty, Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { CountsProgress } from '../../components/Progress';
import { StatusChip } from '../../components/StatusChip';
import { LocalTabs } from '../../components/Tabs';
import { useToast } from '../../components/Toasts';
import { describeEvent, jobCounts, values } from '../../lib/derive';
import { bytes, clockTime, dateTime, parseTime, shortHash, timeAgo } from '../../lib/format';
import { useApi, useCaseState, useNow, useResource, useStore, useStoreSelector } from '../../lib/store';

type Sub = 'jobs' | 'modules' | 'evidence' | 'logs';

export function AdvancedTab({ caseId }: { caseId: string }) {
  const [params] = useSearchParams();
  const [tab, setTab] = useState<Sub>(params.get('evidence') ? 'evidence' : 'jobs');
  return (
    <div className="stack-lg" data-testid="advanced">
      <h2>Advanced</h2>
      <LocalTabs<Sub>
        label="Advanced sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: 'jobs', label: 'Jobs' },
          { id: 'modules', label: 'Modules' },
          { id: 'evidence', label: 'Evidence' },
          { id: 'logs', label: 'Raw logs' },
        ]}
      />
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'jobs' && <Jobs caseId={caseId} />}
        {tab === 'modules' && <Modules caseId={caseId} />}
        {tab === 'evidence' && <EvidenceView caseId={caseId} initial={params.get('evidence')} />}
        {tab === 'logs' && <Logs caseId={caseId} />}
      </div>
    </div>
  );
}

function Jobs({ caseId }: { caseId: string }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const now = useNow();
  const jobs = useMemo(() => (cs ? values(cs.jobs).sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '')) : []), [cs]);
  if (!jobs.length) return <Empty title="No jobs yet">Jobs are created when the rebuild starts.</Empty>;
  const act = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      toast.success(label);
      void store.refresh(caseId, 'jobs');
    } catch (e) {
      toast.error(`${label} failed`, e);
    }
  };
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Jobs">
        <thead>
          <tr>
            <th scope="col">Job</th>
            <th scope="col">State</th>
            <th scope="col">Attempt</th>
            <th scope="col">Progress</th>
            <th scope="col">Heartbeat</th>
            <th scope="col">Blocker / error</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.job_id}>
              <td>
                <div>{j.title}</div>
                <div className="xs muted mono">
                  {j.stage} · {j.job_id}
                </div>
              </td>
              <td>
                <StatusChip status={j.state} />
              </td>
              <td className="num">
                {j.attempt}
                {j.max_attempts ? `/${j.max_attempts}` : ''}
              </td>
              <td>
                <CountsProgress {...jobCounts(j.progress as Record<string, unknown>)} />
              </td>
              <td className="small">{j.state === 'running' ? timeAgo(parseTime(j.heartbeat_at), now) : '—'}</td>
              <td className="small wrap-any">{j.blocker ?? j.error ?? ''}</td>
              <td>
                <div className="btn-group">
                  <button type="button" className="btn sm" disabled={!['queued', 'running', 'blocked'].includes(j.state)} onClick={() => act('Cancel requested', () => api.cancelJob(j.job_id))} aria-label={`Cancel job ${j.title}`}>
                    Cancel
                  </button>
                  <button type="button" className="btn sm" disabled={!['failed', 'cancelled', 'needs_retest'].includes(j.state)} onClick={() => act('Job resumed', () => api.resumeJob(j.job_id))} aria-label={`Resume job ${j.title}`}>
                    Resume
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Modules({ caseId }: { caseId: string }) {
  const api = useApi();
  const cs = useCaseState(caseId);
  const res = useResource(() => api.modules(caseId), [api, caseId], cs?.versions.evidence ?? 0);
  if (res.error) return <ErrorCallout error={res.error} title="Modules could not be loaded" onRetry={res.reload} />;
  if (res.loading && !res.data) return <Loading what="modules" />;
  if (!res.data?.length) return <Empty title="No modules inventoried yet">Discovery lists every executable, library and data package it finds in the source folder.</Empty>;
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Modules">
        <thead>
          <tr>
            <th scope="col">Path</th>
            <th scope="col">Format</th>
            <th scope="col">Profile</th>
            <th scope="col">Arch</th>
            <th scope="col">Size</th>
            <th scope="col">SHA-256</th>
          </tr>
        </thead>
        <tbody>
          {res.data.map((m) => (
            <tr key={m.module_id}>
              <td className="mono small wrap-any">{m.rel_path}</td>
              <td>{m.format}</td>
              <td>{m.profile}</td>
              <td>{m.arch ?? '—'}</td>
              <td className="num">{bytes(m.size)}</td>
              <td className="mono small" title={m.sha256}>
                {shortHash(m.sha256)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceView({ caseId, initial }: { caseId: string; initial: string | null }) {
  const api = useApi();
  const cs = useCaseState(caseId);
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(initial);
  const list = useResource(() => (query ? api.searchEvidence(caseId, query) : api.evidenceList(caseId)), [api, caseId, query], cs?.versions.evidence ?? 0);
  const item = useResource(() => (selected ? api.evidence(selected) : Promise.resolve(null)), [api, selected]);
  return (
    <div className="grid grid-2" style={{ alignItems: 'start' }}>
      <section className="card">
        <form
          className="row"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            setQuery(q.trim());
          }}
        >
          <label htmlFor="ev-q" className="sr-only">
            Search evidence
          </label>
          <input id="ev-q" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search evidence…" style={{ flex: 1 }} />
          <button type="submit" className="btn sm">
            Search
          </button>
        </form>
        <div style={{ marginTop: 12 }}>
          {list.error ? (
            <ErrorCallout error={list.error} onRetry={list.reload} />
          ) : list.loading && !list.data ? (
            <Loading what="evidence" />
          ) : !list.data?.length ? (
            <Empty title={query ? 'No matches' : 'No evidence yet'}>{query ? 'Try another search term.' : 'Evidence (inventories, decompiled functions, captures) appears as analysis runs.'}</Empty>
          ) : (
            <ul className="list">
              {list.data.map((e) => (
                <li key={e.evidence_id}>
                  <button type="button" className="btn ghost sm" style={{ justifyContent: 'flex-start', height: 'auto', whiteSpace: 'normal', textAlign: 'left' }} aria-pressed={selected === e.evidence_id} onClick={() => setSelected(e.evidence_id)}>
                    <span>
                      <strong>{e.title}</strong>{' '}
                      <span className="xs muted">
                        {e.kind} · r{e.revision}
                        {e.stale ? ' · stale' : ''}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
      <section className="card" aria-live="polite">
        {!selected ? (
          <Empty title="Select evidence">Choose an item to see its content (truncated to 64 KB).</Empty>
        ) : item.error ? (
          <ErrorCallout error={item.error} onRetry={item.reload} />
        ) : !item.data ? (
          <Loading what="evidence" />
        ) : (
          <div className="stack">
            <h3>{item.data.title}</h3>
            <dl className="kv small">
              <dt>ID</dt>
              <dd className="mono">{item.data.evidence_id}</dd>
              <dt>Kind</dt>
              <dd>{item.data.kind}</dd>
              <dt>Producer</dt>
              <dd>{item.data.producer ?? '—'}</dd>
              <dt>Created</dt>
              <dd>{dateTime(item.data.created_at)}</dd>
            </dl>
            <pre className="pre">{typeof item.data.body === 'string' ? item.data.body : JSON.stringify(item.data.body, null, 2)}</pre>
          </div>
        )}
      </section>
    </div>
  );
}

function Logs({ caseId }: { caseId: string }) {
  const cs = useCaseState(caseId);
  const [filter, setFilter] = useState('');
  const [onlyLogs, setOnlyLogs] = useState(false);
  const snap = useStoreSelector((st) => st.clientSnap);
  const rows = useMemo(() => {
    const f = filter.toLowerCase();
    return (cs?.log ?? [])
      .filter((e) => (!onlyLogs || e.kind === 'job.log') && (!f || e.kind.includes(f) || JSON.stringify(e.payload).toLowerCase().includes(f)))
      .slice()
      .reverse();
  }, [cs, filter, onlyLogs]);
  return (
    <section className="card">
      <div className="row" style={{ marginBottom: 12 }}>
        <label htmlFor="log-filter" className="sr-only">
          Filter events
        </label>
        <input id="log-filter" type="search" placeholder="Filter by kind or text…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 320 }} />
        <label className="check small">
          <input type="checkbox" checked={onlyLogs} onChange={(e) => setOnlyLogs(e.target.checked)} /> Job log lines only
        </label>
        <span className="small muted">Showing the latest {rows.length} events received this session (newest first).</span>
        {snap && (
          <span className="small muted" data-testid="event-stats" title="Events are deduplicated and applied in seq order; duplicates and replays are dropped.">
            Last seq {snap.lastSeq} · {snap.duplicatesDropped} duplicate{snap.duplicatesDropped === 1 ? '' : 's'} dropped
          </span>
        )}
      </div>
      {rows.length === 0 ? (
        <Empty title="No events yet">Events stream here live from the controller.</Empty>
      ) : (
        <div role="log" aria-label="Raw controller events" data-testid="raw-log">
          {rows.map((e) => (
            <div className="log-line" key={e.seq} data-seq={e.seq}>
              <span className="muted">#{e.seq}</span>
              <span className="muted">{clockTime(e.ts)}</span>
              <span>{e.kind}</span>
              <span className="wrap-any" title={JSON.stringify(e.payload)}>
                {describeEvent(e)}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
