import { test, expect } from '@playwright/test';
import { openApp, resetMock, shot, step } from './helpers';

test('errors explain what happened, what is affected and the next action', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 6);
  await openApp(page, '#/projects/case_demo/feedback');
  const form = page.getByRole('form', { name: 'Give feedback' });

  // 1) the documented error shape
  await request.post('/__mock/fail', {
    data: {
      method: 'POST',
      path: '/cases/case_demo/feedback',
      status: 507,
      body: { error: { code: 'disk_full', message: 'Could not write the feedback record: the data drive is full.', affected: 'This feedback only; nothing else was changed.', next_action: 'Free space on the data drive, then submit again.' } },
    },
  });
  await page.getByLabel('About', { exact: true }).selectOption('feature');
  await page.getByLabel('Target', { exact: true }).selectOption({ label: 'Main window layout' });
  await page.getByLabel('Comment *').fill('Will fail once');
  await page.getByTestId('submit-feedback').click();
  const alert = form.getByRole('alert').filter({ hasText: 'Feedback was not saved' });
  await expect(alert).toContainText('What happened: Could not write the feedback record: the data drive is full.');
  await expect(alert).toContainText('(disk_full)');
  await expect(alert).toContainText('Affected: This feedback only; nothing else was changed.');
  await expect(alert).toContainText('Next: Free space on the data drive, then submit again.');
  await expect(page.getByLabel('Comment *')).toHaveValue('Will fail once'); // input is kept for the retry
  await shot(page, '22-error-callout');

  // 2) FastAPI request validation (422 {"detail": [...]}) as the real controller sends it
  await request.post('/__mock/fail', {
    data: { method: 'POST', path: '/cases/case_demo/feedback', status: 422, body: { detail: [{ loc: ['body', 'priority'], msg: "String should match pattern '^(low|medium|high|critical)$'", type: 'string_pattern_mismatch' }] } },
  });
  await page.getByTestId('submit-feedback').click();
  await expect(alert).toContainText("What happened: The controller rejected the request: priority: String should match pattern '^(low|medium|high|critical)$'.");
  await expect(alert).toContainText('Affected: Nothing was saved');
  await expect(alert).toContainText('Next: Correct the listed fields and try again.');

  // 3) retry succeeds and the callout disappears
  await page.getByTestId('submit-feedback').click();
  await expect(alert).toHaveCount(0);
  await expect(page.getByTestId('feedback-list')).toContainText('Will fail once');
  // the priority the UI sends is one the controller accepts
  const list = await request.get('/cases/case_demo/feedback', { headers: { Authorization: 'Bearer e2e-token' } });
  expect(((await list.json()) as { priority: string }[]).map((f) => f.priority)).toEqual(['medium']);
});

test('a fresh project shows empty states instead of blank panels', async ({ page, request }) => {
  await resetMock(request);
  await openApp(page, '#/new');
  await page.getByLabel(/^Name/).fill('Empty one');
  await page.getByLabel(/Source folder/).fill('/originals/empty');
  await page.getByLabel(/Output folder/).fill('/rebuilds/empty');
  await page.getByTestId('create-project').click();
  await expect(page).toHaveURL(/#\/projects\/case_[0-9a-f]+\/overview$/);
  await expect(page.getByTestId('job-counts')).toHaveText('none yet');
  await expect(page.getByTestId('current-action')).toContainText('Idle');
  await page.getByTestId('tab-preview').click();
  await expect(page.getByRole('status').filter({ hasText: 'No previews yet' })).toBeVisible();
  await page.getByTestId('tab-feedback').click();
  await expect(page.getByRole('status').filter({ hasText: 'No feedback yet' })).toBeVisible();
  await page.getByTestId('tab-comparisons').click();
  await expect(page.getByRole('status').filter({ hasText: 'No comparisons yet' })).toBeVisible();
  await page.getByTestId('tab-advanced').click();
  await expect(page.getByRole('status').filter({ hasText: 'No jobs yet' })).toBeVisible();
  await shot(page, '23-empty-advanced');
  await page.goto('/?token=e2e-token#/projects/case_does_not_exist/overview');
  await expect(page.getByText('Project not found')).toBeVisible();
});

test('status chips explain themselves on hover', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 6);
  await openApp(page, '#/projects/case_demo/advanced');
  const chips = page.locator('.chip[data-status]');
  await expect(chips.first()).toBeVisible();
  const missing = await chips.evaluateAll((els) => els.filter((e) => !e.getAttribute('title')).map((e) => (e as HTMLElement).dataset.status));
  expect(missing).toEqual([]);
  await expect(page.locator('.chip[data-status="completed"]').first()).toHaveAttribute('title', 'Finished successfully');
});
