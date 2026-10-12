// R6 acceptance: the RE workbench in the app against the REAL controller and real rizin on the pecli fixture. Skipped unless REAL_RE=1.
//
//   cd ui && REAL_RE=1 npx playwright test          # Windows: set REBUILD_STUDIO_TOOLS=D:/rs-test-tools (the default when it exists)
//
// playwright.config.ts starts controller/tests/support/live_re_server.py (StudioServices + uvicorn over a fresh temp data folder,
// REAL_RE_DATA, default <tmp>/rs-e2e-re; one project whose installation folder, a copy of fixtures/pecli/original, has been
// scanned; nothing analysed yet) and `vite` proxied to it. Nothing is mocked. The flow: open the project, open Functions and
// choose 'Analyse now', search the list, open a function (decompiled code and disassembly), open References and follow a caller,
// search a byte pattern and follow a hit, rename the function and see the new name in the list and the decompiled code, then
// Undo and see it restored. Evidence screenshots (JPEG, small) go to evidence/windows/20261006-jalon/r6/.
import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expectNoHorizontalOverflow } from './helpers';

const REAL_RE = process.env.REAL_RE === '1';
const DATA = process.env.REAL_RE_DATA ?? path.join(os.tmpdir(), 'rs-e2e-re');
const HERE = path.dirname(fileURLToPath(import.meta.url));
const EVIDENCE = path.resolve(HERE, '..', '..', 'evidence', 'windows', '20261006-jalon', 'r6');
const FN = '0x140001d63';          // pecli: the command lookup (compares the argument with strcmp); called from fcn.140001190
const CALLER = '0x140001190';
const NEW_NAME = 'lookup_command';

test.describe.configure({ mode: 'serial' });
test.skip(!REAL_RE, 'set REAL_RE=1 to run against the real controller (Playwright starts it with a temporary data folder)');
test.use({ viewport: { width: 1366, height: 800 } });

interface CaseInfo {
  case_id: string;
  module_id: string;
  name: string;
  rizin: boolean;
  rizin_version: string | null;
  reason: string | null;
}
const info = (): CaseInfo => JSON.parse(fs.readFileSync(path.join(DATA, 're-case.json'), 'utf8'));
const observed: string[] = [];

async function evidence(page: Page, name: string) {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  await page.screenshot({ path: path.join(EVIDENCE, `${name}.jpg`), type: 'jpeg', quality: 60 });
}

test('R6 end to end on pecli: open, analyse, search, decompile, references, byte search, rename, undo', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  const c = info();
  test.skip(!c.rizin, `rizin is not installed (${c.reason}); install it on the Tools page or point REBUILD_STUDIO_TOOLS at a folder with rizin to run this spec`);
  observed.push(`controller: real (live_re_server.py), rizin ${c.rizin_version}, case ${c.case_id}, module ${c.module_id}`);

  // ---- open the project
  await page.goto('/#/projects');
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected', { timeout: 30_000 });
  await expect(page.getByTestId('mock-banner')).toHaveCount(0);
  // the first-run offer to install tools automatically covers the bottom of the window: decline it (nothing is installed)
  const decline = page.getByRole('button', { name: 'No, I will click Install' });
  if (await decline.waitFor({ state: 'visible', timeout: 8_000 }).then(() => true, () => false)) await decline.click();
  await page.getByTestId(`project-link-${c.case_id}`).click();
  await expect(page).toHaveURL(new RegExp(`#/projects/${c.case_id}/overview`));
  await page.getByTestId('tab-functions').click();
  const tab = page.getByTestId('functions-tab');
  await expect(tab).toBeVisible();
  await expect(tab.getByTestId('fn-module-name')).toContainText('pecli.exe');

  // ---- Functions: nothing is analysed until the user asks
  const notYet = tab.getByTestId('fn-not-analyzed');
  await expect(notYet).toBeVisible({ timeout: 60_000 });
  await expect(notYet).toContainText('has not been analysed yet');
  await evidence(page, 'r6-01-functions-not-analysed');
  const t0 = Date.now();
  await tab.getByTestId('fn-analyze').click();
  await expect(tab.getByTestId('fn-table')).toBeVisible({ timeout: 5 * 60_000 });
  const summary = (await tab.getByTestId('fn-summary').textContent()) ?? '';
  expect(summary).toMatch(/Showing 1–100 of \d+ functions/);
  observed.push(`analysis: ${((Date.now() - t0) / 1000).toFixed(1)} s; ${summary.trim()}`);

  // ---- search the list and open a function: decompiled code, then disassembly
  await tab.getByTestId('fn-search').fill('1d63');
  await expect(tab.getByTestId('fn-summary')).toContainText('matching “1d63”');
  const row = tab.getByTestId(`fn-row-${FN}`);
  await expect(row).toContainText('fcn.140001d63');
  await evidence(page, 'r6-02-functions-search');
  await row.click();
  const pane = tab.getByTestId('fn-pane');
  await expect(pane.getByTestId('fn-pane-name')).toHaveText('fcn.140001d63');
  await expect(page).toHaveURL(new RegExp(`fn=${FN}`));
  const code = pane.getByTestId('fn-dec-code');
  await expect(code).toContainText('{', { timeout: 120_000 });
  await expect(pane.getByTestId('fn-dec-engine')).toBeVisible();
  observed.push(`decompiled by: ${(await pane.getByTestId('fn-dec-engine').textContent())?.trim()}`);
  await evidence(page, 'r6-03-decompiled');
  await pane.getByRole('tab', { name: 'Disassembly' }).click();
  const first = pane.getByTestId(`fn-op-${FN}`);
  await expect(first).toBeVisible({ timeout: 60_000 });
  const opBytes = await pane.locator('[data-testid^="fn-op-0x"] td.col-bytes').evaluateAll((tds) => tds.slice(0, 3).map((td) => (td.textContent ?? '').trim()));
  const flat = opBytes.join('').replace(/\s+/g, '');
  expect(flat).toMatch(/^([0-9a-f]{2})+$/);
  const pattern = (flat.slice(0, 2) + ' ?? ' + (flat.slice(4, 16).match(/../g) ?? []).join(' ')).trim();   // second byte as a wildcard
  observed.push(`first instructions of ${FN}: ${opBytes.join(' | ')} -> search pattern "${pattern}"`);
  await evidence(page, 'r6-04-disassembly');

  // ---- References: follow the caller
  await pane.getByRole('tab', { name: 'References' }).click();
  const caller = pane.getByTestId('fn-refs-to').getByTestId('fn-ref-to-0');
  await expect(caller).toContainText('fcn.140001190', { timeout: 60_000 });
  await expect(caller).toContainText('call');
  await evidence(page, 'r6-05-references');
  await caller.getByRole('link', { name: /Open fcn\.140001190/ }).click();
  await expect(pane.getByTestId('fn-pane-name')).toHaveText('fcn.140001190');
  await expect(page).toHaveURL(new RegExp(`fn=${CALLER}`));
  await expect(tab.getByTestId(`fn-row-${CALLER}`).or(tab.getByTestId('fn-summary'))).toBeVisible();

  // ---- Search: a byte pattern with a wildcard; follow the hit into Functions
  await tab.getByRole('tab', { name: 'Search' }).click();
  const input = tab.getByTestId('ms-pattern');
  await input.fill('0x' + flat.slice(0, 2));
  await expect(tab.getByTestId('ms-error')).toContainText('0x prefixes');                 // explained while typing
  await input.fill(pattern);
  await expect(tab.getByTestId('ms-error')).toHaveCount(0);
  await input.press('Enter');
  await expect(tab.getByTestId('ms-summary')).toContainText(/Showing 1–\d+ of \d+ match/, { timeout: 60_000 });
  const hit = tab.getByTestId('ms-table').getByRole('link', { name: `Open fcn.140001d63 at ${FN}` }).first();
  await expect(hit).toBeVisible();
  observed.push(`byte search: ${(await tab.getByTestId('ms-summary').textContent())?.trim()}`);
  await evidence(page, 'r6-06-byte-search');
  // narrow window: the results fold (section under the address), nothing scrolls sideways
  await page.setViewportSize({ width: 700, height: 800 });
  await expectNoHorizontalOverflow(page, 'search at 700 px');
  await evidence(page, 'r6-06b-byte-search-narrow');
  await page.setViewportSize({ width: 1366, height: 800 });
  await hit.click();
  await expect(tab.getByRole('tab', { name: 'Functions' })).toHaveAttribute('aria-selected', 'true');
  await expect(pane.getByTestId('fn-pane-name')).toHaveText('fcn.140001d63');
  await expect(page).toHaveURL(new RegExp(`fn=${FN}`));
  await expect(page).toHaveURL(/sq=/);                                                    // the search is kept for later

  // ---- rename: the new name in the pane, the list and the decompiled code
  await pane.getByTestId('fn-edit-name').click();
  const nameInput = pane.getByTestId('fn-name-edit-input');
  await nameInput.fill(NEW_NAME);
  await expect(pane.getByTestId('fn-name-edit-check')).toContainText('Valid');
  await nameInput.press('Enter');
  await expect(pane.getByTestId('fn-pane-name')).toHaveText(NEW_NAME, { timeout: 120_000 });
  await expect(tab.getByTestId(`fn-row-${FN}`)).toContainText(NEW_NAME, { timeout: 60_000 });
  await pane.getByRole('tab', { name: 'Decompiled' }).click();
  await expect(pane.getByTestId('fn-dec-code')).toContainText(NEW_NAME, { timeout: 120_000 });
  observed.push(`after rename: list row "${(await tab.getByTestId(`fn-row-${FN}`).innerText()).replace(/\s+/g, ' ').trim()}"`);
  await evidence(page, 'r6-07-renamed');

  // ---- Undo: the analysis name comes back everywhere
  await pane.getByRole('tab', { name: 'History' }).click();
  await expect(pane.getByTestId('fn-history')).toContainText('person', { timeout: 60_000 });
  await pane.getByTestId('fn-undo').click();
  await expect(pane.getByTestId('fn-pane-name')).toHaveText('fcn.140001d63', { timeout: 120_000 });
  await expect(tab.getByTestId(`fn-row-${FN}`)).not.toContainText(NEW_NAME, { timeout: 60_000 });
  await expect(tab.getByTestId(`fn-row-${FN}`)).toContainText('fcn.140001d63');
  await pane.getByRole('tab', { name: 'Decompiled' }).click();
  await expect(pane.getByTestId('fn-dec-code')).toContainText('{', { timeout: 120_000 });
  await expect(pane.getByTestId('fn-dec-code')).not.toContainText(NEW_NAME);
  await evidence(page, 'r6-08-undone');
});

test.afterAll(() => {
  if (observed.length) console.log(['[re-real-controller] observed:', ...observed.map((o) => `  - ${o}`)].join('\n'));
});
