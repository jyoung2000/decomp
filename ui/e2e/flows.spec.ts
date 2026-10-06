import { test, expect } from '@playwright/test';
import { openApp, resetMock, shot, step } from './helpers';

const WS = '#/projects/case_demo';


test('unknown denominators render as raw counts without a percentage', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 1); // discovery running: done=12, total=null
  await openApp(page, `${WS}/overview`);
  const analysis = page.locator('[data-phase="analysis"]');
  await expect(analysis).toContainText('12 files done · unknown scope');
  await expect(analysis).not.toContainText('%');
  const bar = analysis.getByRole('progressbar');
  await expect(bar).not.toHaveAttribute('aria-valuenow', /.*/);
  await expect(bar).toHaveAttribute('aria-valuetext', '12 files done · unknown scope');
  // phases without any counts say so instead of showing 0%
  await expect(page.locator('[data-phase="build"]')).toContainText(/not reported yet|0 done · unknown scope/);
  await expect(page.getByTestId('phase-progress')).not.toContainText('0%');
  // the job row in Advanced shows the raw count too
  await page.getByTestId('tab-advanced').click();
  await expect(page.getByRole('table', { name: 'Jobs' }).getByRole('row').filter({ hasText: 'Inventory the original' })).toContainText('unknown scope');

  await step(request, 2); // scope becomes known → only now a percentage appears
  await page.getByTestId('tab-overview').click();
  await expect(analysis).toContainText('52 / 52 files · 100%');
  await expect(analysis.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '52');
});

test('duplicate and out-of-order events do not double count', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 3); // inventory completed, recovery running
  await openApp(page, `${WS}/overview`);
  await expect(page.getByTestId('job-counts')).toHaveText('1 running · 1 completed');
  // let the initial heartbeat / replay settle, then remember the duplicate counter
  await page.getByTestId('tab-advanced').click();
  await page.getByRole('tab', { name: 'Raw logs' }).click();
  const stats = page.getByTestId('event-stats');
  await expect(stats).toBeVisible();
  const dupBefore = Number((await stats.textContent())!.match(/(\d+) duplicates? dropped/)![1]);

  const job = { job_id: 'job_burst', case_id: 'case_demo', stage: 'verify', title: 'Burst verification job', state: 'queued', attempt: 0, progress: {}, blocker: null, heartbeat_at: null };
  const r = await request.post('/__mock/burst', {
    data: {
      events: [
        { kind: 'job.created', job_id: 'job_burst', payload: { job, depends_on: [] } },
        { kind: 'job.started', job_id: 'job_burst', payload: { job_id: 'job_burst', attempt: 1, stage: 'verify', title: job.title } },
        { kind: 'job.completed', job_id: 'job_burst', payload: { job_id: 'job_burst', title: job.title } },
        { kind: 'job.log', job_id: 'job_burst', payload: { job_id: 'job_burst', message: 'burst-line-unique' } },
      ],
      // completed arrives first, created/started/log are each delivered twice, all out of order
      order: [2, 0, 2, 3, 1, 0, 3, 1],
    },
  });
  expect(r.ok()).toBeTruthy();
  const { seqs } = (await r.json()) as { seqs: number[] };

  const log = page.getByTestId('raw-log');
  await expect(log.locator(`[data-seq="${seqs[3]}"]`)).toHaveCount(1);
  await expect(log.locator('.log-line', { hasText: 'burst-line-unique' })).toHaveCount(1);
  for (const s of seqs) await expect(log.locator(`[data-seq="${s}"]`)).toHaveCount(1);
  // rendered newest first: the four burst events appear in strictly descending seq order
  const order = await log.locator('.log-line').evaluateAll((els) => els.map((e) => Number((e as HTMLElement).dataset.seq)));
  expect(order).toEqual([...order].sort((a, b) => b - a));
  await expect(stats).toContainText(`${dupBefore + 4} duplicate`);

  await page.getByRole('tab', { name: 'Jobs' }).click();
  await expect(page.getByRole('table', { name: 'Jobs' }).getByRole('row').filter({ hasText: 'Burst verification job' })).toHaveCount(1);
  await expect(page.getByRole('table', { name: 'Jobs' }).getByRole('row').filter({ hasText: 'Burst verification job' }).locator('[data-status="completed"]')).toBeVisible();
  await page.getByTestId('tab-overview').click();
  // exactly one more completed job; the late "started" did not move it back to running
  await expect(page.getByTestId('job-counts')).toHaveText('1 running · 2 completed');
  await page.reload();
  await expect(page.getByTestId('job-counts')).toHaveText('1 running · 2 completed');
});

test('cancellation: Stop asks first, then every job and the project read cancelled', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 3);
  await openApp(page, `${WS}/overview`);
  await expect(page.getByTestId('job-counts')).toHaveText('1 running · 1 completed');
  await page.getByTestId('btn-stop').click();
  const dialog = page.getByRole('dialog', { name: 'Stop this rebuild?' });
  await expect(dialog).toContainText('Completed work, evidence and builds are kept');
  await dialog.getByRole('button', { name: 'Stop rebuild' }).click();
  await expect(dialog).toBeHidden();
  const header = page.getByRole('banner', { name: 'Project header' }).or(page.locator('.ws-head'));
  await expect(header.locator('[data-status="cancelled"]').first()).toBeVisible();
  await expect(header.locator('[data-status="cancelled"]').first()).toHaveAttribute('title', /Stopped by request/);
  await expect(page.getByTestId('job-counts')).toHaveText('1 completed · 1 cancelled');
  await expect(page.getByTestId('current-action')).toContainText('Idle');
  await expect(page.getByTestId('btn-resume')).toBeEnabled();
  await expect(page.getByTestId('btn-stop')).toBeDisabled();
  await expect(page.getByTestId('btn-pause')).toBeDisabled();
  await shot(page, '19-cancelled');
  await page.getByTestId('tab-advanced').click();
  const row = page.getByRole('table', { name: 'Jobs' }).getByRole('row').filter({ hasText: 'Recover functions' });
  await expect(row.locator('[data-status="cancelled"]')).toBeVisible();
  await expect(row.getByRole('button', { name: /Resume job/ })).toBeEnabled();
  await expect(row.getByRole('button', { name: /Cancel job/ })).toBeDisabled();
});

test('triage flow: feedback turned into plan work, persisted, visible in the plan', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 6); // v1 built with previews
  await openApp(page, `${WS}/feedback`);
  await page.getByLabel('About', { exact: true }).selectOption('feature');
  await page.getByLabel('Target', { exact: true }).selectOption({ label: 'Main window layout' });
  await page.getByLabel('Comment *').fill('Toolbar icons are missing their tooltips');
  await page.getByTestId('submit-feedback').click();
  const item = page.getByTestId('feedback-list').locator('li').filter({ hasText: 'Toolbar icons are missing' }).first();
  await expect(item.locator('[data-status="received"]').first()).toBeVisible();
  await expect(item.locator('[data-status="received"]').first()).toHaveAttribute('title', /not yet triaged/);

  // triage dialog: keyboard opens it, Esc closes it without changes
  await item.getByTestId('triage-feedback').focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Triage feedback' });
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(item.getByTestId('triage-feedback')).toBeFocused();

  await item.getByTestId('triage-feedback').click();
  await dialog.getByTestId('triage-create-work').check();
  await expect(dialog.getByLabel('New status')).toBeDisabled();
  await dialog.getByLabel('Note').fill('Confirmed on v1; schedule a fix');
  await shot(page, '20-triage-dialog');
  await dialog.getByTestId('triage-submit').click();
  await expect(dialog).toBeHidden();
  await expect(item.locator('[data-status="queued"]').first()).toBeVisible();
  await expect(item.getByTestId('linked-items')).toContainText('FB1');

  await page.reload();
  const again = page.getByTestId('feedback-list').locator('li').filter({ hasText: 'Toolbar icons are missing' }).first();
  await expect(again.locator('[data-status="queued"]').first()).toBeVisible();
  await again.getByRole('button', { name: 'History' }).click();
  await expect(again).toContainText('Confirmed on v1; schedule a fix');
  await shot(page, '21-triage-persisted');

  await page.getByTestId('tab-plan').click();
  await expect(page.getByTestId('plan-item-FB1')).toContainText('Fix: Toolbar icons are missing their tooltips');

  // plain triage to a terminal status
  await page.getByTestId('tab-feedback').click();
  await page.getByLabel('About', { exact: true }).selectOption('milestone');
  await page.getByLabel('Target', { exact: true }).selectOption('M4');
  await page.getByLabel('Comment *').fill('Is printing planned?');
  await page.getByRole('radio', { name: /Question/ }).check();
  await page.getByTestId('submit-feedback').click();
  const q = page.getByTestId('feedback-list').locator('li').filter({ hasText: 'Is printing planned?' }).first();
  await q.getByTestId('triage-feedback').click();
  await dialog.getByLabel('New status').selectOption('resolved');
  await dialog.getByLabel('Note').fill('Printing is deferred (plan item D2)');
  await dialog.getByTestId('triage-submit').click();
  await expect(q.locator('[data-status="resolved"]').first()).toBeVisible();
  await expect(q.getByTestId('triage-feedback')).toHaveCount(0);
});
