import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';

const tasks = ['implementation', 'repair', 'naming', 'visual_review', 'verification_assist', 'knowledge'];
const fit = (ok: string[], note = '') => Object.fromEntries(tasks.map((t) => [t, { ok: ok.includes(t), note: ok.includes(t) ? note : 'not for this' }]));
const coder = { id: 'qwen2.5-coder:14b', server: 'ollama', size_bytes: 8_990_000_000, parameter_size: '14.8B', quantization: 'Q4_K_M', context_window: 131072, effective_context: 32768, capabilities: { completion: true, tools: true, source: 'ollama /api/show' }, tasks: fit(['implementation', 'repair', 'verification_assist', 'knowledge'], 'coding model'), quick_only: false, excluded: false, suitable: true, summary: 'Good for interpreting code, repairing builds.' };
const embed = { id: 'nomic-embed-text:latest', server: 'ollama', size_bytes: 270_000_000, capabilities: { completion: false, embedding: true, source: 'ollama /api/show' }, tasks: fit([]), excluded: true, suitable: false, summary: 'Not suitable: embedding-only model (cannot write text).' };
const snap = (ladder = 'empty') => ({
  detected_at: '2026-10-07T00:00:00Z',
  num_ctx_cap: 32768,
  servers: [
    { kind: 'ollama', name: 'Ollama', label: 'Ollama (this PC)', endpoint: 'http://127.0.0.1:11434/v1', found: true, version: '0.35.0', models: [coder, embed], connection_id: 'conn_local', connection_state: 'ok', models_folder: 'C:\\Users\\me\\.ollama\\models', install_page: 'https://ollama.com/download' },
    { kind: 'lmstudio', name: 'LM Studio', label: 'LM Studio (this PC)', endpoint: 'http://127.0.0.1:1234/v1', found: false, models: [], install_page: 'https://lmstudio.ai/download' },
  ],
  suitable_models: 1,
  ladder: { state: ladder },
  recommend_use: ladder !== 'user',
  recommended_preset: 'all_local',
  advice: ladder === 'user' ? 'You edited your ladder yourself; it is left alone.' : 'No AI ladder is set yet.',
});
const entry = { connection_id: 'conn_local', model: 'qwen2.5-coder:14b', locality: 'local', provider: 'local' };
const preset = { preset: 'all_local', applied: false, tasks: Object.fromEntries(tasks.map((t) => [t, { entries: t === 'visual_review' ? [] : [entry] }])), warnings: ['No local model supports images; visual review has no route.'] };
const config = { models_dir: 'C:\\Users\\me\\AppData\\Local\\RebuildStudio\\models', default_models_dir: 'C:\\Users\\me\\AppData\\Local\\RebuildStudio\\models', has_hf_token: false, ollama_models_folder: 'C:\\Users\\me\\.ollama\\models', num_ctx_cap: 32768 };
const files = {
  repo: 'acme/Small-GGUF', revision: 'abc', license: 'llama3.1', license_permissive: false, license_ack_required: true, license_note: 'Read the license.', gated: false, needs_token: false, has_token: false, page: 'https://huggingface.co/acme/Small-GGUF',
  files: [
    { path: 'Small-Q4_K_M.gguf', size_bytes: 100_000_000, sha256: 'a'.repeat(64), quant: 'Q4_K_M', kind: 'model', split: false, ram_hint_gb: 0.7, downloadable: true, note: null },
    { path: 'mmproj-f16.gguf', size_bytes: 5, sha256: null, quant: 'F16', kind: 'vision_projector', split: false, ram_hint_gb: null, downloadable: false, note: 'Vision add-on file, not a model on its own.' },
  ],
};

function routes(over: Routes = {}): Routes {
  return caseRoutes({
    '/connections': [],
    '/routes': [],
    '/ai/ladder': { config_revision: 1, tasks: {} },
    '/ai/models': [],
    '/hermes/status': { paired: false, diagnostics: [] },
    'POST /ai/local/detect': snap(),
    '/ai/local': snap(),
    'POST /ai/local/use': (b: unknown) => ({ ...preset, applied: (b as { apply: boolean }).apply }),
    '/ai/local/settings': config,
    '/ai/local/jobs': [],
    '/ai/local/models': [],
    '/ai/local/search': { query: 'small', results: [{ repo: 'acme/Small-GGUF', downloads: 1200, likes: 3, license: 'llama3.1', license_permissive: false, gated: false }] },
    '/ai/local/files': files,
    'POST /ai/local/downloads': { job_id: 'j1', kind: 'download', title: 'acme/Small-GGUF/Small-Q4_K_M.gguf', phase: 'queued', bytes_done: 0, bytes_total: 100_000_000, percent: 0, speed_bps: null, eta_s: null, message: '', error: null, finished: false, cancelled: false },
    ...over,
  });
}

beforeEach(() => {
  localStorage.clear();
});

describe('Local AI on this PC', () => {
  it('detects on open and shows servers, models and plain-language suitability', async () => {
    window.location.hash = '#/connections';
    const m = makeStore(routes());
    render(<App store={m.store} />);
    const sec = await screen.findByTestId('local-ai');
    await within(sec).findByTestId('local-models-ollama');
    expect(m.calls.some((c) => c.method === 'POST' && c.path === '/ai/local/detect')).toBe(true);
    expect(within(sec).getByTestId('local-server-ollama')).toHaveTextContent('Running');
    expect(within(sec).getByTestId('local-server-ollama')).toHaveTextContent('version 0.35.0');
    expect(within(sec).getByTestId('local-server-lmstudio')).toHaveTextContent('Not running');
    const row = within(sec).getByTestId('local-model-qwen2.5-coder:14b');
    expect(row).toHaveTextContent('128k');
    expect(row).toHaveTextContent('uses up to 32k');
    expect(row).toHaveTextContent('tools');
    expect(row).toHaveTextContent('Use');
    expect(within(sec).getByTestId('local-model-nomic-embed-text:latest')).toHaveTextContent('Not suitable');
    m.store.stop();
  });

  it('previews then applies "Use detected local models"', async () => {
    const user = userEvent.setup();
    window.location.hash = '#/connections';
    const m = makeStore(routes());
    render(<App store={m.store} />);
    const sec = await screen.findByTestId('local-ai');
    await user.click(await within(sec).findByRole('button', { name: 'Use detected local models' }));
    const prev = await within(sec).findByTestId('use-detected-preview');
    expect(prev).toHaveTextContent('Preview (not applied yet)');
    expect(prev).toHaveTextContent('qwen2.5-coder:14b');
    expect(prev).toHaveTextContent('visual review has no route');
    expect(m.calls.filter((c) => c.path === '/ai/local/use').map((c) => (c.body as { apply: boolean }).apply)).toEqual([false]);
    await user.click(within(prev).getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(m.calls.filter((c) => c.path === '/ai/local/use').map((c) => (c.body as { apply: boolean }).apply)).toEqual([false, true]));
    m.store.stop();
  });

  it('searches, requires a license acknowledgement with an explained disabled Download, then starts the download', async () => {
    const user = userEvent.setup();
    window.location.hash = '#/connections';
    const m = makeStore(routes());
    render(<App store={m.store} />);
    const sec = await screen.findByTestId('find-models');
    await user.type(within(sec).getByLabelText('Search models on Hugging Face'), 'small');
    await user.click(within(sec).getByRole('button', { name: 'Search' }));
    await user.click(await within(sec).findByRole('button', { name: 'Choose a file from acme/Small-GGUF' }));
    const repo = await within(sec).findByTestId('hf-repo');
    expect(within(repo).getByRole('radio', { name: /Q4_K_M/ })).toBeChecked();
    expect(repo).toHaveTextContent('Vision add-on file');
    expect(repo).toHaveTextContent('needs about 0.7 GB');
    const dl = within(repo).getByRole('button', { name: /^Download [0-9]/ });
    expect(dl).toBeDisabled();
    expect(repo).toHaveTextContent('Tick the license box');
    await user.click(within(repo).getByRole('checkbox'));
    expect(dl).toBeEnabled();
    await user.click(dl);
    await waitFor(() => expect(m.calls.some((c) => c.path === '/ai/local/downloads')).toBe(true));
    const body = m.calls.find((c) => c.path === '/ai/local/downloads')!.body as Record<string, unknown>;
    expect(body).toMatchObject({ repo: 'acme/Small-GGUF', path: 'Small-Q4_K_M.gguf', accept_license: true });
    m.store.stop();
  });

  it('shows download progress with speed and ETA and a Cancel button', async () => {
    window.location.hash = '#/connections';
    const job = { job_id: 'j1', kind: 'download', title: 'acme/Small-GGUF/Small-Q4_K_M.gguf', phase: 'downloading', bytes_done: 50_000_000, bytes_total: 100_000_000, percent: 50, speed_bps: 10_000_000, eta_s: 5, message: 'Downloading', error: null, finished: false, cancelled: false };
    const m = makeStore(routes({ '/ai/local/jobs': [job], 'POST /ai/local/jobs/j1/cancel': { ...job, finished: true, phase: 'cancelled' } }));
    render(<App store={m.store} />);
    const item = await screen.findByTestId('local-job-j1');
    expect(within(item).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
    expect(item).toHaveTextContent('5 s left');
    await userEvent.setup().click(within(item).getByRole('button', { name: /Cancel/ }));
    await waitFor(() => expect(m.calls.some((c) => c.path === '/ai/local/jobs/j1/cancel')).toBe(true));
    m.store.stop();
  });

  it('first run: the Projects empty state offers the detected local models', async () => {
    const user = userEvent.setup();
    window.location.hash = '#/';
    const m = makeStore(routes({ '/cases': [], '/tools/setup': { tools: [], any_installed: true, busy: false, tools_dir: 'x' } }));
    render(<App store={m.store} />);
    const hint = await screen.findByTestId('local-ai-hint');
    expect(hint).toHaveTextContent('Local AI detected: 1 model (runs on this PC, free)');
    await user.click(within(hint).getByRole('button', { name: 'Use them' }));
    await waitFor(() => expect(m.calls.some((c) => c.path === '/ai/local/use' && (c.body as { apply: boolean }).apply)).toBe(true));
    m.store.stop();
  });

  it('first run hint stays hidden when the user already has a ladder', async () => {
    window.location.hash = '#/';
    const m = makeStore(routes({ '/cases': [], '/ai/local': snap('user'), '/tools/setup': { tools: [], any_installed: true, busy: false, tools_dir: 'x' } }));
    render(<App store={m.store} />);
    await screen.findByText('No projects yet');
    await waitFor(() => expect(m.calls.some((c) => c.path.startsWith('/ai/local'))).toBe(true));
    expect(screen.queryByTestId('local-ai-hint')).toBeNull();
    m.store.stop();
  });
});
