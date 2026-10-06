import { Link } from 'react-router-dom';
import { useStaleness } from '../../components/ConnectionBanner';
import { Empty } from '../../components/Empty';
import { CountsProgress, PhaseProgressRow } from '../../components/Progress';
import { StatusChip } from '../../components/StatusChip';
import { useToast } from '../../components/Toasts';
import { describeEvent, describeScopeNote, phaseViews, values } from '../../lib/derive';
import { clockTime, duration, parseTime, timeAgo, usd } from '../../lib/format';
import { useApi, useCaseState, useResource, useStoreSelector } from '../../lib/store';
import { openPath } from '../../lib/tauri';
import type { Job } from '../../lib/types';

export function OverviewTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const { stale, reason, now, heartbeatSeconds } = useStaleness();
  const lastHeartbeat = useStoreSelector((s) => s.state.lastHeartbeat);
  const workersActive = useStoreSelector((s) => s.state.workersActive);
  const budgetVersion = useStoreSelector((s) => s.state.versions.budgets ?? 0);
  const budgets = useResource(() => api.budgets(), [api], budgetVersion);
  const calls = useResource(() => api.aiCalls(caseId), [api, caseId], budgetVersion);

  if (!cs || !cs.case) return <Empty title="Loading overview…" />;
  const c = cs.case.value;
  const jobs = values(cs.jobs);
  const running = jobs.filter((j) => j.state === 'running');
  const blocked = jobs.filter((j) => j.state === 'blocked' || (j.blocker && j.state !== 'completed'));
  const failed = jobs.filter((j) => j.state === 'failed');
  const planBlocked = values(cs.planItems).filter((i) => i.status === 'blocked');
  const phases = phaseViews(cs.progress);

  const startMs = parseTime(c.started_at ?? null) ?? jobs.map((j) => parseTime(j.started_at ?? null)).filter((x): x is number => x != null).sort((a, b) => a - b)[0] ?? null;
  const active = running.length > 0 || c.status === 'running';
  const endMs = active ? now : jobs.map((j) => parseTime(j.finished_at ?? null)).filter((x): x is number => x != null).sort((a, b) => b - a)[0] ?? null;
  const elapsed = startMs != null && endMs != null ? endMs - startMs : null;

  const hbAt = parseTime(lastHeartbeat?.ts ?? null);
  const caseBudget = cs.budget ?? budgets.data?.find((b) => b.scope === `case:${caseId}`) ?? null;
  const callRows = calls.data ?? [];
  const actual = callRows.filter((r) => r.cost_known && r.cost_usd != null).reduce((a, r) => a + (r.cost_usd ?? 0), 0);
  const unknownCost = callRows.filter((r) => !r.cost_known).length;

  const latest = cs.latestMeaningful;
  const eta = cs.eta;

  return (
    <div className="stack-lg" data-testid="overview">
      <section className="grid grid-4" aria-label="Status">
        <div className="card stat" data-testid="current-action">
          <span className="label">Current action</span>
          {running.length ? (
            running.slice(0, 2).map((j) => <CurrentJob key={j.job_id} job={j} />)
          ) : (
            <span className="value">{c.status === 'running' ? 'Waiting for the next step' : 'Idle'}</span>
          )}
          {running.length > 2 && <span className="small muted">+{running.length - 2} more running</span>}
        </div>
        <div className="card stat">
          <span className="label">Latest event</span>
          <span className="value" style={{ fontSize: 14 }} data-testid="latest-event">
            {latest ? describeEvent(latest) : 'No events yet'}
          </span>
          {latest && (
            <span className="small muted">
              {clockTime(latest.ts)} · seq {latest.seq}
            </span>
          )}
        </div>
        <div className="card stat" data-testid="heartbeat">
          <span className="label">Last heartbeat</span>
          <span className="row">
            <span className="value">{hbAt ? timeAgo(hbAt, now) : 'none yet'}</span>
            {stale ? (
              <span className="chip warn" data-testid="stale-badge" title={reason === 'disconnected' ? 'Disconnected from the controller' : `No event for more than ${2 * (heartbeatSeconds ?? 30)} s`}>
                ⟳ {reason === 'disconnected' ? 'unknown — disconnected' : 'stale'}
              </span>
            ) : (
              <span className="chip ok">live</span>
            )}
          </span>
          <span className="small muted">Expected every {heartbeatSeconds ?? '?'} s</span>
        </div>
        <div className="card stat">
          <span className="label">Elapsed · workers</span>
          <span className="value">{elapsed != null ? duration(elapsed) : 'not started'}</span>
          <span className="small muted">{workersActive != null ? `${workersActive} worker${workersActive === 1 ? '' : 's'} active` : 'worker count not reported yet'}</span>
        </div>
      </section>

      {(blocked.length > 0 || failed.length > 0 || planBlocked.length > 0) && (
        <section className="card" aria-label="Blockers" data-testid="blockers">
          <h3>Blockers</h3>
          <ul className="list">
            {blocked.map((j) => (
              <li key={j.job_id} className="row">
                <StatusChip status={j.state} /> <strong>{j.title}</strong> <span className="muted">{j.blocker ?? 'waiting on a dependency'}</span>
              </li>
            ))}
            {failed.map((j) => (
              <li key={j.job_id} className="row">
                <StatusChip status="failed" /> <strong>{j.title}</strong> <span className="muted wrap-any">{j.error ?? 'failed'}</span> <span className="small muted">attempt {j.attempt}</span>
              </li>
            ))}
            {planBlocked.map((i) => (
              <li key={i.item_id} className="row">
                <StatusChip status="blocked" /> <span className="mono small">{i.item_id}</span> {i.title} <span className="muted">{i.blockers.join('; ')}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card" aria-labelledby="ov-progress">
        <div className="card-head">
          <h3 id="ov-progress">Progress</h3>
          <span className="small muted">
            Plan revision {cs.planRevision ?? '—'} · percentages appear only when the scope is known
          </span>
        </div>
        <div className="stack" data-testid="phase-progress">
          {phases.map((p) => (
            <PhaseProgressRow key={p.phase} view={p} />
          ))}
        </div>
        <div className="divider" />
        <div className="row small" data-testid="eta">
          {eta ? (
            <span>
              <strong>Estimated remaining:</strong> about {duration(eta.seconds * 1000)}
              {eta.uncertainty != null && ` (± ${typeof eta.uncertainty === 'number' ? duration(eta.uncertainty * 1000) : eta.uncertainty})`} · estimate updated {clockTime(eta.updated_at)} — this is an estimate, not a promise.
            </span>
          ) : (
            <span className="muted">Remaining time unknown — the controller has not provided an estimate.</span>
          )}
        </div>
        {cs.scopeNotes.length > 0 && (
          <div className="callout info" style={{ marginTop: 12 }} data-testid="scope-notes">
            <div className="ttl">Scope changed</div>
            <ul className="bullets small">
              {cs.scopeNotes.slice(-5).map((n) => (
                <li key={`${n.seq}-${n.phase}`}>
                  {describeScopeNote(n)} <span className="muted">({clockTime(n.at)})</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {cs.unknownScope.length > 0 && (
          <p className="small muted" style={{ marginTop: 8 }}>
            Unknown scope remains in {cs.unknownScope.length} area{cs.unknownScope.length === 1 ? '' : 's'} — see <Link to={`/projects/${encodeURIComponent(caseId)}/plan`}>Plan → Discovery</Link>.
          </p>
        )}
      </section>

      <section className="grid grid-2">
        <div className="card" aria-labelledby="ov-cost">
          <h3 id="ov-cost">Resources &amp; cost</h3>
          <dl className="kv">
            <dt>AI budget</dt>
            <dd>
              {caseBudget ? (
                <>
                  {usd(caseBudget.spent_usd)} spent · {usd(caseBudget.reserved_usd)} reserved of {usd(caseBudget.limit_usd)}
                </>
              ) : c.ai_policy?.mode === 'no_ai' ? (
                'AI disabled for this project'
              ) : (
                'No budget reported yet'
              )}
            </dd>
            <dt>AI calls</dt>
            <dd>
              {callRows.length} call{callRows.length === 1 ? '' : 's'} · actual {usd(actual)}
              {unknownCost > 0 && ` (+${unknownCost} with unknown cost)`}
            </dd>
            <dt>CPU / memory / disk</dt>
            <dd>
              {c.resources ? (
                <>
                  {c.resources.cpu_percent != null ? `${c.resources.cpu_percent}% CPU` : 'CPU unknown'} · {c.resources.memory_mb != null ? `${c.resources.memory_mb} MB` : 'memory unknown'} ·{' '}
                  {c.resources.disk_mb != null ? `${c.resources.disk_mb} MB disk` : 'disk unknown'}
                </>
              ) : (
                <span className="muted">not reported by the controller</span>
              )}
            </dd>
            <dt>Jobs</dt>
            <dd>
              {(['running', 'queued', 'blocked', 'completed', 'failed', 'cancelled', 'needs_retest'] as const)
                .map((s) => [s, jobs.filter((j) => j.state === s).length] as const)
                .filter(([, n]) => n > 0)
                .map(([s, n]) => `${n} ${s.replace('_', ' ')}`)
                .join(' · ') || 'none yet'}
            </dd>
          </dl>
        </div>
        <div className="card" aria-labelledby="ov-out">
          <h3 id="ov-out">Output location</h3>
          <dl className="kv">
            <dt>Output folder</dt>
            <dd className="mono small" data-testid="output-root">
              {c.output_root}
            </dd>
            <dt>Contents</dt>
            <dd className="small">source/ · dist/ · evidence/ · reports/</dd>
            <dt>Source (read-only)</dt>
            <dd className="mono small">{c.source_root}</dd>
          </dl>
          <div className="row" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="btn sm"
              onClick={async () => {
                try {
                  if (!(await openPath(c.output_root))) {
                    await navigator.clipboard?.writeText(c.output_root);
                    toast.push({ kind: 'info', title: 'Path copied', message: 'Opening folders works in the desktop app; the path was copied instead.' });
                  }
                } catch (e) {
                  toast.error('Could not open the folder', e);
                }
              }}
            >
              Open output folder
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function CurrentJob({ job }: { job: Job }) {
  const p = job.progress ?? {};
  return (
    <div className="stack" style={{ gap: 2 }}>
      <span className="value" style={{ fontSize: 14 }}>
        {job.title}
      </span>
      {(p.current || p.target) && <span className="small wrap-any">{String(p.current ?? p.target)}</span>}
      <CountsProgress done={p.done} total={p.total} unit={p.unit} />
    </div>
  );
}
