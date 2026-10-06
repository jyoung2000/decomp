import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from './App';
import { ConfirmDialog } from './components/Dialog';
import { PhaseProgressRow } from './components/Progress';
import { toPhaseView } from './lib/derive';
import { caseRoutes, health, makeStore } from './test-utils';

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '';
});
afterEach(() => {
  window.location.hash = '';
});

describe('App shell', () => {
  it('connects, shows the banner state and lists projects', async () => {
    window.location.hash = '#/projects';
    const { store, sockets } = makeStore(caseRoutes());
    render(<App store={store} />);
    expect(await screen.findByRole('link', { name: 'Test project' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected'));
    expect(sockets[0].url).toContain('/ws?token=t&since=0');
    act(() => sockets[0].onclose?.());
    await waitFor(() => expect(screen.getByTestId('conn-banner')).toHaveAttribute('data-state', 'reconnecting'));
    expect(screen.getByTestId('conn-banner')).toHaveTextContent('Last event seq 0');
    store.stop();
  });

  it('shows the mock label only when the controller says it is the mock', async () => {
    const { store } = makeStore({ ...caseRoutes(), '/health': { ...health(), mode: 'mock' } });
    render(<App store={store} />);
    expect(await screen.findByTestId('mock-banner')).toHaveTextContent('Demo data – mock controller');
    store.stop();
  });

  it('overview: unknown scope, no fake percentages, no ETA, live events update state', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store, sockets } = makeStore(caseRoutes());
    render(<App store={store} />);
    const prog = await screen.findByTestId('phase-progress');
    await waitFor(() => expect(within(prog).getByText(/12 files done · unknown scope/)).toBeInTheDocument());
    expect(within(prog).getByText(/2 \/ 8 functions · 25%/)).toBeInTheDocument();
    expect(screen.getByTestId('eta')).toHaveTextContent('Remaining time unknown');
    await waitFor(() => expect(sockets.length).toBe(1));
    act(() => {
      sockets[0].emit({ seq: 1, ts: '2026-01-01T00:00:01Z', case_id: 'c1', job_id: null, kind: 'plan.revised', payload: { revision: 4, reason: 'more functions', progress: { analysis: { done: 52, total: 52 }, recovery: { done: 2, total: 11 } }, eta: { seconds: 600, uncertainty: 300, updated_at: '2026-01-01T00:00:01Z' }, items: [] } });
    });
    await waitFor(() => expect(screen.getByTestId('scope-notes')).toHaveTextContent('Recovery: scope changed from 8 to 11 items'));
    expect(screen.getByTestId('scope-notes')).toHaveTextContent('Discovery: scope became known — 52 items.');
    expect(screen.getByTestId('eta')).toHaveTextContent(/Estimated remaining: about 10 min 0 s \(± 5 min 0 s\)/);
    expect(screen.getByTestId('latest-event')).toHaveTextContent('Plan revised to r4: more functions');
    store.stop();
  });

  it('marks state stale when no event arrives within 2× heartbeat', async () => {
    window.location.hash = '#/projects/c1/overview';
    const { store, sockets } = makeStore(caseRoutes({ '/health': health(0.2) }));
    render(<App store={store} />);
    await waitFor(() => expect(sockets.length).toBe(1));
    act(() => sockets[0].emit({ seq: 1, ts: '2026-01-01T00:00:01Z', case_id: null, job_id: null, kind: 'controller.heartbeat', payload: { active: 1 } }));
    await waitFor(() => expect(screen.getByTestId('conn-banner')).toHaveAttribute('data-state', 'stale'), { timeout: 3000 });
    expect(await screen.findByTestId('stale-badge', {}, { timeout: 3000 })).toBeInTheDocument();
    store.stop();
  });

  it('new project: explains validation errors and disables unsupported combinations', async () => {
    window.location.hash = '#/new';
    const { store, calls } = makeStore(caseRoutes());
    const user = userEvent.setup();
    render(<App store={store} />);
    await user.click(await screen.findByTestId('create-project'));
    const summary = await screen.findByTestId('validation-summary');
    expect(summary).toHaveTextContent('No source folder was chosen.');
    expect(summary).toHaveTextContent('Affected:');
    expect(summary).toHaveTextContent('Next:');
    await waitFor(() => expect(within(screen.getByTestId('output-exe')).getByRole('radio')).toBeDisabled());
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    store.stop();
  });

  it('new project: shows the controller error with affected/next action', async () => {
    window.location.hash = '#/new';
    const { store } = makeStore(
      caseRoutes({
        'POST /cases': () => new Response(JSON.stringify({ error: { code: 'source_not_found', message: 'The source folder does not exist.', affected: 'Nothing was created.', next_action: 'Choose an existing folder.' } }), { status: 400 }),
      }),
    );
    const user = userEvent.setup();
    render(<App store={store} />);
    await user.type(await screen.findByLabelText(/^Name/), 'X');
    await user.type(screen.getByLabelText(/Source folder/), 'C:\\nope');
    await user.type(screen.getByLabelText(/Output folder/), 'D:\\out');
    await user.click(screen.getByTestId('create-project'));
    const alert = await screen.findByText('The source folder does not exist.');
    const box = alert.closest('.callout')!;
    expect(box).toHaveTextContent('Affected: Nothing was created.');
    expect(box).toHaveTextContent('Next: Choose an existing folder.');
    store.stop();
  });
});

describe('components', () => {
  it('progress row never shows a percentage without a denominator', () => {
    render(<PhaseProgressRow view={toPhaseView('recovery', { done: 7, total: null, unit: 'functions' })} />);
    const bar = screen.getByRole('progressbar', { name: 'Recovery progress' });
    expect(bar).not.toHaveAttribute('aria-valuenow');
    expect(screen.getByText('7 functions done · unknown scope')).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it('dialog closes on Escape and traps focus', async () => {
    const user = userEvent.setup();
    let closed = 0;
    render(<ConfirmDialog open title="Stop?" body="really" confirmLabel="Stop it" onConfirm={() => undefined} onClose={() => closed++} />);
    const dialog = screen.getByRole('dialog', { name: 'Stop?' });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.tab();
    await user.tab();
    await user.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard('{Escape}');
    expect(closed).toBe(1);
  });
});
