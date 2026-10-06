import { test, expect, type Page } from '@playwright/test';
import { expectNoHorizontalOverflow, openApp, resetMock, shot, step } from './helpers';

const WS = '#/projects/case_demo';
const VIEWS: { name: string; hash: string; ready: (p: Page) => Promise<void> }[] = [
  { name: 'projects', hash: '#/projects', ready: async (p) => void (await expect(p.getByRole('link', { name: 'Inventory Tool (demo)' }).first()).toBeVisible()) },
  { name: 'overview', hash: `${WS}/overview`, ready: async (p) => void (await expect(p.getByTestId('phase-progress')).toBeVisible()) },
  { name: 'plan', hash: `${WS}/plan`, ready: async (p) => void (await expect(p.getByTestId('plan-item-M1')).toBeVisible()) },
  { name: 'preview', hash: `${WS}/preview`, ready: async (p) => void (await expect(p.getByTestId('preview-kind').first()).toBeVisible()) },
  { name: 'feedback', hash: `${WS}/feedback`, ready: async (p) => void (await expect(p.getByRole('heading', { name: 'Give feedback' })).toBeVisible()) },
  { name: 'comparisons', hash: `${WS}/comparisons`, ready: async (p) => void (await expect(p.getByRole('table', { name: 'Comparison results' })).toBeVisible()) },
  { name: 'advanced', hash: `${WS}/advanced`, ready: async (p) => void (await expect(p.getByRole('table', { name: 'Jobs' })).toBeVisible()) },
  { name: 'connections', hash: '#/connections', ready: async (p) => void (await expect(p.getByRole('table', { name: 'AI connections' })).toBeVisible()) },
  { name: 'knowledge', hash: '#/knowledge', ready: async (p) => void (await expect(p.getByRole('table', { name: 'Knowledge entries' })).toBeVisible()) },
  { name: 'settings', hash: '#/settings', ready: async (p) => void (await expect(p.getByRole('table', { name: 'Dependency doctor' })).toBeVisible()) },
  { name: 'new-project', hash: '#/new', ready: async (p) => void (await expect(p.getByRole('heading', { name: 'New project' })).toBeVisible()) },
];

test.beforeEach(async ({ request }) => {
  await resetMock(request);
  await step(request, 8); // plan → running → v1 preview → feedback triage → v2 (v1 stale)
});

test('navigates every view through the sidebar and workspace tabs', async ({ page }) => {
  await openApp(page, '#/projects');
  await expect(page.getByTestId('mock-banner')).toHaveText(/Demo data – mock controller/);
  await page.getByTestId('project-link-case_demo').click();
  await expect(page).toHaveURL(/#\/projects\/case_demo\/overview$/);
  await expect(page.getByTestId('project-title')).toHaveText('Inventory Tool (demo)');
  await shot(page, '01-overview-1920');

  await page.getByTestId('tab-plan').click();
  await page.getByRole('button', { name: /Expand M2 Recover program logic/ }).click();
  await expect(page.getByTestId('plan-details-M2')).toBeVisible();
  for (const s of ['plan-discovery', 'plan-deferred', 'plan-unsupported', 'plan-revisions']) await expect(page.getByTestId(s)).toBeVisible();
  await shot(page, '02-plan-1920');

  await page.getByTestId('tab-preview').click();
  await expect(page.getByTestId('preview-pv_2_real').getByTestId('preview-kind')).toHaveText('Real build');
  await expect(page.getByTestId('preview-pv_1_real').getByTestId('stale-preview')).toBeVisible();
  await expect(page.getByTestId('lkg-compare')).toBeVisible();
  await shot(page, '03-preview-1920');

  await page.getByTestId('tab-feedback').click();
  await expect(page.getByTestId('fb-plan-rev')).toHaveValue(/^r\d+$/);
  await shot(page, '04-feedback-1920');

  await page.getByTestId('tab-comparisons').click();
  await page.getByLabel('Candidate').selectOption({ label: /Version 1/ } as never).catch(async () => {
    const opts = await page.getByLabel('Candidate').locator('option').allTextContents();
    await page.getByLabel('Candidate').selectOption({ label: opts.find((o) => o.startsWith('Version 1'))! });
  });
  const screensRow = page.getByRole('row').filter({ hasText: 'Screens' });
  await expect(screensRow.getByTestId('tolerance')).toContainText('Not exact — declared tolerance: Max diff ratio ≤ 0.01');
  await expect(page.getByText(/pixel-perfect/i)).toHaveCount(0);
  await screensRow.getByRole('button', { name: /details/ }).click();
  await expect(page.getByTestId('screenshots').locator('img')).toHaveCount(3);
  await shot(page, '05-comparisons-1920');

  await page.getByTestId('tab-advanced').click();
  await page.getByRole('tab', { name: 'Raw logs' }).click();
  await expect(page.getByTestId('raw-log')).toBeVisible();
  await shot(page, '06-advanced-logs-1920');

  await page.getByTestId('nav-connections').click();
  await expect(page.getByText('External client handoff').first()).toBeVisible();
  await expect(page.getByTestId('hermes')).toBeVisible();
  await shot(page, '07-connections-1920');

  await page.getByTestId('nav-knowledge').click();
  await page.getByRole('button', { name: /^Legacy \.plg header/ }).click();
  await expect(page.getByTestId('knowledge-detail')).toContainText('length field misread');
  await shot(page, '08-knowledge-1920');

  await page.getByTestId('nav-settings').click();
  await expect(page.getByRole('table', { name: 'Dependency doctor' })).toContainText('rz-ghidra');
  await shot(page, '09-settings-1920');

  await page.getByTestId('nav-new').click();
  await page.getByTestId('create-project').click();
  await expect(page.getByTestId('validation-summary')).toContainText('No source folder was chosen.');
  await shot(page, '10-new-project-errors-1920');

  // selection persists across views and reloads (URL + localStorage)
  await page.reload();
  await expect(page.getByTestId('nav-workspace')).toContainText('Inventory Tool (demo)');
  await page.goto('/?token=e2e-token#/');
  await expect(page).toHaveURL(/#\/projects\/case_demo\/overview$/);
});

for (const vp of [
  { width: 1024, height: 700 },
  { width: 1920, height: 1080 },
]) {
  test(`no horizontal overflow at ${vp.width}x${vp.height}`, async ({ page }) => {
    await page.setViewportSize(vp);
    await openApp(page, '#/projects');
    for (const v of VIEWS) {
      await page.goto(`/?token=e2e-token${v.hash}`);
      await v.ready(page);
      await expectNoHorizontalOverflow(page, v.name);
      if (vp.width === 1024) await shot(page, `vp1024-${v.name}`);
    }
  });
}

test('no horizontal overflow at 200% scaling (1280x720 CSS px, DPR 2)', async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  for (const v of VIEWS) {
    await page.goto(`/?token=e2e-token${v.hash}`);
    await v.ready(page);
    await expectNoHorizontalOverflow(page, v.name);
  }
  await shot(page, 'dpr2-new-project');
  await ctx.close();
});
