import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, demoCase, makeStore, type Routes } from '../test-utils';

// R5 (docs/AI_LADDER.md section 10): the model scoreboard on Connections, the measured recommendation in the preset preview, and
// the review of model-proposed names / comments in a project's AI tab.
const NOW = new Date().toISOString();
const avail = { state: 'available', probed_at: NOW, detail: null };
const coder = { connection_id: 'conn_local', connection_label: 'Ollama (this PC)', provider: 'local', model: 'qwen2.5-coder:14b', locality: 'local', availability: avail, capabilities: null, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true };
const conns = [{ connection_id: 'conn_local', provider: 'local', label: 'Ollama (this PC)', endpoint: 'http://127.0.0.1:11434/v1', auth_mode: 'local', models: ['qwen2.5-coder:14b'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW }];
const tasks = ['implementation', 'repair', 'naming', 'visual_review', 'verification_assist', 'knowledge'];
const ladder = { config_revision: 2, tasks: Object.fromEntries(tasks.map((t) => [t, { entries: [], rationale: 'auto' }])) };
const board = {
  rows: 10,
  models: [
    { model: 'qwen2.5-coder:14b', provider: 'local', locality: 'local', ever_compiled: true, code_runs: 7, tasks: { repair: { runs: 7, ok: 7, compiled: 1, verified: 1, median_latency_ms: 412000 }, naming: { runs: 4, ok: 4, compiled: 0, verified: 0, truth_checked: 4, truth_match: 3, median_latency_ms: 9100, sources: ['benchmark'] } } },
    { model: 'deepseek-coder-v2:16b', provider: 'local', locality: 'local', ever_compiled: false, code_runs: 3, tasks: { implementation: { runs: 3, ok: 3, compiled: 0, verified: 0, median_latency_ms: 221000 } } },
  ],
};

beforeEach(() => localStorage.clear());

async function openConnections(over: Routes = {}) {
  window.location.hash = '#/connections';
  const m = makeStore(caseRoutes({ '/connections': conns, '/routes': [], '/ai/ladder': ladder, '/ai/models': [], '/hermes/status': { paired: false, diagnostics: [] }, ...over }));
  render(<App store={m.store} />);
  return m;
}

describe('model scoreboard (R5)', () => {
  it('shows what each model achieved here and flags models that never compiled', async () => {
    const { store } = await openConnections({ '/ai/scoreboard': board });
    const card = await screen.findByTestId('model-scoreboard');
    const coderRow = await within(card).findByTestId('score-qwen2.5-coder:14b');
    expect(coderRow).toHaveTextContent('Compiled code here');
    expect(coderRow).toHaveTextContent('Repair: 7 attempts, 1 compiled, 1 verified, median 412 s');
    expect(coderRow).toHaveTextContent('Naming: 4 runs, 4 usable, 3/4 matched the real names, median 9.1 s (includes benchmark runs)');
    const ds = within(card).getByTestId('score-deepseek-coder-v2:16b');
    expect(ds).toHaveTextContent('Never compiled');
    expect(ds).toHaveTextContent('Implementation: 3 attempts, 0 compiled, 0 verified');
    store.stop();
  });

  it('degrades to a plain note when the controller has no scoreboard', async () => {
    const { store } = await openConnections();
    expect(await screen.findByTestId('scoreboard-unavailable')).toHaveTextContent('not available');
    store.stop();
  });

  it('the preset preview says when its first model was measured on this PC', async () => {
    const user = userEvent.setup();
    const preview = {
      preset: 'all_local', applied: false, config_revision: 2,
      tasks: { implementation: { entries: [coder], recommendation: { model: 'qwen2.5-coder:14b', basis: 'measured', note: '7 code attempt(s) here: 1 compiled, 1 verified' } }, naming: { entries: [coder], recommendation: { model: 'qwen2.5-coder:14b', basis: 'heuristic', note: 'not measured here yet' } } },
      warnings: ['deepseek-coder-v2:16b was tried 3 time(s) for code here and never produced anything that compiles; it is kept but placed after models that did.'],
    };
    const { store } = await openConnections({ '/ai/scoreboard': board, 'POST /ai/ladder/preset': () => preview });
    const sec = await screen.findByTestId('ladder-section');
    await user.click(within(sec).getByRole('button', { name: 'All local' }));
    const pv = await within(sec).findByTestId('preset-preview');
    expect(within(pv).getByTestId('preset-rec-implementation')).toHaveTextContent('First: qwen2.5-coder:14b, measured on this PC (7 code attempt(s) here: 1 compiled, 1 verified)');
    expect(within(pv).getByTestId('preset-rec-naming')).toHaveTextContent('chosen by size and coder heuristic (not measured here yet)');
    expect(within(pv).getByRole('alert')).toHaveTextContent('never produced anything that compiles');
    store.stop();
  });
});

describe('AI names and comments (R5)', () => {
  const mod = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'x', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
  const proposals = [
    { kind: 'function', key: '0x140001190', local: null, value: 'parse_command_line', status: 'proposed', label: 'model-proposed', model: 'qwen2.5-coder:14b', analysis_name: 'fcn.140001190', replaced: null },
    { kind: 'comment', key: '0x140001190', local: null, value: 'Splits argv.', status: 'accepted', label: 'accepted by you', model: 'qwen2.5-coder:14b', replaced: null },
  ];
  const view = { revision: 3, proposals, open_proposals: 1, history: [{ evidence_id: 'e1', revision: 1, change: { action: 'rename', new: 'parse_command_line', label: 'model-proposed' } }, { evidence_id: 'e2', revision: 2, change: { action: 'comment' } }, { evidence_id: 'e3', revision: 3, change: { action: 'review', decision: 'accept' } }] };

  async function openAi(over: Routes = {}) {
    window.location.hash = '#/projects/c1/ai';
    const m = makeStore(caseRoutes({
      '/cases/c1': { ...demoCase, ai_policy: { mode: 'assisted' } }, '/cases/c1/ai-policy': { mode: 'assisted' }, '/cases/c1/ai/activity': [], '/connections': conns, '/ai/ladder': ladder,
      '/cases/c1/modules': [mod], '/cases/c1/modules/mod1/annotations': view, ...over,
    }));
    render(<App store={m.store} />);
    const card = await screen.findByTestId('naming-review');
    await within(card).findByTestId('proposal-list');
    return { ...m, card };
  }

  it('lists proposals with their label, the name they replace and accept / reject / roll back actions', async () => {
    const user = userEvent.setup();
    const { store, card, calls } = await openAi({
      'POST /cases/c1/modules/mod1/annotations/review': { revision: 4, proposals: [] },
      'POST /cases/c1/modules/mod1/annotations/rollback': { revision: 5 },
    });
    expect(within(card).getByTestId('naming-open-count')).toHaveTextContent('1 waiting for review');
    const fn = within(card).getByTestId('proposal-function-0x140001190');
    expect(fn).toHaveTextContent('Function 0x140001190: parse_command_line (was fcn.140001190)model-proposed');
    expect(within(card).getByTestId('proposal-comment-0x140001190')).toHaveTextContent('accepted by you');
    expect(within(within(card).getByTestId('proposal-comment-0x140001190')).queryByRole('button', { name: /Accept/ })).not.toBeInTheDocument();
    await user.click(within(fn).getByRole('button', { name: 'Accept Function 0x140001190 parse_command_line' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/cases/c1/modules/mod1/annotations/review')?.body).toEqual({ kind: 'function', key: '0x140001190', local: null, decision: 'accept' }));
    await user.click(within(fn).getByRole('button', { name: /Reject Function/ }));
    await waitFor(() => expect(calls.filter((c) => c.path === '/cases/c1/modules/mod1/annotations/review').map((c) => (c.body as { decision: string }).decision)).toEqual(['accept', 'reject']));
    await user.click(within(card).getByText(/History \(3 revisions\)/));
    await user.click(within(card).getByRole('button', { name: 'Roll back to revision 1' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/cases/c1/modules/mod1/annotations/rollback')?.body).toEqual({ revision: 1 }));
    store.stop();
  });

  it('starts AI naming for the chosen module and number of functions', async () => {
    const user = userEvent.setup();
    const { store, card, calls } = await openAi({ 'POST /cases/c1/modules/mod1/ai/naming': { job: { job_id: 'j1' }, note: 'model-proposed' } });
    await user.selectOptions(within(card).getByLabelText('How many functions to name'), '4');
    await user.click(within(card).getByRole('button', { name: 'Name functions with AI' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/cases/c1/modules/mod1/ai/naming')?.body).toEqual({ limit: 4 }));
    store.stop();
  });

  it('is hidden when the project has no native module', async () => {
    window.location.hash = '#/projects/c1/ai';
    const m = makeStore(caseRoutes({ '/cases/c1/ai-policy': { mode: 'no_ai' }, '/cases/c1/ai/activity': [], '/connections': conns, '/ai/ladder': ladder, '/cases/c1/modules': [{ ...mod, profile: 'dotnet' }] }));
    render(<App store={m.store} />);
    await screen.findByTestId('ai-tab');
    await screen.findByTestId('ai-settings');
    expect(screen.queryByTestId('naming-review')).not.toBeInTheDocument();
    m.store.stop();
  });
});
