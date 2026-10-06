// Integration check against the REAL Python controller (not the mock). Skipped unless REAL_CONTROLLER=1.
//
//   cd controller && REBUILD_STUDIO_DATA=$D python -m rebuild_controller.cli.main serve --port 8765 --data-dir $D
//   cd ui && REAL_CONTROLLER=1 REAL_CONTROLLER_DATA=$D npx playwright test
//
// playwright.config.ts starts `vite` (dev proxy → controller) with VITE_CONTROLLER_URL/VITE_CONTROLLER_TOKEN from
// REAL_CONTROLLER_URL and <REAL_CONTROLLER_DATA>/controller.json. The last test stops (SIGSTOP) and then kills the
// controller (pid from controller.json) to check the stale/unknown state; set REAL_CONTROLLER_KEEP=1 to skip that.
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { shot } from './helpers';

const REAL = process.env.REAL_CONTROLLER === '1';
const SOURCE = process.env.REAL_SOURCE ?? '/home/user/decomp/fixtures/webapp/original';
const DATA = process.env.REAL_CONTROLLER_DATA ?? '';
const BUILD_TIMEOUT = Number(process.env.REAL_BUILD_TIMEOUT_MS ?? 10 * 60_000);

test.describe.configure({ mode: 'serial' });
test.skip(!REAL, 'set REAL_CONTROLLER=1 (and REAL_CONTROLLER_DATA) to run against the Python controller');

let caseUrl = '';
const observed: string[] = [];

function controllerInfo(): { port: number; token: string; pid: number } {
  return JSON.parse(fs.readFileSync(path.join(DATA, 'controller.json'), 'utf8'));
}

async function planRevision(page: Page): Promise<number> {
  const t = (await page.getByText(/^Plan revision \S+/).first().textContent()) ?? '';
  const m = t.match(/Plan revision (\d+)/);
  return m ? Number(m[1]) : 0;
}

test('create a web project through the form, start it and watch live events', async ({ page }) => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'rs-real-out-'));
  await page.goto('/#/new');
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  await expect(page.getByTestId('mock-banner')).toHaveCount(0);

  await page.getByLabel(/^Name/).fill(`Webapp real check ${new Date().toISOString().slice(11, 19)}`);
  await page.getByLabel(/Source folder/).fill(SOURCE);
  await page.getByLabel(/Output folder/).fill(out);
  await page.getByRole('radio', { name: /^HTML \/ CSS \/ JS/ }).check();
  await page.getByTestId('output-web').getByRole('radio').check();
  await page.getByRole('radio', { name: /^No AI/ }).check();
  await page.getByLabel('Execute the original program to capture its behaviour').check();
  await page.getByTestId('launch-web').check();
  await expect(page.getByLabel('Entry page')).toHaveValue('index.html');
  await shot(page, 'real-00-new-project');
  await page.getByTestId('create-project').click();
  await expect(page).toHaveURL(/#\/projects\/[^/]+\/overview$/, { timeout: 30_000 });
  caseUrl = page.url().replace(/\/overview$/, '');
  observed.push(`case created: ${caseUrl.split('/').pop()}`);
  await expect(page.getByTestId('project-title')).toContainText('Webapp real check');

  const revBefore = await planRevision(page);
  await page.getByTestId('btn-start').click();
  // live: jobs appear and the plan revision increments
  await expect(page.getByTestId('job-counts')).not.toHaveText('none yet', { timeout: 60_000 });
  await expect.poll(() => planRevision(page), { timeout: 60_000 }).toBeGreaterThan(revBefore);
  observed.push(`plan revision ${revBefore} → ${await planRevision(page)}; jobs: ${await page.getByTestId('job-counts').textContent()}`);
  await expect(page.getByTestId('latest-event')).not.toHaveText('No events yet');
  await shot(page, 'real-01-overview-running');
  await page.getByTestId('tab-plan').click();
  await expect(page.locator('[data-testid^="plan-item-"]').first()).toBeVisible({ timeout: 30_000 });
  await shot(page, 'real-02-plan-running');
  await page.getByTestId('tab-advanced').click();
  await page.getByRole('tab', { name: 'Raw logs' }).click();
  await expect(page.getByTestId('raw-log')).toBeVisible();
  await shot(page, 'real-03-raw-log-running');
  await page.getByTestId('tab-overview').click();
});

/** Pipeline is stuck when nothing is queued/running and a job failed: report the controller's own error. */
async function stuckReason(request: APIRequestContext, caseId: string): Promise<string | null> {
  const r = await request.get(`/__controller/cases/${caseId}/jobs`, { headers: { Authorization: `Bearer ${controllerInfo().token}` } });
  if (!r.ok()) return null;
  const jobs = (await r.json()) as { stage: string; title: string; state: string; error?: string | null }[];
  if (jobs.some((j) => j.state === 'queued' || j.state === 'running')) return null;
  const failed = jobs.filter((j) => j.state === 'failed');
  return failed.length ? failed.map((j) => `${j.stage} (${j.title}): ${j.error ?? 'failed'}`).join(' | ') : null;
}

test('Preview & Test shows a real candidate once candidate.built arrives, and opening it returns a URL', async ({ page, context, request }) => {
  test.setTimeout(BUILD_TIMEOUT + 3 * 60_000);
  expect(caseUrl, 'previous test created the case').not.toBe('');
  await page.goto(caseUrl.replace(/^https?:\/\/[^/]+/, '') + '/preview');
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  const t0 = Date.now();
  // live update: no reload while waiting; the event stream must bring the candidate in
  const built = page.locator('[data-testid^="candidate-"]').filter({ has: page.locator('[data-status="built"]') }).first();
  const failed = page.locator('[data-testid^="candidate-"]').filter({ has: page.locator('[data-status="failed"]') });
  const caseId = caseUrl.split('/').pop()!;
  let stuck: string | null = null;
  await expect
    .poll(
      async () => {
        if (await failed.count()) return 'failed';
        if (await built.count()) return 'built';
        stuck = await stuckReason(request, caseId);
        return stuck ? 'stuck' : 'waiting';
      },
      { timeout: BUILD_TIMEOUT, intervals: [2000, 5000, 10000] },
    )
    .not.toBe('waiting');
  if (stuck) observed.push(`pipeline stuck: ${stuck}`);
  expect(stuck, 'the controller pipeline stopped with a failed job before any candidate was built').toBeNull();
  expect(await failed.count(), 'a candidate build failed — see Advanced → Raw logs / controller log').toBe(0);
  observed.push(`candidate built after ${Math.round((Date.now() - t0) / 1000)} s`);
  await expect(built.getByText(/sha256:|[0-9a-f]{12}/).first()).toBeVisible();

  const real = page.locator('[data-testid^="preview-"]').filter({ has: page.getByTestId('preview-kind').filter({ hasText: 'Real build' }) }).first();
  await expect(real).toBeVisible({ timeout: 60_000 });
  await shot(page, 'real-04-preview-built');
  const popup = context.waitForEvent('page', { timeout: 15_000 }).catch(() => null);
  await real.getByTestId('open-preview').click();
  const link = real.getByTestId('preview-url');
  await expect(link).toBeVisible({ timeout: 30_000 });
  const href = (await link.getAttribute('href')) ?? '';
  expect(href).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/.+/);
  observed.push(`preview URL: ${href}`);
  const p = await popup;
  if (p) {
    await p.waitForLoadState();
    observed.push(`preview page title: ${await p.title()}`);
    await p.screenshot({ path: path.join(path.dirname(new URL(import.meta.url).pathname), 'screens', 'real-05-preview-page.png') });
    await p.close();
  }
  await shot(page, 'real-06-preview-open');
  await real.getByTestId('stop-preview').click();
  // the verifier's verdict reaches the candidate card live (verification.completed → refresh), without a reload
  const verdict = built.locator('.chip[data-status]').filter({ hasText: 'verification:' });
  await expect(verdict).not.toHaveAttribute('data-status', 'untested', { timeout: 3 * 60_000 });
  observed.push(`candidate verification (live): ${await verdict.getAttribute('data-status')}`);
  await page.getByTestId('tab-comparisons').click();
  await expect(page.getByRole('table', { name: 'Comparison results' })).toBeVisible({ timeout: 60_000 });
  observed.push(`comparisons: ${(await page.getByText(/\d+ rows · /).first().textContent())?.trim()}`);
  await shot(page, 'real-07-comparisons');
  await page.getByTestId('tab-overview').click();
  await shot(page, 'real-08-overview-after-build');
});

test('feedback, triage and every view work against real controller data', async ({ page }) => {
  expect(caseUrl).not.toBe('');
  const ws = caseUrl.replace(/^https?:\/\/[^/]+/, '');
  await page.goto(ws + '/feedback');
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  await page.getByLabel('About', { exact: true }).selectOption('preview');
  await page.getByLabel('Target', { exact: true }).selectOption({ index: 1 });
  await page.getByLabel('Comment *').fill('Real controller: note list spacing differs');
  await page.getByLabel('Priority').selectOption('high');
  await page.getByTestId('submit-feedback').click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  const item = page.getByTestId('feedback-list').locator('li').filter({ hasText: 'note list spacing differs' }).first();
  await expect(item.locator('[data-status="received"]').first()).toBeVisible();
  await page.reload();
  await expect(item.locator('[data-status="received"]').first()).toBeVisible({ timeout: 30_000 });
  observed.push('feedback persisted by the controller (survives reload)');
  await item.getByTestId('triage-feedback').click();
  const dialog = page.getByRole('dialog', { name: 'Triage feedback' });
  await dialog.getByTestId('triage-create-work').check();
  await dialog.getByLabel('Note').fill('convert to plan work');
  await dialog.getByTestId('triage-submit').click();
  await expect(dialog).toBeHidden();
  await expect(item.locator('[data-status="queued"]').first()).toBeVisible();
  await expect(item.getByTestId('linked-items')).toBeVisible();
  observed.push(`triage create_work → ${(await item.getByTestId('linked-items').textContent())?.trim()}`);
  await shot(page, 'real-11-feedback-triaged');

  await page.getByTestId('tab-advanced').click();
  for (const tab of ['Jobs', 'Modules', 'Evidence', 'Raw logs']) {
    await page.getByRole('tab', { name: tab }).click();
    await expect(page.getByRole('alert')).toHaveCount(0);
  }
  for (const [hash, name] of [
    ['#/projects', 'projects'],
    ['#/connections', 'connections'],
    ['#/knowledge', 'knowledge'],
    ['#/settings', 'settings'],
  ] as const) {
    await page.goto('/' + hash);
    await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
    await page.waitForLoadState('networkidle');
    const alerts = await page.getByRole('alert').allTextContents();
    if (alerts.length) observed.push(`${name}: ${alerts.join(' | ').slice(0, 400)}`);
    expect(alerts, `${name} shows an error`).toEqual([]);
    await shot(page, `real-12-${name}`);
  }
});

test('stale/unknown within 2× heartbeat when the controller stops answering, and when it is killed', async ({ page }) => {
  test.skip(process.env.REAL_CONTROLLER_KEEP === '1', 'REAL_CONTROLLER_KEEP=1');
  expect(caseUrl).not.toBe('');
  const { pid } = controllerInfo();
  await page.goto(caseUrl.replace(/^https?:\/\/[^/]+/, '') + '/overview');
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  const hb = Number((await page.getByTestId('heartbeat').textContent())?.match(/Expected every (\d+) s/)?.[1] ?? 30);
  test.setTimeout((2 * hb + 60) * 1000 + 60_000);

  // 1) hung controller: socket stays open, no events → stale after 2× heartbeat
  process.kill(pid, 'SIGSTOP');
  const t0 = Date.now();
  try {
    await expect(page.getByTestId('stale-badge')).toBeVisible({ timeout: (2 * hb + 10) * 1000 });
    const secs = (Date.now() - t0) / 1000;
    observed.push(`SIGSTOP → stale after ${secs.toFixed(1)} s (heartbeat ${hb} s, limit ${2 * hb} s)`);
    expect(secs).toBeLessThanOrEqual(2 * hb + 5);
    await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', /stale|reconnecting|disconnected/);
    await shot(page, 'real-09-stale-hung');
  } finally {
    process.kill(pid, 'SIGCONT');
  }

  // 2) killed controller: socket closes → unknown/disconnected right away
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 60_000 });
  process.kill(pid, 'SIGKILL');
  const t1 = Date.now();
  await expect(page.getByTestId('stale-badge')).toContainText('unknown', { timeout: 2 * hb * 1000 });
  observed.push(`SIGKILL → "unknown — disconnected" after ${((Date.now() - t1) / 1000).toFixed(1)} s`);
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', /reconnecting|disconnected/);
  await shot(page, 'real-10-killed');
});

test.afterAll(() => {
  if (observed.length) console.log(['[real-controller] observed:', ...observed.map((o) => `  - ${o}`)].join('\n'));
});
