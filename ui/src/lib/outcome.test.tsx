import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { CaseStatusChip, OutcomePanel } from '../components/Outcome';
import { demoCase, caseRoutes, makeStore } from '../test-utils';
import { toPhaseView } from './derive';
import { deriveOutcome, factsFromCaseState, sanitizeOutcome, statusLabel, verificationBarPercent } from './outcome';
import type { Candidate, Feature, Outcome, PlanItem } from './types';

const base = { scenariosDeclared: 0, scenarioVerdicts: [] as string[] };
const pass = (n: number) => Array<string>(n).fill('pass');

describe('deriveOutcome (UI fallback, mirrors the controller)', () => {
  it('delivered with nothing verified is "Delivered — not verified", never a match', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 8, built: true, delivered: true, recovered: true, pipelineDone: 10, pipelineTotal: 10 });
    expect(o.state).toBe('delivered');
    expect(o.label).toBe('Delivered — not verified');
    expect(o.can_claim_complete).toBe(false);
    expect(o.verification).toMatchObject({ declared: 8, passed: 0, failed: 0, untested: 8, state: 'untested' });
    expect(o.pipeline.text).toBe('Pipeline: 10/10 steps');
    expect(o.verification.text).toBe('Behavior verified: 0 of 8 scenarios');
    expect(verificationBarPercent(o)).toBe(0);
  });

  it('no baseline / empty scenario list is untested and has no percentage', () => {
    const o = deriveOutcome({ ...base, built: true });
    expect(o.state).toBe('built');
    expect(o.verification.state).toBe('untested');
    expect(verificationBarPercent(o)).toBeNull();
    expect(o.outstanding.join(' ')).toMatch(/No scenarios are declared/);
    expect(deriveOutcome(base).state).toBe('untested');
  });

  it('wrong remake: tested, all failed', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 4, scenarioVerdicts: ['fail', 'fail', 'error', 'fail'], built: true, delivered: true });
    expect(o.state).toBe('tested');
    expect(o.verification).toMatchObject({ passed: 0, failed: 4, verdict: 'failed' });
  });

  it('partial pass stays partial and the bar never fills', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 9, scenarioVerdicts: [...pass(8), 'fail'] });
    expect(o.state).toBe('partially_matched');
    expect(o.can_claim_complete).toBe(false);
    expect(verificationBarPercent(o)!).toBeLessThan(100);
  });

  it('all declared passed is fully matched only within declared coverage and keeps outstanding work', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 8, scenarioVerdicts: pass(8), built: true, delivered: true, featuresWithoutOracle: ['Windows apphost'], unsupportedModules: ['Installer'], unknownScope: true });
    expect(o.state).toBe('fully_matched');
    expect(o.can_claim_complete).toBe(true);
    expect(verificationBarPercent(o)).toBe(100);
    expect(o.outstanding).toHaveLength(3);
    expect(o.scope_statement).toMatch(/only the 8 declared scenarios/);
  });

  it('scaffold-only is never matched, even if the numbers say so', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 2, scenarioVerdicts: pass(2), scaffoldOnly: true, built: true, delivered: true });
    expect(o.state).toBe('scaffolded');
    expect(o.can_claim_complete).toBe(false);
    expect(o.verification.state).not.toBe('fully_matched');
  });

  it('stale results are not counted', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 3, scenarioVerdicts: pass(3), stale: true, built: true });
    expect(o.state).toBe('built');
    expect(o.verification).toMatchObject({ passed: 0, untested: 3, stale: true });
  });

  it('estimates from case state in features, labelled as estimated', () => {
    const feats = [
      { feature_id: 'a', title: 'A', critical: false, impl_status: 'runnable', verify_status: 'untested', verify_candidate: null, user_review: null },
      { feature_id: 'b', title: 'B', critical: false, impl_status: 'unsupported', verify_status: 'untested', verify_candidate: null, user_review: null },
    ] as Feature[];
    const cands = [{ candidate_id: 'k', revision: 1, build_status: 'built', verification: 'untested', meta: { origin: 'scaffold' } }] as unknown as Candidate[];
    const items = [{ item_id: 'c:M-DELIVER', kind: 'milestone', status: 'completed' }, { item_id: 'c:M-BUILD', kind: 'milestone', status: 'queued' }] as PlanItem[];
    const o = deriveOutcome(factsFromCaseState({ ...demoCase, status: 'delivered' } as never, feats, cands, items));
    expect(o).toMatchObject({ state: 'scaffolded', source: 'estimated' });
    expect(o.verification.unit).toBe('features');
    expect(o.pipeline).toMatchObject({ delivered: true, built: true, steps_done: 1, steps_total: 2 });
    expect(o.coverage.features_without_oracle).toBeNull();
    expect(o.coverage.unsupported_modules).toBe(1);
  });
});

describe('sanitizeOutcome', () => {
  it('refuses a controller claim of "fully matched" that its own counts do not support', () => {
    const lying: Outcome = { ...deriveOutcome({ ...base, scenariosDeclared: 8, scenarioVerdicts: [...pass(3)], delivered: true, built: true }), state: 'fully_matched', label: 'x', can_claim_complete: true };
    const s = sanitizeOutcome(lying);
    expect(s.state).toBe('partially_matched');
    expect(s.can_claim_complete).toBe(false);
    const zero = sanitizeOutcome({ ...deriveOutcome({ ...base, delivered: true }), state: 'fully_matched', can_claim_complete: true });
    expect(zero.state).toBe('delivered');
  });
  it('keeps an honest controller value', () => {
    const ok = deriveOutcome({ ...base, scenariosDeclared: 2, scenarioVerdicts: pass(2) });
    expect(sanitizeOutcome(ok)).toMatchObject({ state: 'fully_matched', can_claim_complete: true });
  });
});

describe('display', () => {
  it('status wording never shows bare Delivered', () => {
    expect(statusLabel('delivered', null)).toBe('Delivered — verification unknown');
    expect(statusLabel('delivered', deriveOutcome({ ...base, scenariosDeclared: 8, delivered: true }))).toBe('Delivered — not verified');
    expect(statusLabel('running', null)).toBeNull();
  });

  it('chip: Delivered + 0 verified reads "Delivered — not verified"', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 8, delivered: true, built: true });
    render(<CaseStatusChip status="delivered" outcome={o} />);
    expect(screen.getByTestId('outcome-chip')).toHaveTextContent('Delivered — not verified');
    expect(screen.queryByText(/^Delivered$/)).toBeNull();
  });

  it('panel: pipeline steps and behaviour are separate, with counts, and no 100% when unverified', () => {
    const o = deriveOutcome({
      ...base,
      scenariosDeclared: 8,
      built: true,
      delivered: true,
      recovered: true,
      pipelineDone: 9,
      pipelineTotal: 9,
      featuresTotal: 9,
      featuresWithoutOracle: ['Windows apphost'],
      unsupportedModules: ['Installer'],
      unknownScope: true,
    });
    const { container } = render(<OutcomePanel outcome={{ ...o, source: 'controller' }} />);
    expect(screen.getByTestId('outcome-pipeline')).toHaveTextContent('Pipeline: 9/9 steps');
    expect(screen.getByTestId('verified-text')).toHaveTextContent('Behavior verified: 0 of 8 scenarios');
    const counts = screen.getByTestId('verification-counts');
    expect(counts).toHaveTextContent('Declared8');
    expect(counts).toHaveTextContent('Passed0');
    expect(counts).toHaveTextContent('Failed0');
    expect(counts).toHaveTextContent('Not run (untested)8');
    const cov = screen.getByTestId('coverage');
    expect(cov).toHaveTextContent('Features with no scenario1 of 9');
    expect(cov).toHaveTextContent('Unsupported modules1');
    expect(cov).toHaveTextContent('Unknown scopeyes');
    expect(screen.getByTestId('scope-statement')).toHaveTextContent('only the 8 declared scenarios');
    expect(screen.getByTestId('outstanding')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/100\s?%/);
    expect(container.textContent).not.toMatch(/complete/i);
    const bar = within(screen.getByTestId('outcome-verification')).getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '0');
    expect(container.querySelector('.bar.verify.full')).toBeNull();
  });

  it('panel: full bar and success wording only when fully matched', () => {
    const o = deriveOutcome({ ...base, scenariosDeclared: 2, scenarioVerdicts: pass(2), built: true });
    const { container } = render(<OutcomePanel outcome={o} />);
    expect(container.querySelector('.bar.verify.full')).not.toBeNull();
    expect(screen.getByTestId('outcome-headline')).toHaveTextContent('Every declared scenario passed.');
    expect(screen.getByTestId('scope-statement')).toHaveTextContent('only the 2 declared scenarios');
  });

  it('a phase with 0 of 0 is not 100%', () => {
    expect(toPhaseView('verification', { done: 0, total: 0 }).percent).toBeNull();
  });
});

describe('workspace integration', () => {
  beforeEach(() => {
    localStorage.clear();
    window.location.hash = '';
  });
  afterEach(() => {
    window.location.hash = '';
  });

  it('delivered project with 0 verified shows "Delivered — not verified" in header and overview (estimated fallback)', async () => {
    window.location.hash = '#/projects/c1/overview';
    const delivered = { ...demoCase, status: 'delivered' };
    const { store } = makeStore(caseRoutes({ '/cases/c1': delivered, '/cases': [delivered] }));
    const { container } = render(<App store={store} />);
    const panel = await screen.findByTestId('outcome');
    await waitFor(() => expect(within(screen.getByTestId('project-title').closest('.ws-title') as HTMLElement).getByTestId('outcome-chip')).toHaveTextContent('Delivered — not verified'));
    expect(panel).toHaveAttribute('data-outcome', 'delivered');
    expect(container.querySelector('.bar.verify.full')).toBeNull();
    store.stop();
  });

  it('prefers the controller outcome and shows its scenario counts', async () => {
    window.location.hash = '#/projects/c1/overview';
    const outcome = deriveOutcome({ ...base, scenariosDeclared: 8, scenarioVerdicts: [...pass(5), 'fail', 'fail'], built: true, delivered: true, pipelineDone: 9, pipelineTotal: 9 });
    const delivered = { ...demoCase, status: 'delivered', outcome: { ...outcome, source: 'controller' } };
    const { store } = makeStore(caseRoutes({ '/cases/c1': delivered, '/cases': [delivered] }));
    render(<App store={store} />);
    expect(await screen.findByTestId('verified-text')).toHaveTextContent('Behavior verified: 5 of 8 scenarios');
    expect(screen.getByTestId('verification-counts')).toHaveTextContent('Failed2');
    expect(screen.getByTestId('verification-counts')).toHaveTextContent('Not run (untested)1');
    expect(screen.getByTestId('outcome')).toHaveAttribute('data-outcome', 'partially_matched');
    store.stop();
  });
});
