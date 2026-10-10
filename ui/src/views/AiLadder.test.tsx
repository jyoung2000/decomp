import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, demoCase, makeStore, type Routes } from '../test-utils';

const NOW = new Date().toISOString();
const avail = { state: 'available', probed_at: NOW, detail: null };
const qwen = { connection_id: 'conn_local', connection_label: 'Ollama (this PC)', provider: 'local', model: 'qwen2.5:14b', locality: 'local', availability: avail, capabilities: { vision: false, tools: true, context_window: 32768 }, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true };
const gpt = { connection_id: 'conn_openai', connection_label: 'OpenAI', provider: 'openai', model: 'gpt-demo-small', locality: 'cloud', availability: avail, capabilities: { vision: true, tools: true, context_window: 128000 }, price: { known: true, input_per_mtok: 0.15, output_per_mtok: 0.6 }, free: false };
const claude = { connection_id: 'conn_h', connection_label: 'Claude handoff', provider: 'anthropic', model: 'claude-demo', locality: 'cloud', availability: { state: 'unprobed', probed_at: null }, capabilities: null, price: { known: false }, free: false };
const conns = [
  { connection_id: 'conn_local', provider: 'local', label: 'Ollama (this PC)', endpoint: 'http://127.0.0.1:11434/v1', auth_mode: 'local', models: ['qwen2.5:14b'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW },
  { connection_id: 'conn_openai', provider: 'openai', label: 'OpenAI', endpoint: 'https://api.openai.com/v1', auth_mode: 'api_key', models: ['gpt-demo-small'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW },
];
const tasks = ['implementation', 'repair', 'naming', 'visual_review', 'verification_assist', 'knowledge'];
const ladderOf = (interp: unknown[]) => ({ config_revision: 7, tasks: Object.fromEntries(tasks.map((t) => [t, { entries: t === 'implementation' ? interp : [], rationale: t === 'implementation' ? 'user' : 'auto' }])) });

function ladderRoutes(over: Routes = {}) {
  let interp: unknown[] = [qwen, gpt, claude];
  const routes: Routes = {
    '/connections': conns,
    '/routes': [],
    '/ai/ladder': () => ladderOf(interp),
    'PUT /ai/ladder/implementation': (b: unknown) => {
      const es = (b as { entries: { connection_id: string; model: string }[] }).entries;
      interp = es.map((e) => [qwen, gpt, claude].find((x) => x.model === e.model) ?? { ...e, locality: 'cloud' });
      return { config_revision: 8 };
    },
    '/ai/models': [qwen, gpt, claude],
    '/hermes/status': { paired: false, diagnostics: [] },
    ...over,
  };
  return routes;
}

beforeEach(() => {
  localStorage.clear();
});

async function openConnections(over: Routes = {}) {
  window.location.hash = '#/connections';
  const m = makeStore(caseRoutes(ladderRoutes(over)));
  render(<App store={m.store} />);
  const sec = await screen.findByTestId('ladder-section');
  await within(sec).findByTestId('lad-implementation-ladder');
  return { ...m, sec };
}

describe('model ladder editor', () => {
  it('shows version, numbered rungs, locality, availability, capabilities and price', async () => {
    const { store, sec } = await openConnections();
    expect(within(sec).getByTestId('ladder-version')).toHaveTextContent('Ladder version 7');
    const list = within(sec).getByTestId('lad-implementation-ladder');
    const rows = within(list).getAllByRole('listitem').filter((r) => r.getAttribute('data-testid'));
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent('qwen2.5:14b');
    expect(rows[0]).toHaveTextContent('Local · runs on this PC');
    expect(rows[0]).toHaveTextContent('Free');
    expect(rows[0]).toHaveTextContent('32k context');
    expect(rows[1]).toHaveTextContent('128k context');
    expect(rows[0]).toHaveTextContent('Available · checked');
    expect(rows[1]).toHaveTextContent('Cloud');
    expect(rows[1]).toHaveTextContent('$0.150 in / $0.600 out per Mtok');
    expect(rows[1]).toHaveTextContent('Vision');
    expect(rows[2]).toHaveTextContent('Unknown price — needs approval');
    expect(rows[2]).toHaveTextContent('Capabilities not reported');
    expect(rows[2]).toHaveTextContent('Not checked yet');
    store.stop();
  });

  it('reorders and removes with the keyboard, then saves the new order', async () => {
    const user = userEvent.setup();
    const { store, sec, calls } = await openConnections();
    const save = within(sec).getByRole('button', { name: /Save ladder for Implementation/ });
    expect(save).toBeDisabled();
    expect(save.getAttribute('aria-describedby')).toBeTruthy();
    // button + Enter
    const down = within(sec).getByRole('button', { name: 'Move qwen2.5:14b down' });
    down.focus();
    await user.keyboard('{Enter}');
    let rows = within(sec).getByTestId('lad-implementation-ladder').querySelectorAll('[data-testid^="lad-implementation-rung"]');
    expect(rows[0]).toHaveTextContent('gpt-demo-small');
    expect(rows[1]).toHaveTextContent('qwen2.5:14b');
    // focus stays on the moved rung so repeated moves work (moved after the re-render, so wait for it: CI runners are slower)
    await waitFor(() => expect(within(sec).getByTestId('lad-implementation-ladder').querySelectorAll('[data-testid^="lad-implementation-rung"]')[1]).toHaveFocus(), { timeout: 5000 });
    // Alt+ArrowUp on the focused row moves it back
    await user.keyboard('{Alt>}{ArrowUp}{/Alt}');
    rows = within(sec).getByTestId('lad-implementation-ladder').querySelectorAll('[data-testid^="lad-implementation-rung"]');
    expect(rows[0]).toHaveTextContent('qwen2.5:14b');
    // first row cannot move up and says why
    const up = within(sec).getByRole('button', { name: 'Move qwen2.5:14b up' });
    expect(up).toBeDisabled();
    expect(document.getElementById(up.getAttribute('aria-describedby')!)).toHaveTextContent('Already first');
    // remove the unknown-price one with the keyboard, move gpt first, save
    const rm = within(sec).getByRole('button', { name: 'Remove claude-demo' });
    rm.focus();
    await user.keyboard('{Enter}');
    expect(within(sec).queryByText('claude-demo')).not.toBeInTheDocument();
    within(sec).getByRole('button', { name: 'Move gpt-demo-small up' }).focus();
    await user.keyboard(' ');
    await user.click(within(sec).getByRole('button', { name: /Save ladder for Implementation/ }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/ai/ladder/implementation')?.body).toEqual({ entries: [{ connection_id: 'conn_openai', model: 'gpt-demo-small' }, { connection_id: 'conn_local', model: 'qwen2.5:14b' }] }));
    store.stop();
  }, 20_000);   // keyboard reorders + async focus moves are slow on hosted CI runners

  it('model picker searches the catalog, filters local/cloud and keeps manual model-ID entry', async () => {
    const user = userEvent.setup();
    const { store, sec, calls } = await openConnections({ '/ai/ladder': () => ladderOf([qwen]), '/ai/models': () => [qwen, gpt, claude] });
    await user.click(within(sec).getByRole('button', { name: /Add model to Implementation/ }));
    const dlg = await screen.findByRole('dialog');
    const box = within(dlg).getByRole('combobox', { name: 'Search models' });
    await waitFor(() => expect(within(within(dlg).getByRole('listbox')).getAllByRole('option').length).toBe(3));
    await user.type(box, 'gpt');
    await waitFor(() => expect(calls.some((c) => c.path.startsWith('/ai/models') && c.path.includes('q=gpt') && c.path.includes('task=implementation'))).toBe(true));
    // local-only filter hides cloud models and the already-added one is flagged
    await user.selectOptions(within(dlg).getByLabelText('Where it runs'), 'local');
    await waitFor(() => expect(within(within(dlg).getByRole('listbox')).getAllByRole('option')).toHaveLength(1));
    expect(within(within(dlg).getByRole('listbox')).getByRole('option')).toHaveTextContent('Already in this ladder');
    await user.selectOptions(within(dlg).getByLabelText('Where it runs'), 'any');
    // pick with the keyboard: ArrowDown to the 2nd option, Enter
    box.focus();
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(within(sec).getByTestId('lad-implementation-ladder')).toHaveTextContent('gpt-demo-small');

    // manual entry
    await user.click(within(sec).getByRole('button', { name: /Add model to Implementation/ }));
    const d2 = await screen.findByRole('dialog');
    const add = within(d2).getByRole('button', { name: 'Add this model' });
    expect(add).toBeDisabled();
    expect(document.getElementById(add.getAttribute('aria-describedby')!)).toHaveTextContent('Choose a connection first');
    await user.selectOptions(within(d2).getByLabelText('Connection'), 'conn_local');
    await user.type(within(d2).getByLabelText('Model ID'), 'my-custom:7b');
    await user.click(add);
    const row = await within(sec).findByText('my-custom:7b');
    const li = row.closest('li')!;
    expect(li).toHaveTextContent('Local');
    expect(li).toHaveTextContent('Capabilities not reported');
    store.stop();
  });

  it('manual entry still works when discovery is unavailable', async () => {
    const user = userEvent.setup();
    const { store, sec } = await openConnections({ '/ai/models': new Response(JSON.stringify({ error: { code: 'x', message: 'no discovery' } }), { status: 500 }) });
    await user.click(within(sec).getByRole('button', { name: /Add model to Repair/ }));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText('Model discovery is not available');
    await user.selectOptions(within(dlg).getByLabelText('Connection'), 'conn_openai');
    await user.type(within(dlg).getByLabelText('Model ID'), 'gpt-9');
    await user.click(within(dlg).getByRole('button', { name: 'Add this model' }));
    expect(await within(sec).findByText('gpt-9')).toBeInTheDocument();
    expect(within(sec).getByTestId('lad-repair-ladder')).toHaveTextContent('Unknown price — needs approval');
    store.stop();
  });

  it('presets preview the resulting ladders and warnings before anything is applied', async () => {
    const user = userEvent.setup();
    const preview = { preset: 'all_local', applied: false, config_revision: 7, tasks: { ...Object.fromEntries(tasks.map((t) => [t, { entries: [qwen] }])), visual_review: { entries: [] } }, warnings: ['No local model supports images; visual review has no route.'] };
    const { store, sec, calls } = await openConnections({ 'POST /ai/ladder/preset': () => preview });
    for (const n of ['Local first', 'Cloud first', 'All local', 'All cloud', 'No AI']) expect(within(sec).getByRole('button', { name: n })).toBeInTheDocument();
    await user.click(within(sec).getByRole('button', { name: 'All local' }));
    const pv = await within(sec).findByTestId('preset-preview');
    expect(calls.filter((c) => c.path === '/ai/ladder/preset')).toEqual([{ method: 'POST', path: '/ai/ladder/preset', body: { preset: 'all_local', apply: false } }]);
    expect(pv).toHaveTextContent('Preview: All local (not applied yet)');
    expect(pv).toHaveTextContent('Implementation: 1. qwen2.5:14b Local');
    expect(pv).toHaveTextContent('Visual review: no model');
    expect(within(pv).getByRole('alert')).toHaveTextContent('No local model supports images');
    await user.click(within(pv).getByRole('button', { name: /Apply/ }));
    await waitFor(() => expect(calls.filter((c) => c.path === '/ai/ladder/preset').map((c) => (c.body as { apply: boolean }).apply)).toEqual([false, true]));
    await waitFor(() => expect(within(sec).queryByTestId('preset-preview')).not.toBeInTheDocument());
    store.stop();
  });
});

describe('project AI policy', () => {
  it('New project: No AI is the default and says what it still does and what becomes a scaffold', async () => {
    window.location.hash = '#/new';
    const { store } = makeStore(caseRoutes({ '/connections': conns }));
    render(<App store={store} />);
    const note = await screen.findByTestId('no-ai-explainer');
    expect(note).toHaveTextContent('No AI never contacts any AI service');
    expect(note).toHaveTextContent('detecting and inventorying the original');
    expect(note).toHaveTextContent('recovering code');
    expect(note).toHaveTextContent('collecting evidence');
    expect(note).toHaveTextContent('editable');
    expect(note).toHaveTextContent('deterministic web ports and comparisons');
    expect(note).toHaveTextContent('native, managed (.NET/Java) and game remakes');
    expect(note).toHaveTextContent('scaffolds or are marked blocked');
    // choosing the app ladder reveals locality, budget and the unknown-pricing toggle with its explanation
    await userEvent.click(screen.getByRole('radio', { name: /Use the app ladder/ }));
    expect(screen.queryByTestId('no-ai-explainer')).not.toBeInTheDocument();
    const approve = screen.getByRole('checkbox', { name: 'Allow models whose price is unknown' });
    expect(document.getElementById(approve.getAttribute('aria-describedby')!)).toHaveTextContent('skipped');
    expect(screen.getByLabelText(/Budget per job/)).toHaveAttribute('aria-describedby');
    store.stop();
  });

  it('Workspace AI settings load the policy, save changes, and explain paused behaviour', async () => {
    window.location.hash = '#/projects/c1/ai';
    const user = userEvent.setup();
    let saved: unknown = null;
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1': { ...demoCase, status: 'paused' },
      '/cases/c1/ai-policy': () => saved ?? { mode: 'no_ai' },
      'PUT /cases/c1/ai-policy': (b: unknown) => (saved = b),
      '/cases/c1/ai/activity': [],
      '/connections': conns,
      '/ai/ladder': ladderOf([qwen]),
    }));
    render(<App store={store} />);
    const panel = await screen.findByTestId('ai-settings');
    expect(await within(panel).findByTestId('no-ai-explainer')).toBeInTheDocument();
    expect(within(panel).getByTestId('paused-note')).toHaveTextContent('Changes apply to work that has not started yet');
    const saveBtn = within(panel).getByRole('button', { name: 'Save AI settings' });
    expect(saveBtn).toBeDisabled();
    expect(saveBtn.getAttribute('aria-describedby')).toBeTruthy();
    await user.click(within(panel).getByRole('radio', { name: /Use the app ladder/ }));
    await user.selectOptions(within(panel).getByLabelText('Where models may run'), 'local_only');
    await user.click(within(panel).getByRole('button', { name: 'Save AI settings' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/cases/c1/ai-policy')?.body).toMatchObject({ mode: 'inherit', locality: 'local_only', approve_unknown_pricing: false }));
    store.stop();
  });
});

const planAi = (over: Record<string, unknown> = {}) => ({
  task: 'repair', primary: { provider: 'local', model: 'qwen2.5:14b', locality: 'local' },
  fallbacks: [{ provider: 'openai', model: 'gpt-demo-small', locality: 'cloud' }, { provider: 'anthropic', model: 'claude-demo', locality: 'cloud' }],
  rationale: 'Local first: free and private', expected_cost: { min_usd: 0, max_usd: 0.05, known: true }, budget_usd: 0.5, runs_without_ai: false, without_ai: 'Becomes a scaffold you can edit.', ...over,
});
const item = (over: Record<string, unknown>) => ({ parent_id: null, outcome: '', kind: 'milestone', status: 'queued', owner: null, depends_on: [], acceptance: [], evidence_ids: [], files: [], preview_id: null, blockers: [], feature_id: null, job_ids: [], sort_order: 1, ...over });

describe('plan AI chips', () => {
  it('shows task, primary + locality, fallbacks, rationale, cost, works-without-AI and origin; expands to ordered fallbacks', async () => {
    window.location.hash = '#/projects/c1/plan';
    const user = userEvent.setup();
    const items = [
      item({ item_id: 'M1', title: 'Inventory', origin: 'deterministic' }),
      item({ item_id: 'M2', title: 'Recover', sort_order: 2, origin: 'model_proposed', ai: planAi() }),
      item({ item_id: 'M3', title: 'Unpriced', sort_order: 3, origin: 'verifier_decided', ai: planAi({ rationale: null, expected_cost: { unknown_price: true }, runs_without_ai: true, fallbacks: [] }) }),
    ];
    const { store } = makeStore(caseRoutes({ '/cases/c1/plan': { revision: 3, items, unknown_scope: [], progress: {}, eta: null } }));
    render(<App store={store} />);
    const chip = await screen.findByTestId('ai-chip-M2');
    expect(chip).toHaveTextContent('Repair');
    expect(chip).toHaveTextContent('qwen2.5:14b');
    expect(chip).toHaveTextContent('Local');
    expect(chip).toHaveTextContent('+2 fallbacks');
    expect(chip).toHaveTextContent('Local first: free and private');
    expect(chip).toHaveTextContent('up to $0.050');
    expect(chip).toHaveTextContent('Needs AI');
    const unpriced = screen.getByTestId('ai-chip-M3');
    expect(unpriced).toHaveTextContent('Unknown price');
    expect(unpriced).toHaveTextContent('Auto');
    expect(unpriced).toHaveTextContent('Works without AI');
    expect(within(screen.getByTestId('plan-item-M1')).getByText('Deterministic')).toBeInTheDocument();
    expect(within(screen.getByTestId('plan-item-M2')).getByText('Model proposed')).toBeInTheDocument();
    expect(within(screen.getByTestId('plan-item-M3')).getByText('Verifier decided')).toBeInTheDocument();
    expect(screen.queryByTestId('ai-chip-M1')).not.toBeInTheDocument();
    await user.click(within(chip).getByRole('button', { name: 'AI details' }));
    const items2 = within(chip).getAllByRole('listitem').map((l) => l.textContent);
    expect(items2).toEqual(['Primary: qwen2.5:14b (Local)', 'Fallback 1: gpt-demo-small (Cloud)', 'Fallback 2: claude-demo (Cloud)']);
    expect(chip).toHaveTextContent('Becomes a scaffold you can edit.');
    store.stop();
  });
});

describe('AI activity feed', () => {
  const act1 = { at: '2026-01-01T10:00:00Z', kind: 'attempt', text: 'qwen2.5:14b is not available (model not found); trying gpt-demo-small', plan_item_id: 'M2', job_id: 'j1', task: 'implementation', provider: 'local', model: 'qwen2.5:14b', locality: 'local', outcome: 'model_unavailable', fallback_reason: 'model not found', config_revision: 6, origin: 'model_proposed', prompt: 'LEAK-INITIAL-PROMPT' };

  it('renders initial and live lines grouped by plan item with fallback text, cost, ladder version and links; never raw prompts', async () => {
    window.location.hash = '#/projects/c1/ai';
    const { store, sockets } = makeStore(caseRoutes({
      '/cases/c1/ai-policy': { mode: 'no_ai' }, '/cases/c1/ai/activity': [act1], '/connections': conns, '/ai/ladder': ladderOf([qwen]),
      '/cases/c1/plan': { revision: 1, items: [item({ item_id: 'M2', title: 'Recover logic' })], unknown_scope: [], progress: {}, eta: null },
    }));
    render(<App store={store} />);
    const feed = await screen.findByTestId('ai-activity');
    const group = await within(feed).findByTestId('activity-group-M2');
    expect(group).toHaveTextContent('Recover logic');
    expect(group).toHaveTextContent('trying gpt-demo-small');
    expect(within(group).getByTestId('fallback-reason')).toHaveTextContent('Fell back to the next model: model not found');
    expect(group).toHaveTextContent('Model not available');
    expect(group).toHaveTextContent('Ladder version 6');
    await waitFor(() => expect(sockets.length).toBe(1));
    act(() => {
      sockets[0].emit({ seq: 1, ts: '2026-01-01T10:00:05Z', case_id: 'c1', job_id: 'j1', kind: 'ai.activity', payload: {
        at: '2026-01-01T10:00:05Z', kind: 'attempt', text: 'gpt-demo-small proposed 3 files (candidate r2)', plan_item_id: 'M2', job_id: 'j1', candidate_id: 'cand_r2', evidence_ids: ['ev_9'], task: 'repair', provider: 'openai', model: 'gpt-demo-small', locality: 'cloud',
        outcome: 'ok', tokens_in: 1200, tokens_out: 380, cost_usd: 0.003, cost_known: true, config_revision: 7, origin: 'model_proposed', prompt: 'LEAK-LIVE-PROMPT', raw_prompt: 'LEAK-RAW', messages: [{ role: 'user', content: 'LEAK-MSG' }],
      } });
      sockets[0].emit({ seq: 2, ts: '2026-01-01T10:00:06Z', case_id: 'c1', job_id: null, kind: 'ai.activity', payload: { at: '2026-01-01T10:00:06Z', text: 'Verifier: 6 of 8 declared scenarios passed', origin: 'verifier_decided', cost_usd: 0, cost_known: true, locality: 'local', model: 'qwen2.5:14b' } });
    });
    const live = await within(feed).findByText('gpt-demo-small proposed 3 files (candidate r2)');
    const row = live.closest('[data-testid="activity-row"]')!;
    expect(row).toHaveTextContent('1,200 in / 380 out tokens');
    expect(row).toHaveTextContent('$0.003');
    expect(row).toHaveTextContent('Cloud');
    expect(row).toHaveTextContent('Ladder version 7');
    expect(within(row as HTMLElement).getByRole('link', { name: 'Candidate cand_r2' })).toHaveAttribute('href', expect.stringContaining('cand_r2'));
    expect(within(row as HTMLElement).getByRole('link', { name: 'ev_9' })).toHaveAttribute('href', expect.stringContaining('evidence=ev_9'));
    expect(row.closest('[data-testid="activity-group-M2"]')).not.toBeNull();
    expect(within(feed).getByText('Verifier: 6 of 8 declared scenarios passed').closest('[data-testid="activity-group-general"]')).not.toBeNull();
    expect(within(feed).getByText('Verifier decided')).toBeInTheDocument();
    const html = document.body.innerHTML;
    for (const leak of ['LEAK-INITIAL-PROMPT', 'LEAK-LIVE-PROMPT', 'LEAK-RAW', 'LEAK-MSG']) expect(html).not.toContain(leak);
    store.stop();
  });

  it('while running offers Pause; while paused says changes apply to work not yet started and offers Resume', async () => {
    window.location.hash = '#/projects/c1/ai';
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1/ai-policy': { mode: 'inherit', budget_usd: 1 }, '/cases/c1/ai/activity': [], '/connections': conns, '/ai/ladder': ladderOf([qwen]),
      'POST /cases/c1/pause': { ok: true },
    }));
    render(<App store={store} />);
    const panel = await screen.findByTestId('ai-settings');
    await userEvent.click(within(panel).getByRole('button', { name: /Pause to change settings/ }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/cases/c1/pause')).toBe(true));
    store.stop();
  });
});
