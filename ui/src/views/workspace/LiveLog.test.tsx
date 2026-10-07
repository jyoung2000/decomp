import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { filterLog, logEntryFromEvent, logToText, mergeLog, scrub } from '../../lib/log';
import { caseRoutes, health, makeStore, type Routes } from '../../test-utils';
import type { ControllerEvent, LogEntry } from '../../lib/types';

const T = (s: number) => `2026-01-01T00:00:${String(s).padStart(2, '0')}Z`;
const row = (seq: number, text: string, over: Partial<LogEntry> = {}): LogEntry => ({ seq, at: T(seq), kind: 'log', level: 'info', text, stage: 'inventory', milestone: 'M-ANALYSIS', job_id: 'j1', ...over });
const ev = (seq: number, kind: string, payload: Record<string, unknown>): ControllerEvent => ({ seq, ts: T(seq), case_id: 'c1', job_id: (payload.job_id as string) ?? null, kind, payload });

const INITIAL: LogEntry[] = [
  row(1, 'Started: Inventory installation root', { kind: 'job' }),
  row(2, 'Scanned the installation folder: 214 files, 12 modules (dotnet)'),
  row(3, 'Recovering C# from dotnetapp.dll with ILSpy 9.1…', { stage: 'recover_managed', milestone: 'M-RECOVERY' }),
  row(4, 'ILSpy (ilspycmd) is not installed, so this step is waiting', { stage: 'recover_managed', milestone: 'M-RECOVERY', level: 'warn', detail: 'ilspycmd not found in tools dir, PATH or ~/.dotnet/tools' }),
  row(5, 'gpt-demo-small explained 3 functions', { kind: 'ai', stage: null, model: 'gpt-demo-small', provider: 'openai' }),
];

function routes(over: Routes = {}) {
  return caseRoutes({ '/health': health(30, 5), '/cases/c1/log': { entries: INITIAL, latest_seq: 5 }, ...over });
}

async function open(over: Routes = {}) {
  window.location.hash = '#/projects/c1/live-log';
  const m = makeStore(routes(over));
  render(<App store={m.store} />);
  await screen.findByTestId('live-log');
  await waitFor(() => expect(screen.getAllByTestId('log-row')).toHaveLength(INITIAL.length));
  await waitFor(() => expect(m.sockets.length).toBe(1));
  return m;
}

const texts = () => screen.getAllByTestId('log-row').map((r) => r.textContent ?? '');

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '';
});
afterEach(() => {
  window.location.hash = '';
  vi.restoreAllMocks();
});

describe('live log helpers', () => {
  it('renders job transitions as text and looks up titles of jobs it only knows by id', () => {
    const jobs = { j9: { stage: 'build_candidate', title: 'Build candidate X', milestone_id: 'M-BUILD' } } as never;
    const lookup = (id: string) => (id === 'j9' ? (jobs as Record<string, never>).j9 : undefined);
    expect(logEntryFromEvent(ev(1, 'job.blocked', { job_id: 'j9', blocker: 'install Rust\nsecond line' }), lookup)).toMatchObject({ level: 'warn', text: 'Waiting: Build candidate X — install Rust', stage: 'build_candidate', milestone: 'M-BUILD' });
    expect(logEntryFromEvent(ev(2, 'job.failed', { job_id: 'j9', title: 'Build', error: 'cargo build failed:\nerror[E0425]: nope' }))).toMatchObject({ level: 'error', text: 'Failed: Build — cargo build failed:' });
    expect(logEntryFromEvent(ev(3, 'job.progress', { job_id: 'j9' }))).toBeNull();
    expect(logEntryFromEvent(ev(4, 'ai.activity', { text: 'Asked the model', prompt: 'RAW PROMPT TEXT', model: 'm' }))).not.toHaveProperty('prompt');
  });
  it('scrubs keys and auth headers, merges by seq and filters', () => {
    expect(scrub('key sk-abcdefghijklmnopqrstuvwx and Authorization: Bearer abcdefgh12345 ok')).not.toMatch(/sk-abc|abcdefgh12345/);
    expect(scrub('Authorization: Basic dXNlcjpwYXNzd29yZA==')).not.toContain('dXNlcjpw');
    const merged = mergeLog([row(1, 'a'), row(2, 'b')], [row(2, 'b2'), row(3, 'c')]);
    expect(merged.map((e) => e.text)).toEqual(['a', 'b2', 'c']);
    expect(filterLog(INITIAL, { kind: 'problems' }).map((e) => e.seq)).toEqual([4]);
    expect(filterLog(INITIAL, { kind: 'ai' }).map((e) => e.seq)).toEqual([5]);
    expect(filterLog(INITIAL, { kind: 'stage', stage: 'recover_managed' }).map((e) => e.seq)).toEqual([3, 4]);
    expect(logToText([INITIAL[3]])).toMatch(/WARN\s+Recover: ILSpy.*\n {4}ilspycmd not found/);
  });
});

describe('Live log tab', () => {
  it('loads the initial log, appends live lines in order and dedupes by seq', async () => {
    const { sockets, store } = await open();
    expect(texts()[1]).toContain('Scanned the installation folder: 214 files, 12 modules');
    act(() => {
      sockets[0].emit(ev(6, 'job.log', { job_id: 'j1', stage: 'inventory', milestone: 'M-ANALYSIS', level: 'info', text: 'Planned 1 recovery job(s)' }));
      sockets[0].emit(ev(6, 'job.log', { job_id: 'j1', stage: 'inventory', level: 'info', text: 'Planned 1 recovery job(s)' }));
      sockets[0].emit(ev(2, 'job.log', { job_id: 'j1', stage: 'inventory', level: 'info', text: 'Scanned the installation folder: 214 files, 12 modules (dotnet)' }));
      sockets[0].emit(ev(7, 'job.failed', { job_id: 'j1', title: 'Inventory installation root', stage: 'inventory', error: 'boom' }));
    });
    await waitFor(() => expect(screen.getAllByTestId('log-row')).toHaveLength(7));
    const t = texts();
    expect(t[5]).toContain('Planned 1 recovery job(s)');
    expect(t[6]).toContain('Failed: Inventory installation root — boom');
    expect(screen.getAllByTestId('log-row').map((r) => r.getAttribute('data-seq'))).toEqual(['1', '2', '3', '4', '5', '6', '7']);
    expect(screen.getAllByTestId('log-row')[6]).toHaveAttribute('data-level', 'error');
    store.stop();
  });

  it('filters: All / Problems / AI / per stage', async () => {
    const user = userEvent.setup();
    const { store } = await open();
    await user.click(screen.getByTestId('log-filter-problems'));
    expect(screen.getAllByTestId('log-row')).toHaveLength(1);
    expect(screen.getByTestId('log-filter-problems')).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByTestId('log-filter-ai'));
    expect(texts()).toHaveLength(1);
    expect(texts()[0]).toContain('gpt-demo-small explained 3 functions');
    expect(within(screen.getAllByTestId('log-row')[0]).getByText('AI')).toBeInTheDocument();
    await user.selectOptions(screen.getByTestId('log-filter-stage'), 'recover_managed');
    expect(screen.getAllByTestId('log-row')).toHaveLength(2);
    await user.click(screen.getByTestId('log-filter-all'));
    expect(screen.getAllByTestId('log-row')).toHaveLength(5);
    store.stop();
  });

  it('pause auto-scroll holds position, counts new lines and Jump to latest resumes', async () => {
    const user = userEvent.setup();
    const { sockets, store } = await open();
    const jump = screen.getByTestId('log-jump');
    expect(jump).toBeDisabled();
    await user.click(screen.getByTestId('log-pause'));
    expect(screen.getByTestId('log-pause')).toHaveAttribute('aria-pressed', 'true');
    act(() => {
      sockets[0].emit(ev(6, 'job.log', { job_id: 'j1', stage: 'inventory', level: 'info', text: 'one more' }));
      sockets[0].emit(ev(7, 'job.log', { job_id: 'j1', stage: 'inventory', level: 'info', text: 'and another' }));
    });
    await waitFor(() => expect(screen.getByTestId('log-jump')).toHaveTextContent('2 new'));
    expect(screen.getByTestId('log-jump')).toBeEnabled();
    const feed = screen.getByTestId('log-feed');
    const top = vi.spyOn(feed, 'scrollHeight', 'get').mockReturnValue(1234);
    await user.click(screen.getByTestId('log-jump'));
    await waitFor(() => expect(screen.getByTestId('log-pause')).toHaveAttribute('aria-pressed', 'false'));
    expect(feed.scrollTop).toBe(1234);
    expect(screen.getByTestId('log-jump')).toBeDisabled();
    top.mockRestore();
    store.stop();
  });

  it('is keyboard accessible: the feed is focusable and every control is a real button', async () => {
    const user = userEvent.setup();
    const { store } = await open();
    const feed = screen.getByRole('log', { name: 'Rebuild log' });
    expect(feed).toHaveAttribute('tabindex', '0');
    const bar = screen.getByRole('toolbar', { name: 'Log controls' });
    await user.tab();
    // tab order reaches every control inside the toolbar using only the keyboard
    const names: string[] = [];
    for (let i = 0; i < 40 && names.length < 8; i++) {
      const a = document.activeElement as HTMLElement;
      if (bar.contains(a)) names.push(a.textContent?.trim() || a.getAttribute('aria-label') || '');
      await user.tab();
    }
    expect(names.join('|')).toMatch(/All.*Problems.*AI.*All steps|Pause auto-scroll/);
    expect(screen.getByTestId('log-copy')).toBeEnabled();
    store.stop();
  });

  it('Copy puts the visible lines on the clipboard as plain text', async () => {
    const user = userEvent.setup();
    const { store } = await open();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await user.click(screen.getByTestId('log-filter-problems'));
    await user.click(screen.getByTestId('log-copy'));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = String(writeText.mock.calls[0][0]);
    expect(copied).toContain('WARN');
    expect(copied).toContain('ILSpy (ilspycmd) is not installed');
    expect(copied).not.toContain('Scanned the installation folder');
    expect(await screen.findByText('Log copied')).toBeInTheDocument();
    store.stop();
  });

  it('Save log as text downloads a text/plain Blob with every shown line', async () => {
    const user = userEvent.setup();
    const { store } = await open();
    let saved: Blob | null = null;
    Object.defineProperty(URL, 'createObjectURL', { value: (b: Blob) => ((saved = b), 'blob:mock'), configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: () => undefined, configurable: true });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toMatch(/^rebuild-log-c1-.*\.txt$/);
    });
    await user.click(screen.getByTestId('log-save'));
    expect(click).toHaveBeenCalledTimes(1);
    expect(saved).not.toBeNull();
    const blob = saved as unknown as Blob;
    expect(blob.type).toContain('text/plain');
    const body = await new Promise<string>((res) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result));
      r.readAsText(blob);
    });
    expect(body.trim().split('\n').filter((l) => l.startsWith('['))).toHaveLength(5);
    expect(body).toContain('Scanned the installation folder: 214 files, 12 modules');
    store.stop();
  });

  it('never renders raw prompts or secrets, even if the controller leaked them', async () => {
    const leaky: LogEntry[] = [
      row(1, 'Calling the tool with sk-abcdefghijklmnopqrstuvwx now', { detail: 'Authorization: Bearer zzTOPSECRETtokenvalue987654\nx-api-key: abcdef123456' }),
    ];
    window.location.hash = '#/projects/c1/live-log';
    const m = makeStore(routes({ '/health': health(30, 1), '/cases/c1/log': { entries: leaky, latest_seq: 1 } }));
    render(<App store={m.store} />);
    await waitFor(() => expect(screen.getAllByTestId('log-row')).toHaveLength(1));
    await waitFor(() => expect(m.sockets.length).toBe(1));
    act(() => {
      m.sockets[0].emit(ev(2, 'ai.activity', { text: 'Asked gpt-demo-small for code with sk-ZZZZZZZZZZZZZZZZZZZZZZZZ', prompt: 'SECRET SYSTEM PROMPT', messages: [{ content: 'RAW USER PROMPT' }], model: 'gpt-demo-small' }));
    });
    await waitFor(() => expect(screen.getAllByTestId('log-row')).toHaveLength(2));
    const html = document.body.innerHTML;
    for (const bad of ['sk-abcdefghijklmnopqrstuvwx', 'sk-ZZZZZZZZZZZZZZZZZZZZZZZZ', 'zzTOPSECRETtokenvalue987654', 'abcdef123456', 'SECRET SYSTEM PROMPT', 'RAW USER PROMPT']) expect(html).not.toContain(bad);
    expect(html).toContain('[REDACTED]');
    m.store.stop();
  });

  it('shows a clear stale/disconnected state when the socket drops', async () => {
    const { sockets, store } = await open();
    expect(screen.queryByTestId('live-log-stale')).not.toBeInTheDocument();
    act(() => sockets[0].onclose?.());
    const banner = await screen.findByTestId('live-log-stale');
    expect(banner).toHaveTextContent(/Reconnecting|Disconnected/);
    expect(screen.getAllByTestId('log-row')).toHaveLength(5); // lines already shown stay visible
    store.stop();
  });

  it('empty and error states are explicit', async () => {
    window.location.hash = '#/projects/c1/live-log';
    const m = makeStore(routes({ '/cases/c1/log': { entries: [], latest_seq: 0 } }));
    render(<App store={m.store} />);
    expect(await screen.findByText('Nothing has happened yet')).toBeInTheDocument();
    expect(screen.getByTestId('log-copy')).toBeDisabled();
    m.store.stop();
  });
});

describe('Overview: Now doing', () => {
  it('shows the latest three lines and links to the live log', async () => {
    window.location.hash = '#/projects/c1/overview';
    const m = makeStore(routes());
    render(<App store={m.store} />);
    const strip = await screen.findByTestId('now-doing');
    await waitFor(() => expect(within(strip).getAllByTestId('log-row')).toHaveLength(3));
    expect(within(strip).getAllByTestId('log-row').map((r) => r.getAttribute('data-seq'))).toEqual(['3', '4', '5']);
    await waitFor(() => expect(m.sockets.length).toBe(1));
    act(() => m.sockets[0].emit(ev(6, 'job.log', { job_id: 'j1', stage: 'recover_managed', level: 'info', text: 'Recovered C# from dotnetapp.dll with ILSpy 9.1' })));
    await waitFor(() => expect(within(strip).getAllByTestId('log-row').map((r) => r.getAttribute('data-seq'))).toEqual(['4', '5', '6']));
    expect(within(strip).getByTestId('now-doing-link')).toHaveAttribute('href', expect.stringContaining('/projects/c1/live-log'));
    m.store.stop();
  });

  it('keeps the existing Current action / Latest event cards and the existing tabs', async () => {
    window.location.hash = '#/projects/c1/overview';
    const m = makeStore(routes());
    render(<App store={m.store} />);
    expect(await screen.findByTestId('current-action')).toBeInTheDocument();
    expect(screen.getByTestId('latest-event')).toBeInTheDocument();
    for (const id of ['overview', 'plan', 'preview', 'feedback', 'scenarios', 'comparisons', 'ai', 'advanced', 'live-log']) expect(screen.getByTestId(`tab-${id}`)).toBeInTheDocument();
    m.store.stop();
  });
});
