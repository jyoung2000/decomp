import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore } from '../test-utils';

const doctor = { backends: [], summary: {} };
const cfg = (q0: { client: string; transport: string }) => {
  // R11: `auto` resolves to http when the controller belongs to the installed desktop app (stable MCP endpoint)
  const q = { ...q0, transport: q0.transport === 'auto' ? 'http' : q0.transport };
  return {
    client: q.client,
    transport: q.transport,
    recommended_transport: 'http',
    format: q.client === 'codex' ? 'toml' : 'json',
    where: q.client === 'codex' ? '~/.codex/config.toml' : '~/.claude.json',
    text: q.transport === 'http' ? `{"url":"http://127.0.0.1:5000/mcp","client":"${q.client}"}` : `{"command":"rebuild-mcp.exe","client":"${q.client}"}`,
    notes: [q.transport === 'http' ? 'The address and token stay the same when the app restarts.' : 'Works whether or not the desktop app is running.'],
    command: q.client === 'claude-code' ? 'claude mcp add --scope user rebuild-studio -- rebuild-mcp.exe --toolset all' : undefined,
    http_available: true,
  };
};
const backups = {
  backups: [
    { name: 'studio-20261011T031245Z-start.sqlite3', taken_at: '2026-10-11T03:12:45+00:00', reason: 'start', projects: 3, size: 4096, integrity: 'ok' },
    { name: 'studio-20261010T120000Z-pre-migration-v1.sqlite3', taken_at: '2026-10-10T12:00:00+00:00', reason: 'pre-migration-v1', projects: 2, size: 2048, integrity: 'ok' },
  ],
  last_backup: '2026-10-11T03:12:45+00:00',
  backups_dir: 'C:/data/backups',
  damaged_dir: 'C:/data/damaged',
  policy: { keep_last: 5, keep_daily_days: 7, periodic_hours: 6, when: [] },
  last_recovery: null,
};

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/settings';
});

describe('Settings: Copy MCP config', () => {
  it('defaults to the app connection, switches transport and client, and copies', async () => {
    const user = userEvent.setup();
    const { store, calls } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': { data_dir: 'C:/data' },
        '/maintenance/backups': backups,
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
    // R11: the installed desktop app defaults to the HTTP connection
    expect(await within(card).findByTestId('mcp-config-text')).toHaveTextContent('127.0.0.1:5000/mcp');
    expect((within(card).getByLabelText('Connection') as HTMLSelectElement).value).toBe('http');
    expect(card).toHaveTextContent('recommended');
    expect(calls.some((c) => c.path === '/mcp/config?client=claude-code&transport=auto&toolset=all')).toBe(true);

    await user.selectOptions(within(card).getByLabelText('Connection'), 'stdio');
    await waitFor(() => expect(within(card).getByTestId('mcp-config-text')).toHaveTextContent('rebuild-mcp.exe'));
    await user.click(within(card).getByTestId('mcp-copy'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('{"command":"rebuild-mcp.exe","client":"claude-code"}'));
    await user.click(within(card).getByTestId('mcp-copy-command'));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(expect.stringContaining('claude mcp add')));

    await user.selectOptions(within(card).getByLabelText('Client'), 'codex');
    await user.selectOptions(within(card).getByLabelText('Connection'), 'http');
    await waitFor(() => expect(within(card).getByTestId('mcp-config-text')).toHaveTextContent('"client":"codex"'));
    expect(within(card).getByTestId('mcp-config-text')).toHaveTextContent('127.0.0.1:5000/mcp');
    expect(card).toHaveTextContent('stay the same when the app restarts');
    expect(within(card).queryByTestId('mcp-copy-command')).not.toBeInTheDocument();
    expect(calls.some((c) => c.path === '/mcp/config?client=codex&transport=http&toolset=all')).toBe(true);
    store.stop();
  });

  it('explains when the configuration cannot be loaded', async () => {
    const { store } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': {},
        '/maintenance/backups': backups,
        '/mcp/config': new Response(JSON.stringify({ error: { code: 'unavailable', message: 'MCP HTTP transport is not available', next_action: 'Use stdio' } }), { status: 503 }),
      }),
    );
    render(<App store={store} />);
    const card = await screen.findByTestId('mcp-config');
    expect(await within(card).findByText(/MCP configuration unavailable/)).toBeInTheDocument();
    store.stop();
  });
});

describe('Settings: project database backups (R11)', () => {
  it('shows the kept backups and takes one on request', async () => {
    const user = userEvent.setup();
    let taken = 0;
    const { store, calls } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': {},
        '/mcp/config': cfg({ client: 'claude-code', transport: 'auto' }),
        '/maintenance/backups': backups,
        '/maintenance/backup': () => {
          taken += 1;
          return { ...backups.backups[0], name: 'studio-20261011T100000Z-manual.sqlite3', reason: 'manual' };
        },
      }),
    );
    render(<App store={store} />);
    const card = await screen.findByTestId('data-safety');
    expect(await within(card).findByTestId('last-backup')).toHaveTextContent('2 kept');
    expect(card).toHaveTextContent('App start');
    expect(card).toHaveTextContent('Before an upgrade');
    expect(card).toHaveTextContent('the damaged file is kept');
    await user.click(within(card).getByTestId('backup-now'));
    await waitFor(() => expect(taken).toBe(1));
    expect(calls.some((c) => c.path === '/maintenance/backup' && c.method === 'POST')).toBe(true);
    store.stop();
  });

  it('reports a damaged-store recovery that happened', async () => {
    const { store } = makeStore(
      caseRoutes({
        '/doctor': doctor,
        '/settings': {},
        '/mcp/config': cfg({ client: 'claude-code', transport: 'auto' }),
        '/maintenance/backups': {
          ...backups,
          last_recovery: { at: '2026-10-11T04:00:00+00:00', mode: 'restore', restored: 'studio-20261011T031245Z-start.sqlite3', damaged_files: ['x'] },
        },
      }),
    );
    render(<App store={store} />);
    const card = await screen.findByTestId('data-safety');
    expect(await within(card).findByTestId('last-recovery')).toHaveTextContent('restored studio-20261011T031245Z-start.sqlite3');
    store.stop();
  });
});
