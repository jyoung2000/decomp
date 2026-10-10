import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore } from '../test-utils';

const doctor = { backends: [], summary: {} };
const cfg = (q: { client: string; transport: string }) => ({
  client: q.client,
  transport: q.transport,
  format: q.client === 'codex' ? 'toml' : 'json',
  where: q.client === 'codex' ? '~/.codex/config.toml' : '~/.claude.json',
  text: q.transport === 'http' ? `{"url":"http://127.0.0.1:5000/mcp","client":"${q.client}"}` : `{"command":"rebuild-mcp.exe","client":"${q.client}"}`,
  notes: [q.transport === 'http' ? 'The token and port change when the app restarts.' : 'Works whether or not the desktop app is running.'],
  command: q.client === 'claude-code' ? 'claude mcp add --scope user rebuild-studio -- rebuild-mcp.exe --toolset all' : undefined,
  http_available: true,
});

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/settings';
});

describe('Settings: Copy MCP config', () => {
  it('shows the snippet for the chosen client and transport and copies it', async () => {
    const user = userEvent.setup();
    const { store, calls } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': { data_dir: 'C:\\data' },
        '/mcp/config': () => {
          const last = calls[calls.length - 1].path;
          const q = new URLSearchParams(last.split('?')[1]);
          return cfg({ client: q.get('client') ?? '', transport: q.get('transport') ?? '' });
        },
      }),
    );
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<App store={store} />);
    const card = await screen.findByTestId('mcp-config');
    expect(await within(card).findByTestId('mcp-config-text')).toHaveTextContent('rebuild-mcp.exe');
    await user.click(within(card).getByTestId('mcp-copy'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('{"command":"rebuild-mcp.exe","client":"claude-code"}'));
    await user.click(within(card).getByTestId('mcp-copy-command'));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(expect.stringContaining('claude mcp add')));

    await user.selectOptions(within(card).getByLabelText('Client'), 'codex');
    await user.selectOptions(within(card).getByLabelText('Connection'), 'http');
    await waitFor(() => expect(within(card).getByTestId('mcp-config-text')).toHaveTextContent('"client":"codex"'));
    expect(within(card).getByTestId('mcp-config-text')).toHaveTextContent('127.0.0.1:5000/mcp');
    expect(card).toHaveTextContent('change when the app restarts');
    expect(within(card).queryByTestId('mcp-copy-command')).not.toBeInTheDocument();
    expect(calls.some((c) => c.path === '/mcp/config?client=codex&transport=http&toolset=all')).toBe(true);
    store.stop();
  });

  it('explains when the configuration cannot be loaded', async () => {
    const { store } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': {},
        '/mcp/config': new Response(JSON.stringify({ error: { code: 'unavailable', message: 'MCP HTTP transport is not available', next_action: 'Use stdio' } }), { status: 503 }),
      }),
    );
    render(<App store={store} />);
    const card = await screen.findByTestId('mcp-config');
    expect(await within(card).findByText(/MCP configuration unavailable/)).toBeInTheDocument();
    store.stop();
  });
});
