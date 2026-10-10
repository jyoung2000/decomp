import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';

// R9 (docs/AI_LADDER.md section 9): per-rung failure rules, provider cooldowns, the dry-run "Test route" and the JeV advisor card.
const NOW = new Date().toISOString();
const avail = { state: 'ok', probed_at: NOW, detail: null };
const claude = { position: 1, connection_id: 'conn_claude', connection_label: 'Claude', provider: 'anthropic', model: 'claude-x', locality: 'cloud', availability: avail, capabilities: null, price: { known: true, input_per_mtok: 3, output_per_mtok: 15 }, free: false, rules: {}, cooldown: null };
const gpt = { position: 2, connection_id: 'conn_openai', connection_label: 'OpenAI', provider: 'openai', model: 'gpt-x', locality: 'cloud', availability: avail, capabilities: null, price: { known: true, input_per_mtok: 0.15, output_per_mtok: 0.6 }, free: false, rules: { rate_limit: { action: 'wait', wait_minutes: 2, max_tries: 3 } }, cooldown: null };
const qwen = { position: 3, connection_id: 'conn_local', connection_label: 'Ollama (this PC)', provider: 'local', model: 'qwen2.5-coder:14b', locality: 'local', availability: avail, capabilities: null, price: { known: true, input_per_mtok: 0, output_per_mtok: 0 }, free: true, rules: {}, cooldown: null };
const conns = [
  { connection_id: 'conn_claude', provider: 'anthropic', label: 'Claude', endpoint: '', auth_mode: 'api_key', models: ['claude-x'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW },
  { connection_id: 'conn_openai', provider: 'openai', label: 'OpenAI', endpoint: '', auth_mode: 'api_key', models: ['gpt-x'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW },
  { connection_id: 'conn_local', provider: 'local', label: 'Ollama (this PC)', endpoint: 'http://127.0.0.1:11434/v1', auth_mode: 'local', models: ['qwen2.5-coder:14b'], capabilities: {}, limits: {}, state: 'ok', last_probe: NOW },
];
const tasks = ['implementation', 'repair', 'naming', 'visual_review', 'verification_assist', 'knowledge'];
const chain = 'Use claude-x (Claude); if it hits a limit or fails, use gpt-x (OpenAI); then local qwen2.5-coder:14b.';
const cool = { connection_id: 'conn_claude', connection_label: 'Claude', provider: 'anthropic', outcome: 'credits_exhausted', reason: 'Claude has run out of credits', until: '2030-01-01T10:00:00Z', scope: 'connection', text: 'Claude ran out of credits; every task skips it until 2030-01-01T10:00:00Z' };
const ladder = (cooldowns: unknown[] = []) => ({
  config_revision: 3,
  tasks: Object.fromEntries(tasks.map((t) => [t, t === 'implementation' ? { entries: [{ ...claude, cooldown: cooldowns.length ? cool : null }, gpt, qwen], rationale: 'user', chain } : { entries: [], rationale: 'auto', chain: 'No model: work that needs this task is skipped.' }])),
  cooldowns,
});
const jev = {
  enabled: true, has_key: false, key_source: null, model: 'jev-1.13.0', endpoint: 'https://api.typesafe.ai/v1/systemone', monthly_cap_usd: 1, setup_cap_usd: 0.05,
  price: { input_per_mtok: 0.042 }, month: { spent_usd: 0.0001, limit_usd: 1 }, breaker: { state: 'closed', failures: 0 }, offline_reason: 'no_key',
  jev_install: { key_file_found: true, path: 'C:\\Users\\me\\.jev\\secrets\\typesafe.key' },
  last_decisions: [{ kind: 'order', task: 'repair', choice: 'R2', confidence: 0.82, model: 'jev-1.13.0', source: 'jev', suggested_first: 'qwen2.5-coder:14b', at: NOW }],
};

beforeEach(() => localStorage.clear());

async function open(over: Routes = {}) {
  window.location.hash = '#/connections';
  const routes: Routes = {
    '/connections': conns,
    '/routes': [],
    '/ai/ladder': ladder(),
    '/ai/models': [],
    '/ai/jev': jev,
    '/hermes/status': { paired: false, diagnostics: [] },
    ...over,
  };
  const m = makeStore(caseRoutes(routes));
  render(<App store={m.store} />);
  const sec = await screen.findByTestId('ladder-section');
  await within(sec).findByTestId('lad-implementation-ladder');
  return { ...m, sec };
}

describe('granular AI control (R9)', () => {
  it('shows the fallback chain in plain words and dry-runs the ladder without spending', async () => {
    const user = userEvent.setup();
    const result = {
      task: 'implementation', config_revision: 3, answer: 2, tokens_spent: 0, sent: false, chain,
      summary: 'Right now position 2 (gpt-x, OpenAI) would answer; skipped: Claude ran out of credits; every task skips it until 2030-01-01T10:00:00Z.',
      rungs: [
        { ...claude, status: 'skipped', outcome: 'cooldown', reason: 'Claude ran out of credits; every task skips it until 2030-01-01T10:00:00Z' },
        { ...gpt, status: 'would_answer', outcome: 'ok', reason: 'gpt-x would be asked first (up to $0.0037)', rules_text: ['if it is rate-limited: wait 2 min and retry (up to 3 times), then the next rung'] },
        { ...qwen, status: 'standby', outcome: null, reason: 'used only if the rungs above fail (no cost)' },
      ],
    };
    const { store, sec, calls } = await open({ 'POST /ai/route/test': result });
    expect(within(sec).getByTestId('lad-implementation-chain')).toHaveTextContent(chain);
    await user.click(within(sec).getByRole('button', { name: /Test route for Implementation/ }));
    const out = await within(sec).findByTestId('route-test-implementation-result');
    expect(calls.find((c) => c.method === 'POST' && c.path === '/ai/route/test')?.body).toEqual({ task: 'implementation' });
    expect(out).toHaveTextContent('Right now position 2 (gpt-x, OpenAI) would answer');
    const items = within(out).getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('Skipped now');
    expect(items[0]).toHaveTextContent('Paused (provider cooldown)');
    expect(items[1]).toHaveTextContent('Would answer now');
    expect(items[1]).toHaveTextContent('Rules: if it is rate-limited: wait 2 min');
    expect(items[2]).toHaveTextContent('Next if the ones above fail');
    store.stop();
  });

  it('edits per-rung failure rules and saves them with the ladder', async () => {
    const user = userEvent.setup();
    const { store, sec, calls } = await open({ 'PUT /ai/ladder/implementation': { config_revision: 4 } });
    const ladderEl = within(sec).getByTestId('lad-implementation-ladder');
    expect(ladderEl).toHaveTextContent('if it is rate-limited: wait 2 min, up to 3×');
    // the test button explains why it is disabled while the ladder has unsaved changes
    await user.click(within(ladderEl).getByRole('button', { name: /Failure rules for claude-x/ }));
    const box = within(ladderEl).getByRole('group', { name: 'What to do when claude-x fails' });
    await user.selectOptions(within(box).getByLabelText('runs out of credits'), 'stop');
    // "wait" is not offered where waiting cannot help
    expect(within(within(box).getByTestId(/auth_failed$/)).queryByRole('option', { name: 'Wait and retry' })).toBeNull();
    await user.selectOptions(within(box).getByLabelText('hits its usage limit'), 'wait');
    const mins = within(box).getAllByLabelText('minutes')[0];
    await user.clear(mins);
    await user.type(mins, '10');
    expect(within(sec).getByRole('button', { name: /Test route for Implementation/ })).toBeDisabled();
    await user.click(within(sec).getByRole('button', { name: /Save ladder for Implementation/ }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'PUT' && c.path === '/ai/ladder/implementation')?.body).toEqual({
        entries: [
          { connection_id: 'conn_claude', model: 'claude-x', rules: { credits_exhausted: { action: 'stop' }, usage_limit: { action: 'wait', wait_minutes: 10, max_tries: 3 } } },
          { connection_id: 'conn_openai', model: 'gpt-x', rules: { rate_limit: { action: 'wait', wait_minutes: 2, max_tries: 3 } } },
          { connection_id: 'conn_local', model: 'qwen2.5-coder:14b', rules: {} },
        ],
      }),
    );
    store.stop();
  });

  it('shows paused providers as a badge and clears a cooldown', async () => {
    const user = userEvent.setup();
    let cleared = false;
    const { store, sec, calls } = await open({
      '/ai/ladder': () => ladder(cleared ? [] : [cool]),
      'DELETE /ai/cooldowns/conn_claude': () => {
        cleared = true;
        return { connection_id: 'conn_claude', cleared: true, cooldowns: [] };
      },
    });
    const list = await within(sec).findByTestId('cooldown-list');
    expect(list).toHaveTextContent('Claude');
    expect(within(sec).getAllByTestId('cooldown-badge')[0]).toHaveTextContent('Paused: out of credits');
    await user.click(within(list).getByRole('button', { name: /Clear cooldown for Claude/ }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.path === '/ai/cooldowns/conn_claude')).toBe(true));
    await waitFor(() => expect(within(sec).queryByTestId('cooldown-list')).toBeNull());
    store.stop();
  });

  it('JeV advisor card: key entry, explicit import from the JeV install, cap, test and last decisions', async () => {
    const user = userEvent.setup();
    const KEY = 'apikey_NOT_REAL_ui_123456';
    const withKey = (src: string) => ({ ...jev, has_key: true, key_source: src, offline_reason: null });
    const { store, calls } = await open({
      'PUT /ai/jev/key': () => withKey('entered'),
      'POST /ai/jev/key/import': () => ({ ...withKey('jev_install'), imported: true }),
      'PUT /ai/jev': (b: unknown) => ({ ...withKey('entered'), ...(b as object) }),
      'POST /ai/jev/test': { ok: true, reason: 'ok', message: 'JeV answered (jev-1.13.0); the advisor is ready.' },
    });
    const card = await screen.findByTestId('jev-card');
    await within(card).findByTestId('jev-offline');
    expect(card).toHaveTextContent('No key yet: your ladder order is used as is.');
    expect(within(card).getByTestId('jev-decisions')).toHaveTextContent('repair: try qwen2.5-coder:14b first · confidence 0.82');
    expect(within(card).getByRole('button', { name: 'Test JeV' })).toBeDisabled();
    // the import is an explicit click; nothing is read before it
    expect(calls.some((c) => c.path === '/ai/jev/key/import')).toBe(false);
    await user.click(within(card).getByRole('button', { name: 'Use the key from my JeV install' }));
    await waitFor(() => expect(card).toHaveTextContent('Key stored (from your JeV install)'));
    // typed key: sent once, never echoed back into the page
    await user.type(within(card).getByLabelText('Replace key'), KEY);
    await user.click(within(card).getByRole('button', { name: 'Save key' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/ai/jev/key')?.body).toEqual({ key: KEY }));
    await waitFor(() => expect((within(card).getByLabelText('Replace key') as HTMLInputElement).value).toBe(''));
    expect(document.body.textContent).not.toContain(KEY);
    // monthly cap
    const cap = within(card).getByLabelText('Monthly cap (USD)');
    await user.clear(cap);
    await user.type(cap, '2.5');
    await user.click(within(card).getByRole('button', { name: 'Save cap' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/ai/jev')?.body).toEqual({ monthly_cap_usd: 2.5 }));
    // on/off
    await user.click(within(card).getByRole('checkbox', { name: 'Use JeV advice' }));
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT' && c.path === '/ai/jev').at(-1)?.body).toEqual({ enabled: false }));
    await user.click(within(card).getByRole('button', { name: 'Test JeV' }));
    expect(await within(card).findByTestId('jev-test')).toHaveTextContent('the advisor is ready');
    store.stop();
  }, 20_000);
});
