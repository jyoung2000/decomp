import { useEffect, useState, type FormEvent } from 'react';
import { aiControl, offlineText, type JevDecision, type JevStatus, type JevTest } from '../../lib/aiControl';
import { dateTime, usd } from '../../lib/format';
import { useApi, useResource } from '../../lib/store';
import { Loading } from '../Empty';
import { ErrorCallout } from '../ErrorCallout';
import { useToast } from '../Toasts';

function decisionText(d: JevDecision): string {
  if (d.source !== 'jev') return `No advice (${(d.reason ?? 'unavailable').replace(/_/g, ' ')}); your ladder order was used`;
  if (d.kind === 'reassess') return `Between attempts${d.task ? ` (${d.task})` : ''}: ${d.choice === 'STOP' ? 'stop' : d.choice === 'SWITCH' ? 'switch to the next model' : 'retry'}`;
  return `${d.task ? `${d.task}: ` : ''}try ${d.suggested_first ?? d.choice} first`;
}

/**
 * Connections -> "JeV advisor": optional routing advisor over the TypeSafe API. Key entry (stored like every other key, never shown
 * again), an explicit "use the key from my JeV install" button, on/off, monthly cap, a one-request test and the last decisions.
 */
export function JevAdvisorCard() {
  const api = useApi();
  const toast = useToast();
  const ctl = aiControl(api);
  const res = useResource<JevStatus>(() => ctl.jev(), [api]);
  const [st, setSt] = useState<JevStatus | null>(null);
  const [key, setKey] = useState('');
  const [cap, setCap] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [test, setTest] = useState<JevTest | null>(null);
  useEffect(() => {
    if (res.data) {
      setSt(res.data);
      setCap(String(res.data.monthly_cap_usd));
    }
  }, [res.data]);
  const act = async (what: string, fn: () => Promise<JevStatus>, ok?: string) => {
    setBusy(what);
    try {
      const s = await fn();
      setSt(s);
      setCap(String(s.monthly_cap_usd));
      if (ok) toast.success(ok);
    } catch (e) {
      toast.error('JeV settings were not changed', e);
    } finally {
      setBusy(null);
    }
  };
  const saveKey = async (e: FormEvent) => {
    e.preventDefault();
    if (!key.trim()) return;
    await act('key', () => ctl.putJevKey(key.trim()), 'JeV key saved');
    setKey('');
  };
  const runTest = async () => {
    setBusy('test');
    try {
      const t = await ctl.testJev();
      setTest(t);
      res.reload();
    } catch (e) {
      toast.error('JeV test failed', e);
    } finally {
      setBusy(null);
    }
  };
  const s = st;
  return (
    <section className="card" aria-labelledby="jev-h" data-testid="jev-card">
      <div className="card-head">
        <h3 id="jev-h">JeV advisor</h3>
        {s && (
          <span className={`chip ${s.offline_reason ? 'muted' : 'ok'}`} data-testid="jev-state">
            {s.offline_reason ? 'Offline' : 'On'}
          </span>
        )}
      </div>
      <p className="small muted">Optional. JeV only re-orders the models already in your ladders and advises retry, switch or stop between repair attempts. It never adds a model, never spends past its monthly cap and never overrides the verifier. Only task names, token estimates and model descriptions are sent — never your code.</p>
      {res.error && !s ? (
        <ErrorCallout error={res.error} title="JeV status unavailable" onRetry={res.reload} tone="warn" />
      ) : !s ? (
        <Loading what="JeV status" />
      ) : (
        <div className="stack">
          <p className="small" data-testid="jev-offline">{offlineText(s)}</p>
          <div className="row">
            <label className="row small">
              <input type="checkbox" checked={s.enabled} disabled={busy != null} onChange={(e) => act('enabled', () => ctl.putJev({ enabled: e.target.checked }))} />
              Use JeV advice
            </label>
            <span className="small muted">
              Model <span className="mono">{s.model}</span>
              {s.price ? ` · $${s.price.input_per_mtok} per million input tokens` : ''}
            </span>
          </div>
          <form className="row" onSubmit={saveKey}>
            <label htmlFor="jev-key" className="small">
              {s.has_key ? 'Replace key' : 'TypeSafe / JeV key'}
            </label>
            <input id="jev-key" type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder={s.has_key ? 'stored — enter a new one to replace it' : 'paste your key'} style={{ minWidth: 260 }} />
            <button type="submit" className="btn sm primary" disabled={!key.trim() || busy != null}>
              {busy === 'key' ? 'Saving…' : 'Save key'}
            </button>
            {s.has_key && (
              <button type="button" className="btn sm" disabled={busy != null} onClick={() => act('remove', () => ctl.putJevKey(null), 'JeV key removed')}>
                Remove key
              </button>
            )}
          </form>
          <div className="row small">
            {s.has_key ? <span className="chip ok">Key stored{s.key_source === 'jev_install' ? ' (from your JeV install)' : ''}</span> : <span className="chip muted">No key</span>}
            <button type="button" className="btn sm" disabled={busy != null || !s.jev_install?.key_file_found} onClick={() => act('import', () => ctl.importJevKey(), 'Key copied from your JeV install')} aria-describedby="jev-import-why">
              {busy === 'import' ? 'Copying…' : 'Use the key from my JeV install'}
            </button>
            <span id="jev-import-why" className="xs muted">
              {s.jev_install?.key_file_found ? `Reads only ${s.jev_install.path}; the key is never shown.` : `No JeV key file found${s.jev_install?.path ? ` at ${s.jev_install.path}` : ''}.`}
            </span>
          </div>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              const n = Number(cap);
              if (Number.isFinite(n)) act('cap', () => ctl.putJev({ monthly_cap_usd: n }), 'Monthly cap saved');
            }}
          >
            <label htmlFor="jev-cap" className="small">
              Monthly cap (USD)
            </label>
            <input id="jev-cap" type="number" min={0} max={50} step={0.1} value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: 90 }} />
            <button type="submit" className="btn sm" disabled={busy != null || cap === String(s.monthly_cap_usd)}>
              Save cap
            </button>
            {s.month && (
              <span className="small muted" data-testid="jev-spend">
                This month: {usd(s.month.spent_usd)} of {usd(s.month.limit_usd)}
              </span>
            )}
          </form>
          <div className="row">
            <button type="button" className="btn sm" disabled={busy != null || !s.has_key} onClick={runTest}>
              {busy === 'test' ? 'Testing…' : 'Test JeV'}
            </button>
            <span className="xs muted">One small request, paid from a separate ${s.setup_cap_usd ?? 0.05} test allowance.</span>
            {test && (
              <span className={`small ${test.ok ? '' : 'bad'}`} role="status" data-testid="jev-test">
                {test.ok ? test.message ?? 'JeV answered.' : `Not working: ${test.message ?? test.reason}`}
              </span>
            )}
          </div>
          <div>
            <h4 className="small">Last decisions</h4>
            {s.last_decisions.length === 0 ? (
              <p className="small muted">None yet.</p>
            ) : (
              <ul className="list" data-testid="jev-decisions">
                {s.last_decisions.slice(0, 8).map((d, i) => (
                  <li key={`${d.at ?? ''}-${i}`} className="small">
                    {decisionText(d)}
                    {d.confidence != null ? <span className="muted"> · confidence {d.confidence.toFixed(2)}</span> : null}
                    {d.at ? <span className="muted"> · {dateTime(d.at)}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
