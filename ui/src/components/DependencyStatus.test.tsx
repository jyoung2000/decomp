import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, demoCase, makeStore } from '../test-utils';

const queue = (over: Record<string, unknown> = {}) => ({
  id: null, state: 'idle', items: [], reason: null, total: 0, done: 0, failed: [], bytes_total: 0, bytes_done: 0, percent: null, current: null, running: false, ...over,
});

const tool = (over: Record<string, unknown>) => ({
  name: 'rizin', title: 'Rizin', purpose: 'Needed to analyze native Windows programs.', optional: false, state: 'not_installed', status: 'not_installed',
  disk_status: 'not_installed', satisfied: false, installable: true, version: '0.9.1', installed_version: null, hash_ok: null, smoke: null, external_path: null,
  requires: [], blocked_reason: null, download_bytes: 140_000_000, disk_bytes: 400_000_000, install_path: 'C:\\tools\\rizin', job: null,
  needed_by: [{ kind: 'project', case_id: 'c1', name: 'Test project', required: true, why: 'to analyze Windows programs (.exe/.dll)' }], required: true, ...over,
});

const report = (over: Record<string, unknown> = {}) => ({
  checked_at: '2026-10-10T00:00:00Z', tools_dir: 'C:\\tools', tools: [tool({})], services: [], host: [], projects: [],
  queue: queue(), settings: { auto_install: false, asked: true }, missing_required: ['rizin', 'rust'], missing_recommended: [],
  install_all: { items: ['rizin', 'rust'], count: 2, download_bytes: 160_000_000, label: "Install what's missing (2 items, 160 MB)" },
  overall: 'error', sentence: '2 tools your projects need are missing: Rizin and Rust compiler (private) (needed by Test project).',
  fix: { kind: 'install', items: ['rizin', 'rust'], count: 2, download_bytes: 160_000_000, label: "Install what's missing (2 items, 160 MB)" },
  ...over,
});

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/projects';
});

describe('dependency status bar', () => {
  it('is red with one plain sentence and installs everything missing with one click', async () => {
    const { store, calls } = makeStore(caseRoutes({ '/health/dependencies': report(), 'POST /health/dependencies/install': queue({ state: 'running', running: true }) }));
    render(<App store={store} />);
    const bar = await screen.findByTestId('dep-status');
    expect(bar).toHaveAttribute('data-state', 'error');
    expect(bar).toHaveTextContent('Action needed:');
    expect(bar).toHaveTextContent('2 tools your projects need are missing: Rizin and Rust compiler (private) (needed by Test project).');
    await userEvent.click(within(bar).getByRole('button', { name: "Install what's missing (2 items, 160 MB)" }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/health/dependencies/install')?.body).toEqual({ items: ['rizin', 'rust'], repair: false }));
    store.stop();
  });

  it('is green when everything is installed and running', async () => {
    const { store } = makeStore(caseRoutes({ '/health/dependencies': report({ overall: 'ok', sentence: 'Everything your projects need is installed and working.', fix: null }) }));
    render(<App store={store} />);
    const bar = await screen.findByTestId('dep-status');
    expect(bar).toHaveAttribute('data-state', 'ok');
    expect(within(bar).queryByRole('button')).not.toBeInTheDocument();
    store.stop();
  });

  it('asks once on first run whether to install automatically (default off)', async () => {
    const { store, calls } = makeStore(caseRoutes({
      '/health/dependencies': report({ settings: { auto_install: false, asked: false } }),
      'PUT /health/dependencies/settings': { auto_install: false, asked: true, install: null },
    }));
    render(<App store={store} />);
    const ask = await screen.findByTestId('dep-first-run');
    expect(ask).toHaveTextContent('Install the tools your projects need automatically?');
    expect(ask).toHaveTextContent('never installs anything else');
    await userEvent.click(within(ask).getByRole('button', { name: 'No, I will click Install' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/health/dependencies/settings')?.body).toEqual({ auto_install: false }));
    store.stop();
  });

  it('a damaged tool is repaired only after one confirmation', async () => {
    const { store, calls } = makeStore(caseRoutes({
      '/health/dependencies': report({ sentence: 'Rizin is damaged (its files are missing or changed).', fix: { kind: 'repair', items: ['rizin'], confirm: true, label: 'Repair Rizin' } }),
      'POST /health/dependencies/install': queue({ state: 'running', running: true }),
    }));
    render(<App store={store} />);
    const bar = await screen.findByTestId('dep-status');
    await userEvent.click(within(bar).getByRole('button', { name: 'Repair Rizin' }));
    expect(calls.some((c) => c.path === '/health/dependencies/install')).toBe(false);
    const dlg = await screen.findByRole('dialog');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Repair Rizin' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/health/dependencies/install')?.body).toEqual({ items: ['rizin'], repair: true }));
    store.stop();
  });

  it('offers to start an installed Ollama when a project uses AI on this PC', async () => {
    const { store, calls } = makeStore(caseRoutes({
      '/health/dependencies': report({ sentence: 'Ollama is installed but not running.', fix: { kind: 'start_ollama', label: 'Start Ollama' } }),
      'POST /health/services/ollama/start': { started: true, running: true, message: 'Ollama started.' },
    }));
    render(<App store={store} />);
    const bar = await screen.findByTestId('dep-status');
    await userEvent.click(within(bar).getByRole('button', { name: 'Start Ollama' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/health/services/ollama/start')).toBe(true));
    store.stop();
  });

  it('stays out of the way when the controller has no dependency report', async () => {
    const { store } = makeStore(caseRoutes({}));
    render(<App store={store} />);
    await screen.findByRole('heading', { name: 'Projects' });
    expect(screen.queryByTestId('dep-status')).not.toBeInTheDocument();
    store.stop();
  });
});

describe('preflight before Start', () => {
  const created = { ...demoCase, status: 'created' };
  const pf = (over: Record<string, unknown> = {}) => ({
    case_id: 'c1', ok: false, installing: false, sentence: 'This project needs Rizin and Rust compiler (private) before it can start (Windows programs (.exe/.dll) → rust).',
    profile: 'native_pe', profile_title: 'Windows programs (.exe/.dll)', target: 'rust', comparator: 'cli', ai: 'no_ai',
    missing: [
      { name: 'rizin', title: 'Rizin', state: 'not_installed', why: 'to analyze Windows programs (.exe/.dll)', download_bytes: 140_000_000, installable: true, blocked_reason: null, job: null },
      { name: 'rust', title: 'Rust compiler (private)', state: 'not_installed', why: 'to build the Rust (.exe) rebuild', download_bytes: 160_000_000, installable: true, blocked_reason: null, job: null },
    ],
    optional: [], services: [], blocking_services: [], host: [],
    install: { items: ['rizin', 'rust'], count: 2, download_bytes: 300_000_000, label: "Install what's missing (2 items, 300 MB)" },
    optional_install: null, settings: { auto_install: false, asked: true }, ...over,
  });

  it('blocks Start with the right list and a single install action', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store, calls } = makeStore(caseRoutes({
      '/cases/c1': created, '/cases': [created], '/cases/c1/preflight': pf(),
      'POST /health/dependencies/install': queue({ state: 'running', running: true }),
    }));
    render(<App store={store} />);
    const card = await screen.findByTestId('preflight');
    expect(card).toHaveTextContent('Before this project can start');
    expect(within(card).getByTestId('preflight-missing-rizin')).toHaveTextContent('Rizin – to analyze Windows programs (.exe/.dll)');
    expect(within(card).getByTestId('preflight-missing-rust')).toHaveTextContent('to build the Rust (.exe) rebuild');
    const start = screen.getByTestId('btn-start');
    expect(start).toBeDisabled();
    expect(document.getElementById(start.getAttribute('aria-describedby')!)).toHaveTextContent('needs Rizin and Rust compiler');
    await userEvent.click(within(card).getByRole('button', { name: "Install what's missing (2 items, 300 MB)" }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/health/dependencies/install')?.body).toEqual({ case_id: 'c1' }));
    store.stop();
  });

  it('enables Start when nothing is missing and offers optional tools', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store } = makeStore(caseRoutes({
      '/cases/c1': created, '/cases': [created],
      '/cases/c1/preflight': pf({ ok: true, missing: [], install: null, sentence: 'Everything this project needs is installed and running.',
        optional: [{ name: 'upx', title: 'UPX (optional)', state: 'not_installed', why: 'optional for Windows programs (.exe/.dll)', download_bytes: 680_000, installable: true, job: null }],
        optional_install: { items: ['upx'], count: 1, download_bytes: 680_000, label: "Install what's missing (1 item, 1 MB)" } }),
    }));
    render(<App store={store} />);
    const opt = await screen.findByTestId('preflight-optional');
    expect(opt).toHaveTextContent('UPX (optional)');
    expect(within(opt).getByRole('button', { name: 'Install optional tools (1 item, 1 MB)' })).toBeEnabled();
    expect(screen.getByTestId('btn-start')).toBeEnabled();
    store.stop();
  });
});

describe('Tools page: what projects need', () => {
  it('shows needed-by, installs everything with one click and shows per-item errors with Retry', async () => {
    window.location.hash = '#/tools';
    const err = { code: 'checksum_mismatch', message: 'The downloaded file does not match the pinned checksum; nothing was installed.', affected: 'rust', next_action: 'Retry the download.', retryable: true, url: null };
    const q = queue({
      state: 'failed', total: 2, done: 1, failed: ['rust'], bytes_total: 300, bytes_done: 140, percent: 46.7,
      items: [
        { name: 'rizin', title: 'Rizin', status: 'done', repair: false, bytes_total: 140, bytes_done: 140, phase: 'done', message: 'Installed', error: null },
        { name: 'rust', title: 'Rust compiler (private)', status: 'failed', repair: false, bytes_total: 160, bytes_done: 0, phase: 'failed', message: err.message, error: err },
      ],
    });
    const setupEntry = { name: 'rizin', title: 'Rizin', purpose: 'x', role: 'x', version: '0.9.1', installed_version: null, license: 'LGPL', optional: false, size_bytes: 1, file_name: 'r.zip', url: null, sha256: null, requires: [], status: 'not_installed', disk_status: 'not_installed', blocked_reason: null, install_path: 'C:\\tools\\rizin', job: null };
    const { store, calls } = makeStore(caseRoutes({
      '/tools/setup': { tools_dir: 'C:\\tools', lock_path: null, tools: [setupEntry], any_installed: false, required_missing: [], busy: false },
      '/health/dependencies': report({ queue: q }),
      'POST /health/dependencies/install': queue({ state: 'running', running: true }),
      'POST /health/dependencies/install/retry': queue({ state: 'running', running: true }),
    }));
    render(<App store={store} />);
    const panel = await screen.findByTestId('dep-panel');
    expect(screen.getByTestId('tool-rizin-needed-by')).toHaveTextContent('Needed by: Test project (required)');
    await userEvent.click(within(panel).getByRole('button', { name: 'Install everything needed (2 items, 160 MB)' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/health/dependencies/install')?.body).toEqual({ items: ['rizin', 'rust'], repair: false }));
    const qv = screen.getByTestId('dep-queue');
    expect(within(qv).getByTestId('dep-item-rust')).toHaveTextContent('Retry the download.');
    expect(within(qv).getByRole('progressbar', { name: 'Combined install progress' })).toHaveAttribute('aria-valuenow', '46');
    await userEvent.click(within(qv).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/health/dependencies/install/retry')).toBe(true));
    store.stop();
  });
});
