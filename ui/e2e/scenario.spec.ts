import { test, expect } from '@playwright/test';
import { openApp, resetMock, shot, step } from './helpers';

test('live scenario: start → plan → preview → feedback persisted → retest', async ({ page, request, context }) => {
  await resetMock(request);
  await openApp(page, '#/projects/case_demo/overview');
  await expect(page.getByTestId('latest-event')).toContainText('Plan revised to r1');
  await page.getByTestId('btn-start').click();
  await expect(page.getByRole('heading', { level: 1, name: 'Inventory Tool (demo)' })).toBeVisible();
  await expect(page.getByTestId('current-action')).toContainText('Inventory the original');
  const discovery = page.locator('[data-phase="analysis"]');
  await expect(discovery).toContainText('12 files done · unknown scope');
  await expect(page.getByTestId('eta')).toContainText('Remaining time unknown');

  await step(request, 2); // → discovery done, scope known
  await expect(discovery).toContainText('52 / 52 files · 100%');
  await expect(page.locator('[data-phase="recovery"]')).toContainText('0 / 8 functions · 0%');
  await step(request, 2); // → recovery 4/8, then scope grows to 11
  await expect(page.getByTestId('scope-notes')).toContainText('Recovery: scope changed from 8 to 11 items (new work discovered)');
  await expect(page.getByTestId('eta')).toContainText('this is an estimate');
  await shot(page, '11-overview-scope-change');

  await step(request, 1); // → v1 built + previews + comparisons
  await page.getByTestId('tab-preview').click();
  const real = page.getByTestId('preview-pv_1_real');
  await expect(real.getByTestId('preview-kind')).toHaveText('Real build');
  await expect(page.getByTestId('preview-pv_1_mockup').getByTestId('preview-kind')).toHaveText('Mockup');
  const [popup] = await Promise.all([context.waitForEvent('page'), real.getByTestId('open-preview').click()]);
  await popup.waitForLoadState();
  await expect(popup.locator('body')).toContainText('Demo data – mock controller');
  await popup.close();
  await expect(real.getByTestId('preview-url')).toBeVisible();
  await expect(real.getByTestId('stop-preview')).toBeEnabled();
  await shot(page, '12-preview-open');
  await real.getByTestId('stop-preview').click();
  await expect(real.getByTestId('stop-preview')).toBeDisabled();

  // Test Feature → report a problem → feedback form prefilled
  await real.getByTestId('test-feature').click();
  await page.getByRole('dialog').getByLabel('Feature').selectOption({ label: 'Main window layout' });
  await page.getByRole('button', { name: 'Report a problem…' }).click();
  await expect(page).toHaveURL(/feedback\?target_kind=feature&target_id=F3/);
  await expect(page.getByLabel('About', { exact: true })).toHaveValue('feature');
  await expect(page.getByLabel('Target', { exact: true })).toHaveValue('F3');
  await expect(page.getByTestId('fb-build-hash')).toHaveValue(/^sha256:/);
  await page.getByLabel('Comment *').fill('Header bar is 6px off compared to the original');
  await page.getByLabel('Expected').fill('Row starts at x=16');
  await page.getByLabel('Actual').fill('Row starts at x=22');
  await page.getByLabel('Priority').selectOption('high');
  await page.getByLabel('Attachments').setInputFiles({ name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('screenshot notes') });
  await page.getByTestId('submit-feedback').click();
  const list = page.getByTestId('feedback-list');
  await expect(list).toContainText('Header bar is 6px off');
  await expect(list.locator('[data-status="received"]')).toBeVisible();
  await expect(list).toContainText('1 attachment');

  // persisted by the controller: survives a full reload
  await page.reload();
  await expect(page.getByTestId('feedback-list')).toContainText('Header bar is 6px off');
  await shot(page, '13-feedback-persisted');

  await step(request, 1); // triage → in progress
  await expect(page.getByTestId('feedback-list').locator('[data-status="in_progress"]')).toBeVisible();
  await step(request, 1); // v2 fixes it → ready to retest
  const item = page.getByTestId('feedback-list').locator('li').first();
  await expect(item.locator('[data-status="ready_to_retest"]')).toBeVisible();
  await item.getByRole('button', { name: 'History' }).click();
  await expect(item).toContainText('Fixed in version 2');
  await item.getByRole('button', { name: 'Compare fix' }).click();
  await expect(item.getByTestId('fix-compare')).toContainText('Fixed (v2)');
  await shot(page, '14-feedback-retest-compare');
  await item.getByTestId('reopen-feedback').click();
  await expect(item.locator('[data-status="reopened"]').first()).toBeVisible();

  await page.getByTestId('tab-preview').click();
  await expect(page.getByTestId('preview-pv_1_real').getByTestId('stale-preview')).toContainText('superseded by version 2');
});

test('stale detection and reconnect with replay', async ({ page, request }) => {
  await resetMock(request);
  await step(request, 8);
  await openApp(page, '#/projects/case_demo/overview');
  await expect(page.getByTestId('heartbeat')).toContainText('live');

  await request.post('/__mock/stall', { data: { seconds: 12 } });
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'stale', { timeout: 10_000 });
  await expect(page.getByTestId('stale-badge')).toBeVisible();
  await shot(page, '15-stale');

  await request.post('/__mock/disconnect', { data: { seconds: 3 } });
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', /reconnecting|disconnected/);
  await expect(page.getByTestId('conn-banner')).toContainText(/Last event seq \d+/);
  await expect(page.getByTestId('stale-badge')).toContainText('unknown');
  await shot(page, '16-reconnecting');
  await request.post('/__mock/emit', { data: { kind: 'job.log', payload: { message: 'emitted-while-offline' } } });
  await request.post('/__mock/stall', { data: { seconds: 0 } });

  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 20_000 });
  await page.getByTestId('tab-advanced').click();
  await page.getByRole('tab', { name: 'Raw logs' }).click();
  await expect(page.getByTestId('raw-log')).toContainText('emitted-while-offline');
});

test('create a project, start it and see raw counts with unknown scope', async ({ page, request }) => {
  await resetMock(request);
  await openApp(page, '#/new');
  await page.getByLabel(/^Name/).fill('Calculator');
  await page.getByLabel(/Source folder/).fill('C:\\Originals\\Calc');
  await page.getByLabel(/Output folder/).fill('C:\\Originals\\Calc\\out');
  await page.getByRole('radio', { name: /^Rust Native/ }).check();
  await expect(page.getByTestId('output-web').getByRole('radio')).toBeDisabled();
  await page.getByTestId('create-project').click();
  await expect(page.getByTestId('validation-summary')).toContainText('The output folder is inside the source folder.');
  await page.getByLabel(/Output folder/).fill('D:\\Rebuilds\\Calc');
  await page.getByRole('radio', { name: /AI-assisted/ }).check();
  await page.getByLabel(/Per-job budget/).fill('0.25');
  await page.getByTestId('create-project').click();
  await expect(page).toHaveURL(/#\/projects\/case_[0-9a-f]+\/overview$/);
  await expect(page.getByTestId('project-title')).toHaveText('Calculator');
  await page.getByTestId('btn-start').click();
  await expect(page.locator('[data-phase="analysis"]')).toContainText('3 files done · unknown scope');
  await expect(page.getByTestId('btn-pause')).toBeEnabled();
});
