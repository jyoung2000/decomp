// Outcome model: pipeline progress and verified behaviour are different things and are never merged into one percentage.
// The controller derives the real value (controller/rebuild_controller/outcome.py). This file holds the pure TS fallback used
// when the controller does not send one, the sanity check applied to controller values, and the display helpers.
import type { CaseState } from './derive';
import type { Candidate, Case, Feature, Outcome, OutcomeState, PlanItem, VerificationState } from './types';

export const OUTCOME_LABELS: Record<OutcomeState, string> = {
  fully_matched: 'Fully matched within declared coverage',
  partially_matched: 'Partially matched',
  tested: 'Tested: does not match',
  scaffolded: 'Scaffold only: not a working remake',
  delivered: 'Delivered — not verified',
  built: 'Built — not verified',
  recovered: 'Recovered — nothing rebuilt yet',
  untested: 'Nothing measured yet',
};

export const OUTCOME_MEANING: Record<OutcomeState, string> = {
  fully_matched: 'Every declared scenario passed. Behaviour outside the declared scenarios has not been measured.',
  partially_matched: 'Some declared scenarios passed; others failed or have not been run.',
  tested: 'Declared scenarios were run and none of them passed.',
  scaffolded: 'The output is a build scaffold that reports itself as unimplemented. It does not reproduce the original behaviour.',
  delivered: 'The output folder was written, but nobody has measured whether it behaves like the original.',
  built: 'A build exists but has not been delivered or checked against the original.',
  recovered: 'The original was analysed; nothing has been rebuilt yet.',
  untested: 'No scenario results exist yet.',
};

export interface OutcomeFacts {
  scenariosDeclared: number;
  /** one verdict per scenario that was run: pass | fail | error */
  scenarioVerdicts: string[];
  stale?: boolean;
  /** features that no scenario checks; null when unknown */
  featuresWithoutOracle?: string[] | null;
  featuresTotal?: number;
  unsupportedModules?: string[];
  unknownScope?: boolean;
  scaffoldOnly?: boolean;
  built?: boolean;
  delivered?: boolean;
  recovered?: boolean;
  pipelineDone?: number;
  pipelineTotal?: number;
  unit?: 'scenarios' | 'features';
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;

/** Mirror of controller outcome.derive_outcome. Pure; never rounds up. */
export function deriveOutcome(f: OutcomeFacts): Outcome {
  const unit = f.unit ?? 'scenarios';
  const stale = !!f.stale;
  const counted = stale ? [] : f.scenarioVerdicts;
  const passed = counted.filter((v) => v === 'pass').length;
  const failed = counted.filter((v) => v === 'fail' || v === 'error').length;
  const ran = passed + failed;
  let declared = Math.max(0, f.scenariosDeclared);
  let untested = Math.max(0, declared - ran);
  if (ran > declared) {
    declared = ran;
    untested = 0;
  }
  let vstate: VerificationState;
  if (declared === 0 || ran === 0) vstate = 'untested';
  else if (passed === declared && failed === 0 && untested === 0) vstate = 'fully_matched';
  else if (passed > 0) vstate = 'partially_matched';
  else vstate = 'tested';

  const scaffold = !!f.scaffoldOnly;
  let state: OutcomeState;
  if (scaffold) {
    state = 'scaffolded';
    if (vstate === 'fully_matched') vstate = 'partially_matched';
  } else if (vstate !== 'untested') state = vstate;
  else if (f.delivered) state = 'delivered';
  else if (f.built) state = 'built';
  else if (f.recovered) state = 'recovered';
  else state = 'untested';

  const noOracle = f.featuresWithoutOracle ?? null;
  const unsupported = f.unsupportedModules ?? [];
  const noun = unit === 'features' ? 'feature' : 'scenario';
  const out: string[] = [];
  if (scaffold) out.push('The implementation is a scaffold that exits as unimplemented; no behaviour has been rebuilt yet.');
  if (declared === 0) out.push(`No ${noun}s are declared, so no behaviour can be compared. Declare scenarios or provide a baseline.`);
  else {
    if (stale) out.push('Earlier verification results are out of date and are not counted; run verification again.');
    if (failed) out.push(`${plural(failed, noun)} failed or errored.`);
    if (untested && !stale) out.push(`${plural(untested, `declared ${noun}`)} not run yet.`);
  }
  if (noOracle && noOracle.length) out.push(`${plural(noOracle.length, 'discovered feature')} with no scenario to check it against.`);
  if (unsupported.length) out.push(`${plural(unsupported.length, 'unsupported module')}: ${unsupported.slice(0, 5).join(', ')}.`);
  if (f.unknownScope) out.push('Discovery is not finished; undiscovered behaviour is not counted anywhere.');

  const done = f.pipelineDone ?? 0;
  const total = f.pipelineTotal ?? 0;
  return {
    state,
    label: OUTCOME_LABELS[state],
    can_claim_complete: state === 'fully_matched',
    scaffold_only: scaffold,
    verification: {
      state: vstate,
      verdict: vstate === 'fully_matched' ? 'verified' : vstate === 'partially_matched' ? 'partial' : vstate === 'tested' ? 'failed' : 'untested',
      declared,
      passed,
      failed,
      untested,
      stale,
      unit,
      text: declared ? `Behavior verified: ${passed} of ${declared} ${noun}s` : `Behavior verified: no ${noun}s declared`,
      scope: 'declared_scenarios_only',
    },
    pipeline: { recovered: !!f.recovered, scaffolded: scaffold, built: !!f.built, delivered: !!f.delivered, steps_done: done, steps_total: total, text: total ? `Pipeline: ${done}/${total} steps` : 'Pipeline: steps not reported' },
    coverage: {
      features_total: f.featuresTotal ?? 0,
      features_without_oracle: noOracle ? noOracle.length : null,
      features_without_oracle_titles: noOracle ?? [],
      unsupported_modules: unsupported.length,
      unsupported_modules_titles: unsupported,
      unknown_scope: !!f.unknownScope,
    },
    outstanding: out,
    scope_statement: declared ? `Results cover only the ${plural(declared, `declared ${noun}`)}; behaviour outside them is not measured.` : `No ${noun}s are declared, so nothing about behaviour is measured.`,
    source: 'estimated',
  };
}

/** Facts the UI can read without the controller's outcome: features, candidates, plan items and case status. */
export function factsFromCaseState(c: Case | null, features: Feature[], candidates: Candidate[], items: PlanItem[]): OutcomeFacts {
  const cand = [...candidates].sort((a, b) => b.revision - a.revision)[0];
  const meta = (cand?.meta ?? {}) as Record<string, unknown>;
  const milestones = items.filter((i) => i.kind === 'milestone');
  const short = (i: PlanItem) => i.item_id.split(':').pop();
  const done = (id: string) => items.some((i) => short(i) === id && i.status === 'completed');
  // Scenario counts are not in the case/features snapshots: estimate in features and say so (unit: 'features').
  const verdicts = features.flatMap((f) => (f.verify_status === 'verified' ? ['pass'] : f.verify_status === 'failed' ? ['fail'] : []));
  return {
    unit: 'features',
    scenariosDeclared: features.length,
    scenarioVerdicts: verdicts,
    stale: cand?.verification === 'stale',
    featuresTotal: features.length,
    featuresWithoutOracle: null,
    unsupportedModules: [...features.filter((f) => f.impl_status === 'unsupported').map((f) => f.title), ...items.filter((i) => i.kind === 'unsupported').map((i) => i.title)],
    unknownScope: items.some((i) => i.kind === 'discovery' && i.status !== 'completed'),
    scaffoldOnly: !!cand && meta.origin === 'scaffold' && !meta.author && !meta.proposed_files,
    built: cand?.build_status === 'built',
    delivered: c?.status === 'delivered' || done('M-DELIVER'),
    recovered: done('M-RECOVERY'),
    pipelineDone: milestones.filter((i) => i.status === 'completed').length,
    pipelineTotal: milestones.length,
  };
}

/**
 * A controller outcome is used as-is except that it is never allowed to claim more than its own numbers support:
 * "fully matched" needs declared > 0, every scenario passed, nothing failed or unrun, not stale and not a scaffold.
 */
export function sanitizeOutcome(o: Outcome): Outcome {
  const v = o.verification;
  const supported = v.declared > 0 && v.passed === v.declared && v.failed === 0 && v.untested === 0 && !v.stale && !o.scaffold_only;
  if (o.state === 'fully_matched' && !supported) {
    const state: OutcomeState = o.scaffold_only ? 'scaffolded' : v.passed > 0 ? 'partially_matched' : v.failed > 0 ? 'tested' : o.pipeline.delivered ? 'delivered' : o.pipeline.built ? 'built' : 'untested';
    return { ...o, state, label: OUTCOME_LABELS[state], can_claim_complete: false, verification: { ...v, state: v.passed > 0 ? 'partially_matched' : v.failed > 0 ? 'tested' : 'untested' } };
  }
  return { ...o, can_claim_complete: o.state === 'fully_matched' && supported, source: o.source ?? 'controller' };
}

/** Prefer the controller's value (case snapshot or plan snapshot); otherwise estimate from what the UI has. */
export function selectOutcome(cs: CaseState | null | undefined, valuesOf: { features: Feature[]; candidates: Candidate[]; items: PlanItem[] }): Outcome | null {
  if (!cs) return null;
  if (cs.outcome) return sanitizeOutcome(cs.outcome);
  const c = cs.case?.value ?? null;
  if (c?.outcome) return sanitizeOutcome(c.outcome);
  if (!c) return null;
  return deriveOutcome(factsFromCaseState(c, valuesOf.features, valuesOf.candidates, valuesOf.items));
}

/** Fraction (0-100) for the behaviour bar. Never reaches 100 unless fully matched; null when nothing is declared. */
export function verificationBarPercent(o: Outcome): number | null {
  const v = o.verification;
  if (!v.declared) return null;
  const pct = (v.passed / v.declared) * 100;
  if (o.can_claim_complete) return 100;
  return Math.min(pct, 98);
}

/** Short wording for a case status chip: "Delivered" alone is never shown when behaviour is unverified. */
export function statusLabel(status: string, o: Outcome | null): string | null {
  if (status !== 'delivered' && status !== 'completed') return null;
  if (!o) return 'Delivered — verification unknown';
  if (o.state === 'fully_matched') return 'Delivered — fully matched within declared coverage';
  if (o.state === 'partially_matched') return 'Delivered — partially matched';
  if (o.state === 'tested') return 'Delivered — does not match';
  if (o.state === 'scaffolded') return 'Delivered — scaffold only';
  return 'Delivered — not verified';
}
