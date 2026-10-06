import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Dialog } from '../../components/Dialog';
import { Empty } from '../../components/Empty';
import { StatusChip } from '../../components/StatusChip';
import { useToast } from '../../components/Toasts';
import { values } from '../../lib/derive';
import { dateTime, shortHash } from '../../lib/format';
import { useApi, useCaseState, useResource } from '../../lib/store';
import { hasTauri, launchPreview, openPath } from '../../lib/tauri';
import type { Candidate, Preview, PreviewOpenResult } from '../../lib/types';

const KIND_LABEL: Record<string, string> = { real: 'Real build', mockup: 'Mockup', recording: 'Recording', fixture: 'Fixture' };
const KIND_HELP: Record<string, string> = {
  real: 'Built from the rebuilt source in this project.',
  mockup: 'Not a working build: a mockup of the intended result.',
  recording: 'A recording of a previous run, not a live build.',
  fixture: 'A test fixture, not the rebuilt program.',
};

export function KindBadge({ kind }: { kind: string }) {
  return (
    <span className={`kind-badge ${kind}`} data-testid="preview-kind" title={KIND_HELP[kind]}>
      {KIND_LABEL[kind] ?? kind}
    </span>
  );
}

export function PreviewTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const toast = useToast();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const focus = params.get('preview');
  const cs = useCaseState(caseId);
  const [running, setRunning] = useState<Record<string, PreviewOpenResult>>({});
  const [testing, setTesting] = useState<Preview | null>(null);

  const candidates = useMemo(() => (cs ? values(cs.candidates).sort((a, b) => b.revision - a.revision) : []), [cs]);
  const previews = useMemo(() => (cs ? values(cs.previews) : []), [cs]);
  const features = useMemo(() => (cs ? values(cs.features) : []), [cs]);
  const lkg = candidates.find((c) => !!c.last_known_good) ?? null;

  useEffect(() => {
    if (focus) requestAnimationFrame(() => document.getElementById(`preview-${focus}`)?.focus());
  }, [focus, previews.length]);

  if (!cs) return <Empty title="Loading previews…" />;

  const open = async (p: Preview) => {
    try {
      const r = await api.openPreview(p.preview_id);
      setRunning((m) => ({ ...m, [p.preview_id]: r }));
      const launched = await launchPreview({ previewId: p.preview_id, url: r.url, command: r.command, instanceId: r.instance_id }).catch((e) => {
        toast.error('The desktop shell could not launch the preview', e);
        return true;
      });
      if (!launched && r.kind === 'browser' && r.url) window.open(r.url, '_blank', 'noopener');
      toast.success('Preview opened', r.kind === 'browser' ? r.url : 'Native preview launched');
    } catch (e) {
      toast.error('Preview could not be opened', e);
    }
  };
  const stop = async (p: Preview) => {
    try {
      await api.stopPreview(p.preview_id, running[p.preview_id]?.instance_id);
      setRunning((m) => {
        const n = { ...m };
        delete n[p.preview_id];
        return n;
      });
      toast.success('Preview stopped');
    } catch (e) {
      toast.error('Preview could not be stopped', e);
    }
  };

  if (!candidates.length && !previews.length) {
    return (
      <Empty title="No previews yet">
        Previews appear once a build candidate exists. Each one says whether it is a real build, a mockup, a recording or a fixture. Start the rebuild from the header; the Plan tab shows what is being built.
      </Empty>
    );
  }

  const orphan = previews.filter((p) => !candidates.some((c) => c.candidate_id === p.candidate_id));
  return (
    <div className="stack-lg" data-testid="preview">
      <h2>Preview &amp; Test</h2>
      {candidates.map((c, idx) => (
        <CandidateCard
          key={c.candidate_id}
          caseId={caseId}
          c={c}
          latest={idx === 0}
          lkg={lkg && lkg.candidate_id !== c.candidate_id ? lkg : null}
          previews={previews.filter((p) => p.candidate_id === c.candidate_id)}
          running={running}
          focus={focus}
          onOpen={open}
          onStop={stop}
          onTest={setTesting}
        />
      ))}
      {orphan.length > 0 && (
        <section className="card">
          <h3>Other previews</h3>
          <div className="stack-lg">
            {orphan.map((p) => (
              <PreviewCard key={p.preview_id} p={p} instance={running[p.preview_id]} focus={focus === p.preview_id} onOpen={open} onStop={stop} onTest={setTesting} />
            ))}
          </div>
        </section>
      )}
      <TestDialog
        preview={testing}
        features={features.filter((f) => !testing?.feature_ids || testing.feature_ids.includes(f.feature_id))}
        onClose={() => setTesting(null)}
        onOpen={() => testing && open(testing)}
        onReport={(classification, featureId) => {
          if (!testing) return;
          const q = new URLSearchParams({
            target_kind: featureId ? 'feature' : 'preview',
            target_id: featureId || testing.preview_id,
            candidate_id: testing.candidate_id,
            classification,
          });
          setTesting(null);
          nav(`/projects/${encodeURIComponent(caseId)}/feedback?${q.toString()}`);
        }}
      />
    </div>
  );
}

function CandidateCard(props: {
  caseId: string;
  c: Candidate;
  latest: boolean;
  lkg: Candidate | null;
  previews: Preview[];
  running: Record<string, PreviewOpenResult>;
  focus: string | null;
  onOpen: (p: Preview) => void;
  onStop: (p: Preview) => void;
  onTest: (p: Preview) => void;
}) {
  const { c, lkg } = props;
  const toast = useToast();
  return (
    <section className="card" aria-labelledby={`cand-${c.candidate_id}`} data-testid={`candidate-${c.candidate_id}`}>
      <div className="card-head">
        <div className="row">
          <h3 id={`cand-${c.candidate_id}`}>Version {c.revision}</h3>
          {props.latest && <span className="chip info">latest</span>}
          {!!c.last_known_good && <span className="chip ok">✓ last known good</span>}
          <StatusChip status={c.build_status} label={`build: ${c.build_status}`} />
          <StatusChip status={c.verification} label={`verification: ${c.verification}`} />
        </div>
        <button
          type="button"
          className="btn sm"
          disabled={!c.dist_dir}
          data-tooltip={c.dist_dir ? 'Open the built output files' : 'Nothing has been built yet'}
          data-tooltip-pos="left"
          onClick={async () => {
            if (!c.dist_dir) return;
            try {
              if (!(await openPath(c.dist_dir))) {
                await navigator.clipboard?.writeText(c.dist_dir);
                toast.push({ kind: 'info', title: 'Path copied', message: `${c.dist_dir} — opening folders works in the desktop app.` });
              }
            } catch (e) {
              toast.error('Could not open output files', e);
            }
          }}
        >
          Open output files
        </button>
      </div>
      <dl className="kv small">
        <dt>Build hash</dt>
        <dd className="mono" title={c.build_hash ?? ''}>
          {shortHash(c.build_hash, 16)}
        </dd>
        <dt>Plan revision</dt>
        <dd>r{c.plan_revision}</dd>
        <dt>Candidate</dt>
        <dd className="mono">{c.candidate_id}</dd>
        <dt>Created</dt>
        <dd>{dateTime(c.created_at)}</dd>
        {c.dist_dir && (
          <>
            <dt>Output</dt>
            <dd className="mono wrap-any">{c.dist_dir}</dd>
          </>
        )}
      </dl>
      {lkg && (
        <div className="callout info" style={{ marginTop: 12 }} data-testid="lkg-compare">
          <div className="ttl">Compared with last known good (version {lkg.revision})</div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col"></th>
                  <th scope="col">This version ({c.revision})</th>
                  <th scope="col">Last known good ({lkg.revision})</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Build hash</th>
                  <td className="mono">{shortHash(c.build_hash)}</td>
                  <td className="mono">{shortHash(lkg.build_hash)}</td>
                </tr>
                <tr>
                  <th scope="row">Plan revision</th>
                  <td>r{c.plan_revision}</td>
                  <td>r{lkg.plan_revision}</td>
                </tr>
                <tr>
                  <th scope="row">Verification</th>
                  <td>
                    <StatusChip status={c.verification} />
                  </td>
                  <td>
                    <StatusChip status={lkg.verification} />
                  </td>
                </tr>
                <ComparisonCountsRow caseId={props.caseId} a={c.candidate_id} b={lkg.candidate_id} />
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="stack-lg" style={{ marginTop: 16 }}>
        {props.previews.length === 0 ? (
          <p className="small muted">No preview published for this version yet.</p>
        ) : (
          props.previews.map((p) => <PreviewCard key={p.preview_id} p={p} instance={props.running[p.preview_id]} focus={props.focus === p.preview_id} onOpen={props.onOpen} onStop={props.onStop} onTest={props.onTest} />)
        )}
      </div>
    </section>
  );
}

function ComparisonCountsRow({ caseId, a, b }: { caseId: string; a: string; b: string }) {
  const api = useApi();
  const ra = useResource(() => api.comparisons(caseId, a), [api, caseId, a]);
  const rb = useResource(() => api.comparisons(caseId, b), [api, caseId, b]);
  const fmt = (rows?: { verdict: string }[]) => {
    if (!rows) return '…';
    const n = (v: string) => rows.filter((r) => r.verdict === v).length;
    return `${n('pass')} pass · ${n('fail')} fail · ${n('error')} error · ${n('skipped')} skipped`;
  };
  return (
    <tr>
      <th scope="row">Comparisons</th>
      <td>{fmt(ra.data)}</td>
      <td>{fmt(rb.data)}</td>
    </tr>
  );
}

function PreviewCard({ p, instance, focus, onOpen, onStop, onTest }: { p: Preview; instance?: PreviewOpenResult; focus: boolean; onOpen: (p: Preview) => void; onStop: (p: Preview) => void; onTest: (p: Preview) => void }) {
  const stale = !!p.stale;
  const isRunning = !!instance || !!p.instance_id;
  // mock: {kind, url}; controller: {type: "browser", root, entry} or {type: "native", command, cwd}
  const launch = p.launch as { kind?: string; type?: string; url?: string; command?: string[] | string; root?: string; entry?: string };
  return (
    <article
      className="card"
      style={{ background: 'var(--bg)', outline: focus ? '2px solid var(--focus)' : undefined }}
      aria-labelledby={`pv-${p.preview_id}`}
      id={`preview-${p.preview_id}`}
      tabIndex={-1}
      data-testid={`preview-${p.preview_id}`}
    >
      <div className="card-head">
        <div className="row">
          <KindBadge kind={p.kind} />
          <h4 id={`pv-${p.preview_id}`}>{p.title}</h4>
          {stale && (
            <span className="chip warn" data-testid="stale-preview" title={p.stale_reason ?? 'Inputs changed since this preview was built'}>
              ⟳ stale{p.stale_reason ? `: ${p.stale_reason}` : ''}
            </span>
          )}
          {isRunning && <span className="chip run">▶ running</span>}
        </div>
        <div className="btn-group">
          <button type="button" className="btn sm primary" onClick={() => onOpen(p)} data-testid="open-preview" data-tooltip={stale ? 'Opens an outdated build — results may not reflect the latest plan' : 'Launch this preview'}>
            Open Preview
          </button>
          <button type="button" className="btn sm" onClick={() => onTest(p)} data-testid="test-feature">
            Test Feature
          </button>
          <button type="button" className="btn sm danger" onClick={() => onStop(p)} disabled={!isRunning} data-testid="stop-preview" data-tooltip={isRunning ? 'Stop the running preview' : 'Not running'} data-tooltip-pos="left">
            Stop
          </button>
        </div>
      </div>
      {p.kind !== 'real' && (
        <p className="callout warn small" role="note">
          {KIND_HELP[p.kind] ?? 'Not a real build.'} Behaviour here is not evidence that the rebuilt program works.
        </p>
      )}
      <div className="grid grid-3" style={{ marginTop: 8 }}>
        <div>
          <h4>Available</h4>
          {p.available?.length ? (
            <ul className="bullets small">
              {p.available.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          ) : (
            <p className="small muted">Nothing listed.</p>
          )}
        </div>
        <div>
          <h4>Incomplete</h4>
          {p.incomplete?.length ? (
            <ul className="bullets small">
              {p.incomplete.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          ) : (
            <p className="small muted">Nothing listed.</p>
          )}
        </div>
        <div>
          <h4>Launch requirements</h4>
          {p.requirements?.length ? (
            <ul className="bullets small">
              {p.requirements.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          ) : (
            <p className="small muted">None.</p>
          )}
        </div>
      </div>
      <dl className="kv small" style={{ marginTop: 8 }}>
        <dt>Build hash</dt>
        <dd className="mono">{shortHash(p.build_hash, 16)}</dd>
        <dt>Plan revision</dt>
        <dd>r{p.plan_revision}</dd>
        <dt>Verification</dt>
        <dd>
          <StatusChip status={p.verification} />
        </dd>
        <dt>Launch</dt>
        <dd className="mono wrap-any">
          {launch?.url ??
            (Array.isArray(launch?.command) ? launch.command.join(' ') : launch?.command) ??
            (launch?.entry ? `${launch.type ?? 'browser'}: ${launch.entry}${launch.root ? ` from ${launch.root}` : ''}` : null) ??
            launch?.kind ??
            launch?.type ??
            '—'}
        </dd>
      </dl>
      {instance && instance.kind === 'native' && !hasTauri() && (
        <div className="callout info small" role="status" style={{ marginTop: 8 }}>
          Native previews are launched by the desktop app. Command: <code className="wrap-any">{Array.isArray(instance.command) ? instance.command.join(' ') : instance.command}</code>
        </div>
      )}
      {instance?.url && (
        <p className="small" style={{ marginTop: 8 }}>
          Running at{' '}
          <a href={instance.url} target="_blank" rel="noopener noreferrer" data-testid="preview-url">
            {instance.url}
          </a>{' '}
          · instance <span className="mono">{instance.instance_id}</span>
        </p>
      )}
    </article>
  );
}

function TestDialog({ preview, features, onClose, onOpen, onReport }: { preview: Preview | null; features: { feature_id: string; title: string }[]; onClose: () => void; onOpen: () => void; onReport: (cls: 'acceptance' | 'bug', featureId: string) => void }) {
  const [feature, setFeature] = useState('');
  useEffect(() => setFeature(''), [preview?.preview_id]);
  return (
    <Dialog
      open={!!preview}
      title="Test a feature"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onOpen}>
            Open Preview
          </button>
          <button type="button" className="btn danger" onClick={() => onReport('bug', feature)}>
            Report a problem…
          </button>
          <button type="button" className="btn primary" onClick={() => onReport('acceptance', feature)}>
            It works — record acceptance…
          </button>
        </>
      }
    >
      {preview && (
        <>
          <div className="row">
            <KindBadge kind={preview.kind} /> <strong>{preview.title}</strong>
          </div>
          <div className="field">
            <label htmlFor="test-feature">Feature</label>
            <select id="test-feature" value={feature} onChange={(e) => setFeature(e.target.value)} data-autofocus>
              <option value="">Whole preview</option>
              {features.map((f) => (
                <option key={f.feature_id} value={f.feature_id}>
                  {f.title}
                </option>
              ))}
            </select>
          </div>
          <div>
            <h4>Test steps</h4>
            {preview.steps?.length ? (
              <ol className="bullets">
                {preview.steps.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            ) : (
              <p className="small muted">No scripted steps; explore the preview and describe what you saw.</p>
            )}
          </div>
          <p className="small muted">
            Your result is saved as feedback with this build’s hash and plan revision attached.
          </p>
        </>
      )}
    </Dialog>
  );
}
