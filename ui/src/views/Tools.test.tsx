import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore } from '../test-utils';

const base = (over: Record<string, unknown>) => ({
  name: 'rizin', title: 'Rizin', purpose: 'Needed to analyze native Windows programs (.exe and .dll files).', role: 'x', version: '0.9.1', installed_version: null,
  license: 'LGPL-3.0-only', optional: false, size_bytes: 115391593, file_name: 'rizin.zip', url: 'https://example.test/rizin.zip', sha256: 'a'.repeat(64),
  requires: [], status: 'not_installed', disk_status: 'not_installed', blocked_reason: null, install_path: 'C:\\tools\\rizin', job: null, ...over,
});
const snap = (tools: unknown[], busy = false) => ({ tools_dir: 'C:\\tools', lock_path: null, tools, any_installed: false, required_missing: [], busy });

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/tools';
});

describe('Tools view', () => {
  it('explains each tool in plain language and shows size and license', async () => {
    const { store } = makeStore(caseRoutes({ '/tools/setup': snap([base({})]) }));
    render(<App store={store} />);
    const card = await screen.findByTestId('tool-rizin');
    expect(within(card).getByText(/Needed to analyze native Windows programs/)).toBeInTheDocument();
    expect(card).toHaveTextContent('LGPL-3.0-only');
    expect(card).toHaveTextContent('110.0 MB');
    expect(within(card).getByRole('button', { name: 'Install' })).toBeEnabled();
    store.stop();
  });

  it('renders a real progress bar with bytes and offers Cancel while installing', async () => {
    const job = { phase: 'downloading', bytes_done: 5_242_880, bytes_total: 10_485_760, percent: 50, message: 'Downloading', error: null, finished: false, chain: ['rizin'], cancelled: false };
    const { store, calls } = makeStore(caseRoutes({
      '/tools/setup': snap([base({ status: 'installing', job })], true),
      'POST /tools/setup/rizin/cancel': base({}),
    }));
    render(<App store={store} />);
    const bar = await screen.findByRole('progressbar', { name: /Rizin install progress/ });
    expect(bar).toHaveAttribute('aria-valuenow', '5242880');
    expect(bar).toHaveAttribute('aria-valuemax', '10485760');
    expect(screen.getByTestId('tool-rizin-nums')).toHaveTextContent('5.0 MB of 10.0 MB · 50%');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/tools/setup/rizin/cancel')).toBe(true));
    expect(screen.queryByRole('button', { name: 'Install' })).not.toBeInTheDocument();
    store.stop();
  });

  it('shows the error with a next action and retries', async () => {
    const error = { code: 'offline', message: 'Could not reach example.test (ConnectError). Download URL: https://example.test/rizin.zip', affected: 'https://example.test/rizin.zip', next_action: 'Check your internet connection and retry.', retryable: true, url: 'https://example.test/rizin.zip' };
    const job = { phase: 'failed', bytes_done: 0, bytes_total: 0, percent: null, message: '', error, finished: true, chain: ['rizin'], cancelled: false };
    const { store, calls } = makeStore(caseRoutes({ '/tools/setup': snap([base({ job })]), 'POST /tools/setup/rizin/install': base({}) }));
    render(<App store={store} />);
    const card = await screen.findByTestId('tool-rizin');
    expect(within(card).getByRole('alert')).toHaveTextContent('Check your internet connection and retry.');
    expect(card).toHaveTextContent('Install from file');
    await userEvent.click(within(card).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/tools/setup/rizin/install')).toBe(true));
    store.stop();
  });

  it('install from file posts the typed path; its button is explained while disabled', async () => {
    const { store, calls } = makeStore(caseRoutes({ '/tools/setup': snap([base({})]), 'POST /tools/setup/rizin/install-from-file': base({}) }));
    render(<App store={store} />);
    const card = await screen.findByTestId('tool-rizin');
    await userEvent.click(within(card).getByRole('button', { name: 'Install from file…' }));
    const go = within(card).getByRole('button', { name: 'Install this file' });
    expect(go).toBeDisabled();
    expect(go.getAttribute('aria-describedby')).toBeTruthy();
    expect(card).toHaveTextContent('Type the full path of the file first.');
    await userEvent.type(within(card).getByLabelText('Downloaded file'), 'C:\\dl\\rizin.zip');
    expect(go).toBeEnabled();
    await userEvent.click(go);
    await waitFor(() => expect(calls.find((c) => c.path === '/tools/setup/rizin/install-from-file')?.body).toEqual({ path: 'C:\\dl\\rizin.zip' }));
    store.stop();
  });

  it('never leaves a disabled button without an explanation', async () => {
    const tools = [
      base({ name: 'rizin', status: 'blocked_unverified', blocked_reason: 'No verified checksum is pinned for this download.' }),
      base({ name: 'gdre', title: 'GDRE Tools', status: 'installed', disk_status: 'installed' }),
      base({ name: 'node', title: 'Node.js', status: 'not_installed' }),
    ];
    const { store } = makeStore(caseRoutes({ '/tools/setup': snap(tools, true) }));
    render(<App store={store} />);
    await screen.findByTestId('tool-rizin');
    const disabled = screen.getAllByRole('button').filter((b) => (b as HTMLButtonElement).disabled);
    expect(disabled.length).toBeGreaterThan(0);
    for (const b of disabled) {
      const id = b.getAttribute('aria-describedby');
      expect(id, `${b.textContent} has no aria-describedby`).toBeTruthy();
      expect(document.getElementById(id!)?.textContent?.trim().length).toBeGreaterThan(5);
    }
    expect(screen.getByTestId('tool-rizin')).toHaveTextContent('No verified checksum is pinned');
    store.stop();
  });

  it('Projects empty state points to Tools when nothing is installed', async () => {
    window.location.hash = '#/projects';
    const { store } = makeStore(caseRoutes({ '/cases': [], '/tools/setup': snap([base({})]) }));
    render(<App store={store} />);
    const hint = await screen.findByTestId('tools-first-run');
    expect(within(hint).getByRole('link', { name: /open Tools/ })).toHaveAttribute('href', '#/tools');
    store.stop();
  });
});
