import { useMemo } from 'react';
import { values } from '../lib/derive';
import { OUTCOME_MEANING, selectOutcome, statusLabel, verificationBarPercent } from '../lib/outcome';
import { useCaseState } from '../lib/store';
import type { Outcome, OutcomeState } from '../lib/types';
import { StatusChip } from './StatusChip';

const TONE: Record<OutcomeState, string> = {
  fully_matched: 'ok',
  partially_matched: 'warn',
  tested: 'bad',
  scaffolded: 'warn',
  delivered: 'outline',
  built: 'outline',
  recovered: 'outline',
  untested: 'outline',
};
const GLYPH: Record<OutcomeState, string> = { fully_matched: '✓', partially_matched: '~', tested: '✕', scaffolded: '!', delivered: '?', built: '?', recovered: '?', untested: '?' };

/** The outcome for one case: the controller's value when present, otherwise an estimate made from the snapshots the UI holds. */
export function useOutcome(caseId: string): Outcome | null {
  const cs = useCaseState(caseId);
  return useMemo(() => (cs ? selectOutcome(cs, { features: values(cs.features), candidates: values(cs.candidates), items: values(cs.planItems) }) : null), [cs]);
}

export function OutcomeChip({ outcome, label }: { outcome: Outcome; label?: string }) {
  return (
    <span className={`chip ${TONE[outcome.state]}`} title={OUTCOME_MEANING[outcome.state]} data-status={outcome.state} data-testid="outcome-chip">
      <span className="glyph" aria-hidden="true">
        {GLYPH[outcome.state]}
      </span>
      {label ?? outcome.label}
    </span>
  );
}

/**
 * Case status chip. A delivered/completed case is shown with its measured outcome ("Delivered — not verified"),
 * never as a bare success state.
 */
export function CaseStatusChip({ status, outcome }: { status: string; outcome: Outcome | null }) {
  const text = statusLabel(status, outcome);
  if (!text) return <StatusChip status={status} />;
  if (!outcome) {
    return (
      <span className="chip outline" data-status="delivered" title="The output was written. Whether it behaves like the original is not known here.">
        {text}
      </span>
    );
  }
  return <OutcomeChip outcome={outcome} label={text} />;
}

/**
 * Two separate readings, deliberately styled differently: segmented pipeline steps (work done) and a measured-behaviour bar
 * (declared scenarios that passed). A full bar and "complete" wording appear only when the outcome is fully matched.
 */
export function OutcomePanel({ outcome, compact = false }: { outcome: Outcome; compact?: boolean }) {
  const v = outcome.verification;
  const p = outcome.pipeline;
  const c = outcome.coverage;
  const unit = v.unit === 'features' ? 'features' : 'scenarios';
  const pct = verificationBarPercent(outcome);
  const estimated = outcome.source === 'estimated';
  const steps = Math.max(0, p.steps_total);
  return (
    <section className={`card outcome ${outcome.state}`} aria-label="Outcome" data-testid="outcome" data-outcome={outcome.state}>
      <div className="card-head">
        <h3>Outcome</h3>
        <OutcomeChip outcome={outcome} />
      </div>
      <p className="outcome-headline" data-testid="outcome-headline">
        {outcome.can_claim_complete ? 'Every declared scenario passed.' : OUTCOME_MEANING[outcome.state]}
      </p>
      <div className="outcome-grid">
        <div className="outcome-block" data-testid="outcome-pipeline">
          <div className="label">Work done</div>
          <div className="outcome-line">{p.steps_total ? `Pipeline: ${p.steps_done}/${steps} steps` : 'Pipeline: steps not reported'}</div>
          {steps > 0 && (
            <div className="steps" role="img" aria-label={`Pipeline: ${p.steps_done} of ${steps} steps finished`}>
              {Array.from({ length: steps }, (_, i) => (
                <span key={i} className={i < p.steps_done ? 'on' : ''} />
              ))}
            </div>
          )}
          <ul className="flags small" aria-label="Pipeline stages">
            <li className={p.recovered ? 'yes' : ''}>{p.recovered ? '✓' : '–'} Recovered</li>
            <li className={p.scaffolded ? 'yes' : ''}>{p.scaffolded ? '✓' : '–'} Scaffolded</li>
            <li className={p.built ? 'yes' : ''}>{p.built ? '✓' : '–'} Built</li>
            <li className={p.delivered ? 'yes' : ''}>{p.delivered ? '✓' : '–'} Delivered</li>
          </ul>
          <p className="xs muted">Steps finished is not the same as the result matching the original.</p>
        </div>
        <div className="outcome-block" data-testid="outcome-verification">
          <div className="label">Behavior checked against the original</div>
          <div className="outcome-line" data-testid="verified-text">
            {v.declared ? `Behavior verified: ${v.passed} of ${v.declared} ${unit}` : `Behavior verified: no ${unit} declared`}
            {estimated && <span className="muted"> (estimated)</span>}
          </div>
          {pct != null ? (
            <div
              className={`bar verify${outcome.can_claim_complete ? ' full' : ''}`}
              role="progressbar"
              aria-label="Behavior verified"
              aria-valuemin={0}
              aria-valuemax={v.declared}
              aria-valuenow={v.passed}
              aria-valuetext={`${v.passed} of ${v.declared} ${unit} passed`}
            >
              <span style={{ width: `${pct}%` }} />
            </div>
          ) : (
            <div className="bar unknown" aria-hidden="true" />
          )}
          <dl className="kv small" data-testid="verification-counts">
            <dt>Declared</dt>
            <dd>{v.declared}</dd>
            <dt>Passed</dt>
            <dd>{v.passed}</dd>
            <dt>Failed</dt>
            <dd>{v.failed}</dd>
            <dt>Not run (untested)</dt>
            <dd>{v.untested}</dd>
          </dl>
          {v.stale && <p className="small">Earlier results are out of date and are not counted.</p>}
        </div>
      </div>
      {!compact && (
        <>
          <dl className="kv small" data-testid="coverage">
            <dt>Features with no scenario</dt>
            <dd>{c.features_without_oracle == null ? 'unknown' : `${c.features_without_oracle} of ${c.features_total}`}</dd>
            <dt>Unsupported modules</dt>
            <dd>{c.unsupported_modules}</dd>
            <dt>Unknown scope</dt>
            <dd>{c.unknown_scope ? 'yes — discovery is not finished' : 'none reported'}</dd>
          </dl>
          <p className="small muted" data-testid="scope-statement">
            {outcome.scope_statement ?? 'Scenario results cover only the declared scenarios; behaviour outside them is not measured.'}
          </p>
          {outcome.outstanding.length > 0 && (
            <div className="callout warn" data-testid="outstanding">
              <div className="ttl">Still outstanding</div>
              <ul className="bullets small">
                {outcome.outstanding.map((o, i) => (
                  <li key={i}>{o}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}
