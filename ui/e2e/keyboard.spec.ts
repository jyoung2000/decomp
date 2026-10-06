import { test, expect, type Page } from '@playwright/test';
import { openApp, resetMock, shot, step } from './helpers';

async function focusedInfo(page: Page) {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { text: (el.getAttribute('aria-label') || el.textContent || '').trim(), tag: el.tagName, outline: cs.outlineStyle, outlineWidth: cs.outlineWidth, testid: el.dataset.testid ?? null };
  });
}

async function tabTo(page: Page, match: (i: NonNullable<Awaited<ReturnType<typeof focusedInfo>>>) => boolean, max = 60) {
  for (let i = 0; i < max; i++) {
    await page.keyboard.press('Tab');
    const info = await focusedInfo(page);
    if (info && match(info)) return info;
  }
  throw new Error('focus target not reached by Tab');
}

test.beforeEach(async ({ request }) => {
  await resetMock(request);
  await step(request, 6);
});

test('everything is reachable by keyboard with a visible focus ring', async ({ page }) => {
  await openApp(page, '#/projects');
  await page.keyboard.press('Tab');
  const first = await focusedInfo(page);
  expect(first?.text).toBe('Skip to content');

  const nav = await tabTo(page, (i) => i.testid === 'nav-connections');
  expect(nav.outline).not.toBe('none');
  expect(parseFloat(nav.outlineWidth)).toBeGreaterThan(0);
  await shot(page, '17-keyboard-focus-ring');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/connections$/);

  // workspace tabs: arrow keys move focus, Enter activates
  await page.goto('/?token=e2e-token#/projects/case_demo/overview');
  await expect(page.getByTestId('phase-progress')).toBeVisible();
  await page.getByTestId('tab-overview').focus();
  await page.keyboard.press('ArrowRight');
  expect((await focusedInfo(page))?.testid).toBe('tab-plan');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/plan$/);

  // plan tree expands from the keyboard
  await tabTo(page, (i) => /Expand M1 /.test(i.text));
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('plan-details-M1')).toBeVisible();
  await page.keyboard.press('Space');
  await expect(page.getByTestId('plan-details-M1')).toBeHidden();
});

test('dialogs open from the keyboard, trap focus and close with Esc', async ({ page }) => {
  await openApp(page, '#/projects/case_demo/overview');
  await expect(page.getByTestId('btn-stop')).toBeEnabled();
  await page.getByTestId('btn-stop').focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Stop this rebuild?' });
  await expect(dialog).toBeVisible();
  for (let i = 0; i < 5; i++) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  }
  await shot(page, '18-dialog');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect((await focusedInfo(page))?.testid).toBe('btn-stop');
});
