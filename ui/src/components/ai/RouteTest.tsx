import { useState } from 'react';
import { outcomeInfo } from '../../lib/ai';
import { aiControl, type RouteTestResult } from '../../lib/aiControl';
import { useApi } from '../../lib/store';
import { ErrorCallout } from '../ErrorCallout';
import { LocalityBadge } from './Badges';

const STATUS_TEXT: Record<string, string> = { would_answer: 'Would answer now', standby: 'Next if the ones above fail', skipped: 'Skipped now' };

/** "Test route": dry-runs the saved ladder (no tokens, nothing sent) and explains which rung would answer and why others are skipped. */
export function RouteTest({ task, taskLabel, disabledReason }: { task: string; taskLabel: string; disabledReason?: string }) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<RouteTestResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setRes(await aiControl(api).routeTest(task));
    } catch (e) {
      setRes(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack" data-testid={`route-test-${task}`}>
      <div className="row">
        <button type="button" className="btn sm" onClick={run} disabled={busy || !!disabledReason} title={disabledReason ?? 'Checks the saved ladder without sending anything (no tokens are used)'}>
          {busy ? 'Testing…' : 'Test route'}
          <span className="sr-only"> for {taskLabel}</span>
        </button>
        {disabledReason ? <span className="small muted">{disabledReason}</span> : <span className="small muted">Dry run of the saved ladder: no tokens, nothing is sent.</span>}
      </div>
      {error ? <ErrorCallout error={error} title="The route could not be tested" /> : null}
      {res && (
        <div className="callout info" role="region" aria-live="polite" aria-label={`Route test for ${taskLabel}`} data-testid={`route-test-${task}-result`}>
          <div className="ttl">{res.summary}</div>
          {res.rungs.length > 0 && (
            <ol className="list">
              {res.rungs.map((r) => {
                const o = r.status === 'skipped' ? outcomeInfo(r.outcome) : null;
                return (
                  <li key={`${r.connection_id}::${r.model}::${r.position}`} data-status={r.status}>
                    <span className="row" style={{ display: 'inline-flex', gap: 6 }}>
                      <strong className="mono">{r.model}</strong>
                      <LocalityBadge locality={r.locality} />
                      <span className={`chip ${r.status === 'would_answer' ? 'ok' : r.status === 'skipped' ? 'warn' : 'queue'}`}>{STATUS_TEXT[r.status] ?? r.status}</span>
                      {o ? <span className="small muted">{o.label}</span> : null}
                    </span>
                    <div className="small">{r.reason}</div>
                    {r.rules_text && r.rules_text.length > 0 ? <div className="xs muted">Rules: {r.rules_text.join('; ')}</div> : null}
                  </li>
                );
              })}
            </ol>
          )}
          {res.advisor ? <p className="xs muted">{res.advisor}</p> : null}
        </div>
      )}
    </div>
  );
}
