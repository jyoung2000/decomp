// REAL controller + REAL local model proof of the AI UI (Connections ladder, project AI tab, plan AI chips). Skipped unless REAL_AI=1.
//
//   cd controller && python tests/support/live_ui_server.py --data $D      # real StudioServices + uvicorn + Ollama
//   cd ui && REAL_AI=1 REAL_AI_DATA=$D npx playwright test                 # vite dev proxied to that controller
//
// The server waits for <D>/go before creating the implement_loop job; this spec touches it after the page is open so the
// feed is verified live (WebSocket `ai.activity`), not from a reload. Nothing is mocked.
import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { shot } from './helpers';

const REAL_AI = process.env.REAL_AI === '1';
const DATA = process.env.REAL_AI_DATA ?? '';
const LIVE_TIMEOUT = Number(process.env.REAL_AI_TIMEOUT_MS ?? 10 * 60_000);

test.describe.configure({ mode: 'serial' });
test.skip(!REAL_AI, 'set REAL_AI=1 and REAL_AI_DATA=<live_ui_server data dir>');

const info = (): { case_id: string; ladder_revision: number; model: string; missing: string } => JSON.parse(fs.readFileSync(path.join(DATA, 'ui-case.json'), 'utf8'));
const observed: string[] = [];

async function connected(page: Page) {
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  await expect(page.getByTestId('mock-banner')).toHaveCount(0);
}

test('Connections: real ladder version, both rungs, Local badge, missing model flagged unavailable', async ({ page }) => {
  const { model, missing } = info();
  await page.goto('/#/connections');
  await connected(page);
  const conn = page.getByTestId('connections').locator('table[aria-label="AI connections"] tbody tr').first();
  await expect(conn).toContainText('Ollama (this PC)');
  await expect(conn).not.toContainText('[object Object]');
  await expect(conn).toContainText(model);
  await conn.getByRole('button', { name: /^Probe / }).click();
  const section = page.getByTestId('ladder-section');
  await expect(section.getByTestId('ladder-version')).toHaveText(/Ladder version \d+/);
  observed.push(`ladder: ${await section.getByTestId('ladder-version').textContent()}`);
  const ladder = section.getByTestId('lad-interpretation-ladder');
  await expect(ladder).toBeVisible();
  const r0 = ladder.getByTestId('lad-interpretation-rung-0');
  const r1 = ladder.getByTestId('lad-interpretation-rung-1');
  await expect(r0).toContainText(missing);
  await expect(r1).toContainText(model);
  await expect(r0).toContainText('Primary');
  await expect(r1).toContainText('Fallback 1');
  await expect(r0.locator('[data-locality="local"]')).toContainText('Local');
  await expect(r1.locator('[data-locality="local"]')).toContainText('Local');
  await expect(r0.locator('.chip[data-status="model_unavailable"]')).toBeVisible({ timeout: 20_000 });
  await expect(r1.locator('.chip[data-status="model_unavailable"]')).toHaveCount(0);
  observed.push(`rung 1 availability: ${(await r0.locator('.chip[data-status]').first().textContent())?.trim()}`);
  await shot(page, 'real-ai-01-REAL-controller-connections-ladder');
});

test('AI tab: real policy, then live feed while the real model repairs the bug', async ({ page }) => {
  test.setTimeout(LIVE_TIMEOUT + 120_000);
  const { case_id: caseId, model, missing } = info();
  await page.goto(`/#/projects/${caseId}/ai`);
  await connected(page);
  const settings = page.getByTestId('ai-settings');
  await expect(settings).toBeVisible();
  await expect(page.getByTestId('ai-policy-policy').or(settings.locator('[data-testid$="-policy"]')).first()).toBeVisible();
  await expect(settings.getByLabel(/Where models may run/)).toHaveValue('local_only');
  await expect(settings.getByLabel(/Budget per job/)).toHaveValue('0');
  await expect(settings.getByRole('radio', { name: /Use the app ladder/ })).toBeChecked();
  await expect(page.getByTestId('ai-activity')).toContainText('No AI activity yet');
  await shot(page, 'real-ai-02-REAL-controller-ai-tab-before');

  const t0 = Date.now();
  fs.writeFileSync(path.join(DATA, 'go'), 'go');
  const feed = page.getByTestId('ai-activity');
  const seen = async (re: RegExp, label: string, timeout = LIVE_TIMEOUT) => {
    await expect(feed.getByTestId('activity-row').filter({ hasText: re }).first()).toBeVisible({ timeout });
    observed.push(`${((Date.now() - t0) / 1000).toFixed(0)} s: feed shows ${label}`);
  };
  await seen(/is not available/, `"is not available" (${missing})`);
  await expect(feed.getByTestId('activity-row').filter({ hasText: `trying ${model}` }).first()).toBeVisible();
  await expect(feed.getByTestId('fallback-reason').first()).toBeVisible();
  await shot(page, 'real-ai-03-REAL-live-feed-fallback');
  await seen(/proposed/, 'a model proposal');
  await seen(/Build (passed|failed)/, 'the build result');
  // the loop ends either with a verifier verdict or with "Next: stop"; both are reported, the model's success is not assumed
  await seen(/Verifier: \d+ of \d+|Next: stop/, 'the end of the loop (verifier verdict or stop)');
  const text = (await feed.getByTestId('activity-row').allInnerTexts()).join(' ');
  observed.push(`model outcome: build passed=${/Build passed/.test(text)}, verifier line=${/Verifier: \d+ of \d+/.exec(text)?.[0] ?? 'none'}`);
  const rows = await feed.getByTestId('activity-row').allInnerTexts();
  observed.push('feed (newest first):\n' + rows.map((r) => '      ' + r.replace(/\s+/g, ' ').trim()).join('\n'));
  // let the loop finish (it may retry) so the last screenshot shows the final state
  await page.waitForTimeout(3000);
  await expect(feed.getByTestId('activity-row').first()).toBeVisible();
  await expect(feed.getByText(/Ladder version \d+/).first()).toBeVisible();
  await shot(page, 'real-ai-04-REAL-live-feed-final');
});

test('Plan: the M-IMPL item carries the AI chip with the local primary model and origin badges', async ({ page }) => {
  const { case_id: caseId, model, missing } = info();
  await page.goto(`/#/projects/${caseId}/plan`);
  await connected(page);
  const impl = page.getByTestId(`plan-item-${caseId}:M-IMPL`);
  await expect(impl).toBeVisible({ timeout: 30_000 });
  const chip = page.getByTestId(`ai-chip-${caseId}:M-IMPL`);
  await expect(chip).toBeVisible();
  await expect(chip).toContainText('Interpretation');
  await expect(chip).toContainText(missing);
  await expect(chip.locator('[data-locality="local"]')).toContainText('Local');
  await expect(chip).toContainText('+1 fallback');
  await expect(chip.getByText('Works without AI')).toBeVisible();
  await chip.getByRole('button', { name: 'AI details' }).click();
  await expect(page.getByTestId(`ai-chip-${caseId}:M-IMPL`)).toContainText(model);
  await expect(page.locator('[data-origin]').first()).toBeVisible();
  const origins = await page.locator('[data-testid^="plan-item-"] [data-origin]').evaluateAll((els) => els.map((e) => e.getAttribute('data-origin')));
  observed.push(`plan origin badges: ${origins.join(', ')}`);
  expect(origins).toContain('deterministic');
  await shot(page, 'real-ai-05-REAL-controller-plan-ai-chip');
});

test.afterAll(() => {
  if (observed.length) console.log(['[ai-real-controller] observed:', ...observed.map((o) => `  - ${o}`)].join('\n'));
});
