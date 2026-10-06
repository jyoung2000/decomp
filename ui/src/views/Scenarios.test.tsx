import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { emptyScenarioForm, formToBody, validateScenarioForm } from '../lib/scenarios';
import { caseRoutes, demoCase, makeStore } from '../test-utils';

const PERMISSION = 'Original execution needs your permission: Rebuild Studio wants to run pecli.exe to record its behaviour. The network is NOT blocked and the program can still read your files.';
const consent = (allowed: boolean, over: Record<string, unknown> = {}) => ({ allowed, at: allowed ? '2026-02-03T10:00:00Z' : null, via: allowed ? 'create_case' : null, history: [], ...over });
const scenario = (over: Record<string, unknown> = {}) => ({
  scenario_id: 'usc_1', case_id: 'c1', title: 'Add then read back', feature_id: null, steps: [{ args: ['add', '{work}/s.dat', 'k', 'two words'], stdin: '' }], compare: { exit_code: true, output: true, files: false },
  normalize: { line_endings: true, trailing_spaces: false, trim: false, ignore_timestamps: false }, timeout: 60, status: 'no_baseline', recorded_at: null, recorded_evidence: null, updated_at: '2026-01-01T00:00:00Z', ...over,
});
const counts = (over: Record<string, number> = {}) => ({ declared: 0, no_baseline: 0, changed: 0, recorded: 0, passed: 0, failed: 0, not_run: 0, ...over });
const list = (rows: unknown[], c: ReturnType<typeof consent>, cn = counts({ declared: rows.length, no_baseline: rows.length })) => ({ scenarios: rows, counts: cn, consent: c });
const isolation = { platform: 'win32', network_default: 'open: not blocked', network_blocking: 'not available without administrator rights', default_mode: 'low' };
const blockedJob = { job_id: 'j1', case_id: 'c1', stage: 'capture_original', title: 'Capture original behaviour', state: 'blocked', attempt: 1, progress: {}, blocker: PERMISSION, heartbeat_at: null };

beforeEach(() => {
  localStorage.clear();
});

describe('consent card', () => {
  it('banner shows when the plan needs the original: explains protection truthfully; Grant records consent', async () => {
    window.location.hash = '#/projects/c1/overview';
    let state = consent(false);
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/jobs': [blockedJob], '/isolation': isolation,
      '/cases/c1/consent/original-execution': () => state,
      'PUT /cases/c1/consent/original-execution': (b: unknown) => (state = consent((b as { allow: boolean }).allow, { via: 'api' })),
    }));
    render(<App store={store} />);
    const banner = await screen.findByTestId('consent-missing');
    expect(banner).toHaveTextContent('has not been allowed to run the original');
    expect(screen.getByTestId('consent-keep-off')).toBeEnabled();
    // the blocked job row shows the permission message with its own Grant button
    const blockers = await screen.findByTestId('blockers');
    expect(blockers).toHaveTextContent('Original execution needs your permission');
    expect(within(blockers).getByTestId('blocked-grant')).toBeInTheDocument();
    await userEvent.click(within(banner).getByTestId('consent-grant'));
    const dlg = await screen.findByRole('dialog');
    expect(within(dlg).getByTestId('network-truth')).toHaveTextContent('not blocked');
    expect(dlg).toHaveTextContent('reduced-rights Windows process');
    expect(dlg).toHaveTextContent('can still read files your account can read');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Allow for this project' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path.endsWith('/consent/original-execution'))?.body).toMatchObject({ allow: true }));
    await waitFor(() => expect(screen.queryByTestId('consent-missing')).not.toBeInTheDocument());
    store.stop();
  });

  it('Keep it off hides the banner without calling the controller', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store, calls } = makeStore(caseRoutes({ '/cases/c1/jobs': [blockedJob], '/cases/c1/consent/original-execution': consent(false) }));
    render(<App store={store} />);
    await userEvent.click(await screen.findByTestId('consent-keep-off'));
    expect(screen.queryByTestId('consent-missing')).not.toBeInTheDocument();
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
    store.stop();
  });

  it('no banner when the plan does not need the original', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store } = makeStore(caseRoutes({ '/cases/c1/consent/original-execution': consent(false) }));
    render(<App store={store} />);
    await screen.findByTestId('overview');
    expect(screen.queryByTestId('consent-missing')).not.toBeInTheDocument();
    store.stop();
  });

  it('granted state says when and how it was given and offers Revoke', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/scenarios': list([], consent(true, { history: [{ allowed: true, at: '2026-02-03T10:00:00Z', via: 'create_case', note: 'execute_original enabled at creation' }] })),
      '/cases/c1/consent/original-execution': consent(true, { history: [{ allowed: true, at: '2026-02-03T10:00:00Z', via: 'create_case', note: 'execute_original enabled at creation' }] }),
      'PUT /cases/c1/consent/original-execution': consent(false),
    }));
    render(<App store={store} />);
    const card = await screen.findByTestId('consent-granted');
    expect(card).toHaveTextContent('You allowed this when the project was created');
    expect(card).toHaveTextContent(new Date('2026-02-03T10:00:00Z').toLocaleString());
    await userEvent.click(within(card).getByTestId('consent-revoke'));
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke permission' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toMatchObject({ allow: false }));
    store.stop();
  });
});

describe('scenario form validation (pure)', () => {
  it('requires a title, a step with balanced quotes, something to compare and a sane time limit', () => {
    const f = emptyScenarioForm();
    expect(validateScenarioForm(f).map((e) => e.field)).toEqual(['title']);
    const bad = { ...f, title: 'x', steps: [{ args: 'add "oops', stdin: '' }], exit_code: false, output: false, files: false, timeout: '0' };
    expect(validateScenarioForm(bad).map((e) => e.field)).toEqual(['args_0', 'compare', 'timeout']);
    expect(validateScenarioForm({ ...f, title: 'ok' })).toEqual([]);
  });
  it('turns the form into the API body (quotes honoured)', () => {
    const b = formToBody({ ...emptyScenarioForm(), title: ' T ', steps: [{ args: 'add f "two words"', stdin: 'q\n' }], files: true, ignore_timestamps: true });
    expect(b).toEqual({ title: 'T', feature_id: null, steps: [{ args: ['add', 'f', 'two words'], stdin: 'q\n' }], compare: { exit_code: true, output: true, files: true },
      normalize: { line_endings: true, trailing_spaces: false, trim: false, ignore_timestamps: true }, timeout: 60 });
  });
});

describe('Scenarios tab', () => {
  it('explains what a scenario is, with an example, and every control has a label', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const { store } = makeStore(caseRoutes({ '/cases/c1/scenarios': list([], consent(false)), '/cases/c1/consent/original-execution': consent(false) }));
    render(<App store={store} />);
    const help = await screen.findByTestId('scenario-help');
    expect(help).toHaveTextContent('A scenario is one thing you want the rebuilt program to keep doing exactly like the original');
    expect(help).toHaveTextContent('Example:');
    await userEvent.click(screen.getByTestId('scenario-add'));
    const form = await screen.findByTestId('scenario-form');
    for (const name of [/^Title/, /^Feature this checks/, /Command-line arguments \(step 1\)/, /What to type into the program \(step 1\)/, /Exit code/, /Printed text/, /Files it writes/, /Line-ending differences/, /Dates and times/, /Time limit per run/]) {
      expect(within(form).getByLabelText(name)).toBeInTheDocument();
    }
    store.stop();
  });

  it('shows validation errors instead of saving, then saves a valid scenario', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/scenarios': list([], consent(true)), '/cases/c1/consent/original-execution': consent(true),
      'POST /cases/c1/scenarios': scenario(),
    }));
    render(<App store={store} />);
    await userEvent.click(await screen.findByTestId('scenario-add'));
    await userEvent.click(screen.getByTestId('scenario-save'));
    expect(await screen.findByTestId('scenario-errors')).toHaveTextContent('The scenario has no title.');
    expect(screen.getByLabelText(/^Title/)).toHaveAttribute('aria-invalid', 'true');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    await userEvent.type(screen.getByLabelText(/^Title/), 'Add then read back');
    await userEvent.type(screen.getByLabelText(/Command-line arguments \(step 1\)/), 'add {{work}/s.dat k "two words"');
    await userEvent.type(screen.getByLabelText(/What to type into the program \(step 1\)/), 'hello');
    await userEvent.click(screen.getByTestId('scenario-save'));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/cases/c1/scenarios')?.body).toMatchObject({
      title: 'Add then read back', steps: [{ args: ['add', '{work}/s.dat', 'k', 'two words'], stdin: 'hello' }], compare: { exit_code: true, output: true, files: false } }));
    store.stop();
  });

  it('record without consent explains the permission first and records nothing if declined', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/scenarios': list([scenario()], consent(false)), '/cases/c1/consent/original-execution': consent(false), '/isolation': isolation,
    }));
    render(<App store={store} />);
    const rec = await screen.findByTestId('scenario-record');
    await waitFor(() => expect(rec).toBeEnabled());
    expect(screen.getByTestId('scenario-status')).toHaveTextContent('No baseline yet');
    await userEvent.click(rec);
    const dlg = await screen.findByRole('dialog');
    expect(dlg).toHaveTextContent('needs your permission');
    expect(within(dlg).getByTestId('network-truth')).toHaveTextContent('not blocked');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Cancel' }));
    expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/record'))).toBe(false);
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
    store.stop();
  });

  it('shows the controller’s permission message if the server still refuses', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const refuse = new Response(JSON.stringify({ error: { code: 'original_execution_not_permitted', message: PERMISSION, affected: 'running the original program', next_action: 'Grant permission for this project.' } }), { status: 409 });
    const { store } = makeStore(caseRoutes({
      '/cases/c1/scenarios': list([scenario()], consent(true)), '/cases/c1/consent/original-execution': consent(true), 'POST /cases/c1/scenarios/record': refuse,
    }));
    render(<App store={store} />);
    const rec = await screen.findByTestId('scenario-record');
    await waitFor(() => expect(rec).toBeEnabled());
    await userEvent.click(rec);
    const alert = await screen.findByText(/Original execution needs your permission/);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Grant permission for this project.');
    store.stop();
  });

  it('allow-and-record grants consent then records; the success message names the revision', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    let allowed = false;
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/scenarios': () => list([scenario()], consent(allowed)), '/cases/c1/consent/original-execution': () => consent(allowed),
      'PUT /cases/c1/consent/original-execution': () => ((allowed = true), consent(true)),
      'POST /cases/c1/scenarios/record': () => ({ ...list([scenario({ status: 'not_run' })], consent(true), counts({ declared: 1, recorded: 1, not_run: 1 })), evidence_id: 'ev1', baseline_revision: 2, recorded: ['usc_1'], scenarios_in_baseline: 2 }),
    }));
    render(<App store={store} />);
    const rec = await screen.findByTestId('scenario-record');
    await waitFor(() => expect(rec).toBeEnabled());
    await userEvent.click(rec);
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Allow and record' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/cases/c1/scenarios/record')?.body).toEqual({ scenario_ids: ['usc_1'] }));
    expect(calls.findIndex((c) => c.method === 'PUT')).toBeLessThan(calls.findIndex((c) => c.path.endsWith('/record')));
    expect(await screen.findByText(/baseline revision 2/)).toBeInTheDocument();
    store.stop();
  });

  it('counts match the Outcome panel and every disabled control is explained', async () => {
    window.location.hash = '#/projects/c1/scenarios';
    const outcome = { state: 'partially_matched', label: 'Partially matched', can_claim_complete: false, scaffold_only: false,
      verification: { state: 'partially_matched', verdict: 'partial', declared: 3, passed: 1, failed: 1, untested: 1, stale: false, text: '', scope: 'declared_scenarios_only' },
      pipeline: { recovered: true, scaffolded: false, built: true, delivered: false, steps_done: 3, steps_total: 5, text: '' },
      coverage: { features_total: 0, features_without_oracle: 0, features_without_oracle_titles: [], unsupported_modules: 0, unsupported_modules_titles: [], unknown_scope: false }, outstanding: [], scope_statement: '' };
    const rows = [scenario({ scenario_id: 'a', status: 'passed' }), scenario({ scenario_id: 'b', title: 'B', status: 'failed' })];
    const { store } = makeStore(caseRoutes({
      '/cases/c1': { ...demoCase, outcome },
      '/cases/c1/scenarios': list(rows, consent(true), counts({ declared: 2, recorded: 2, passed: 1, failed: 1 })), '/cases/c1/consent/original-execution': consent(true),
    }));
    render(<App store={store} />);
    const box = await screen.findByTestId('scenario-counts');
    await waitFor(() => expect(within(box).getByTestId('count-declared')).toHaveTextContent('2'));
    expect(within(box).getByTestId('count-passed')).toHaveTextContent('1');
    expect(within(box).getByTestId('count-failed')).toHaveTextContent('1');
    await waitFor(() => expect(within(box).getByTestId('outcome-counts')).toHaveTextContent('3 declared, 1 passed, 1 failed, 1 not run'));
    expect(screen.getAllByTestId('scenario-status').map((n) => n.textContent)).toEqual(['Passed', 'Failed']);
    // everything recorded: Record is disabled and says why
    const rec = screen.getByTestId('scenario-record');
    expect(rec).toBeDisabled();
    const why = document.getElementById(rec.getAttribute('aria-describedby') ?? '');
    expect(why?.textContent).toMatch(/already has recorded behaviour/);
    for (const b of within(screen.getByTestId('scenarios')).getAllByRole('button').filter((x) => (x as HTMLButtonElement).disabled)) {
      expect(b.getAttribute('aria-describedby'), `${b.textContent} has no explanation`).toBeTruthy();
    }
    store.stop();
  });
});

describe('New project: run the original?', () => {
  it('defaults to No, tells the truth about protection, and sends/confirms the consent choice', async () => {
    window.location.hash = '#/new';
    let rec = consent(false);
    const { store, calls } = makeStore(caseRoutes({
      '/isolation': isolation,
      'POST /cases': () => ({ ...demoCase, case_id: 'c9' }),
      '/cases/c9/consent/original-execution': () => rec,
      'PUT /cases/c9/consent/original-execution': () => (rec = consent(true, { via: 'api' })),
    }));
    render(<App store={store} />);
    const no = await screen.findByTestId('run-original-no');
    expect(no).toBeChecked();
    expect(screen.getByTestId('run-original-yes')).not.toBeChecked();
    const summary = screen.getByTestId('original-run-summary');
    expect(summary).toHaveTextContent('Why:');
    expect(summary).toHaveTextContent('not blocked');
    expect(summary).toHaveTextContent('reduced-rights Windows process');
    await userEvent.click(screen.getByTestId('run-original-yes'));
    await userEvent.type(screen.getByLabelText(/^Name/), 'P');
    await userEvent.type(screen.getByLabelText(/Source folder/), 'C:\\src');
    await userEvent.type(screen.getByLabelText(/Output folder/), 'D:\\out');
    await userEvent.type(screen.getByLabelText(/Program to run/), 'bin\\app.exe');
    await userEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/cases')?.body).toMatchObject({ launch_profile: { execute_original: true } }));
    // the controller had not recorded consent: the form fixes it through the consent API
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/cases/c9/consent/original-execution')?.body).toMatchObject({ allow: true }));
    store.stop();
  });
});
