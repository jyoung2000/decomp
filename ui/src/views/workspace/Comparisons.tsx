import { Fragment, useEffect, useMemo, useState } from 'react';
import { Empty, Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { OutcomePanel, useOutcome } from '../../components/Outcome';
import { StatusChip } from '../../components/StatusChip';
import { values } from '../../lib/derive';
import { dateTime, describeTolerance, humanize, shortHash } from '../../lib/format';
import { useApi, useCaseState, useResource } from '../../lib/store';
import type { Artifact, Comparison } from '../../lib/types';

export function ComparisonsTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const cs = useCaseState(caseId);
  const outcome = useOutcome(caseId);
  const candidates = useMemo(() => (cs ? values(cs.candidates).sort((a, b) => b.revision - a.revision) : []), [cs]);
  const features = useMemo(() => (cs ? values(cs.features) : []), [cs]);
  const [candidate, setCandidate] = useState<string>('');
  const [feature, setFeature] = useState('');
  const [verdict, setVerdict] = useState('');
  const [channel, setChannel] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    if (!candidate && candidates[0]) setCandidate(candidates[0].candidate_id);
  }, [candidate, candidates]);
  const res = useResource(() => api.comparisons(caseId, candidate || undefined), [api, caseId, candidate], cs?.versions.comparisons ?? 0);
  const rows = useMemo(
    () => (res.data ?? []).filter((r) => (!feature || r.feature_id === feature) && (!verdict || r.verdict === verdict) && (!channel || r.channel === channel)),
    [res.data, feature, verdict, channel],
  );
  const channels = useMemo(() => [...new Set((res.data ?? []).map((r) => r.channel))].sort(), [res.data]);
  const featureTitle = (id: string | null) => (id ? features.find((f) => f.feature_id === id)?.title ?? id : '—');
  const counts = (v: string) => rows.filter((r) => r.verdict === v).length;

  return (
    <div className="stack-lg" data-testid="comparisons">
      <div>
        <h2>Comparisons</h2>
        <p className="small muted">Each row is a verifier check of the rebuilt candidate against the original. Only the verifier writes verdicts. Tolerances are shown exactly as declared. Passing rows prove only the declared scenarios, not the whole program.</p>
      </div>
      {outcome && <OutcomePanel outcome={outcome} />}
      <div className="filters" role="search" aria-label="Filter comparisons">
        <div className="field">
          <label htmlFor="cmp-cand">Candidate</label>
          <select id="cmp-cand" value={candidate} onChange={(e) => setCandidate(e.target.value)}>
            <option value="">All candidates</option>
            {candidates.map((c) => (
              <option key={c.candidate_id} value={c.candidate_id}>
                Version {c.revision} · {shortHash(c.build_hash)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="cmp-feat">Feature</label>
          <select id="cmp-feat" value={feature} onChange={(e) => setFeature(e.target.value)}>
            <option value="">All features</option>
            {features.map((f) => (
              <option key={f.feature_id} value={f.feature_id}>
                {f.title}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="cmp-verdict">Verdict</label>
          <select id="cmp-verdict" value={verdict} onChange={(e) => setVerdict(e.target.value)}>
            <option value="">Any</option>
            {['pass', 'fail', 'error', 'skipped'].map((v) => (
              <option key={v} value={v}>
                {humanize(v)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="cmp-channel">Channel</label>
          <select id="cmp-channel" value={channel} onChange={(e) => setChannel(e.target.value)}>
            <option value="">Any</option>
            {channels.map((c) => (
              <option key={c} value={c}>
                {humanize(c)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {res.error ? (
        <ErrorCallout error={res.error} title="Comparisons could not be loaded" onRetry={res.reload} />
      ) : res.loading && !res.data ? (
        <Loading what="comparisons" />
      ) : rows.length === 0 ? (
        <Empty title={res.data?.length ? 'No comparisons match the filters' : 'No comparisons yet'}>
          {res.data?.length ? 'Clear a filter to see more rows.' : 'Comparisons are recorded after a candidate is built and the verifier runs the original’s scenarios against it.'}
        </Empty>
      ) : (
        <>
          <p className="small" aria-live="polite">
            {rows.length} rows · {counts('pass')} pass · {counts('fail')} fail · {counts('error')} error · {counts('skipped')} skipped
          </p>
          <div className="table-wrap">
            <table className="table" aria-label="Comparison results">
              <thead>
                <tr>
                  <th scope="col">Channel</th>
                  <th scope="col">Feature</th>
                  <th scope="col">Rule</th>
                  <th scope="col">Tolerance</th>
                  <th scope="col">Verdict</th>
                  <th scope="col">Hashes (original / candidate)</th>
                  <th scope="col">Artifacts</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const tol = describeTolerance(r.tolerance);
                  const isOpen = expanded === r.comparison_id;
                  return (
                    <Fragment key={r.comparison_id}>
                      <tr data-testid={`cmp-${r.comparison_id}`}>
                        <td>{humanize(r.channel)}</td>
                        <td>{featureTitle(r.feature_id)}</td>
                        <td className="mono small wrap-any">{r.rule}</td>
                        <td className="small" data-testid="tolerance">
                          {tol.exact ? <span>{tol.text}</span> : <span className="chip warn" style={{ whiteSpace: 'normal', height: 'auto' }}>{tol.text}</span>}
                        </td>
                        <td>
                          <StatusChip status={r.verdict} />
                        </td>
                        <td className="mono small">
                          <span title={r.original_hash ?? ''}>{shortHash(r.original_hash)}</span>
                          <br />
                          <span title={r.candidate_hash ?? ''}>{shortHash(r.candidate_hash)}</span>
                          {r.original_hash && r.candidate_hash && (
                            <div className="xs muted">{r.original_hash === r.candidate_hash ? 'identical' : 'different'}</div>
                          )}
                        </td>
                        <td>
                          <button type="button" className="btn sm" aria-expanded={isOpen} aria-controls={`cmp-detail-${r.comparison_id}`} onClick={() => setExpanded(isOpen ? null : r.comparison_id)}>
                            {r.artifacts?.length ? `${r.artifacts.length} · details` : 'Details'}
                          </button>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr id={`cmp-detail-${r.comparison_id}`}>
                          <td colSpan={7}>
                            <ComparisonDetail r={r} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function ComparisonDetail({ r }: { r: Comparison }) {
  const images = (r.artifacts ?? []).filter((a) => (a.media_type ?? '').startsWith('image/') || /\.(png|jpe?g|webp|gif)$/i.test(a.path ?? a.url ?? ''));
  const order = ['original', 'candidate', 'diff'];
  images.sort((a, b) => order.indexOf(a.role ?? '') - order.indexOf(b.role ?? ''));
  const others = (r.artifacts ?? []).filter((a) => !images.includes(a));
  return (
    <div className="stack" data-testid="cmp-detail">
      {images.length > 0 && (
        <div className="shots" data-testid="screenshots">
          {images.map((a, i) => (
            <figure key={i}>
              <AuthImage a={a} />
              <figcaption>
                {humanize(a.role ?? a.name ?? `image ${i + 1}`)}
                {a.path && <span className="mono"> · {a.path}</span>}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      {others.length > 0 && (
        <ul className="bullets small mono">
          {others.map((a, i) => (
            <li key={i} className="wrap-any">
              {a.role ? `${a.role}: ` : ''}
              {a.name ?? a.path ?? a.url}
            </li>
          ))}
        </ul>
      )}
      <dl className="kv small">
        <dt>Details</dt>
        <dd>{typeof r.details === 'string' ? r.details : <pre className="pre">{JSON.stringify(r.details, null, 2)}</pre>}</dd>
        {r.command && (
          <>
            <dt>Command</dt>
            <dd className="mono">{r.command}</dd>
          </>
        )}
        <dt>Recorded</dt>
        <dd>{dateTime(r.created_at)}</dd>
        <dt>Comparison</dt>
        <dd className="mono">{r.comparison_id}</dd>
      </dl>
    </div>
  );
}

function AuthImage({ a }: { a: Artifact }) {
  const api = useApi();
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!a.url) return;
    let url: string | null = null;
    let alive = true;
    api.blobUrl(a.url).then(
      (u) => {
        url = u;
        if (alive) setSrc(u);
        else URL.revokeObjectURL(u);
      },
      () => alive && setFailed(true),
    );
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [api, a.url]);
  if (!a.url) return <div className="pre small">Image stored at {a.path ?? 'unknown path'} (no URL provided by the controller)</div>;
  if (failed) return <div className="pre small">Image could not be loaded.</div>;
  if (!src) return <div className="pre small">Loading image…</div>;
  return <img src={src} alt={`${a.role ?? 'artifact'} screenshot`} />;
}
