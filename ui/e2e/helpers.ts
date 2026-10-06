import { expect, type APIRequestContext, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const TOKEN = 'e2e-token';
export const SCREENS = path.join(path.dirname(fileURLToPath(import.meta.url)), 'screens');

export async function resetMock(request: APIRequestContext, preload = 0) {
  const r = await request.post('/__mock/reset', { data: { preload } });
  expect(r.ok()).toBeTruthy();
}

export async function step(request: APIRequestContext, count = 1) {
  const r = await request.post('/__mock/step', { data: { count } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()) as { step: number };
}

export async function openApp(page: Page, hash = '#/') {
  await page.goto(`/?token=${TOKEN}${hash}`);
  await expect(page.getByTestId('conn-banner')).toHaveAttribute('data-state', 'connected');
}

export async function shot(page: Page, name: string) {
  fs.mkdirSync(SCREENS, { recursive: true });
  await page.screenshot({ path: path.join(SCREENS, `${name}.png`), fullPage: false });
}

/** No horizontal page overflow: neither the document nor the main scroll container may scroll sideways. */
export async function expectNoHorizontalOverflow(page: Page, label = '') {
  const r = await page.evaluate(() => {
    const doc = document.documentElement;
    const main = document.querySelector('.main-scroll') as HTMLElement | null;
    const offenders: string[] = [];
    if (main) {
      const limit = main.getBoundingClientRect().right + 1;
      const clipped = (el: HTMLElement) => {
        // content inside an element that clips or scrolls horizontally (ellipsis, table wrappers) is not page overflow
        for (let a = el.parentElement; a && a !== main; a = a.parentElement) {
          const ox = getComputedStyle(a).overflowX;
          if (ox !== 'visible') return true;
        }
        return false;
      };
      for (const el of Array.from(main.querySelectorAll<HTMLElement>('*'))) {
        if (el.closest('.sr-only')) continue;
        const b = el.getBoundingClientRect();
        if (b.width > 0 && b.right > limit && !clipped(el)) offenders.push(`${el.tagName.toLowerCase()}.${el.className}: ${(el.textContent ?? '').slice(0, 40)}`.slice(0, 120));
      }
    }
    return { doc: doc.scrollWidth - doc.clientWidth, main: main ? main.scrollWidth - main.clientWidth : 0, offenders: offenders.slice(0, 5) };
  });
  expect(r.doc, `${label}: document horizontal overflow`).toBeLessThanOrEqual(0);
  expect(r.main, `${label}: main horizontal overflow (${r.offenders.join(', ')})`).toBeLessThanOrEqual(0);
  expect(r.offenders, `${label}: elements past the right edge`).toEqual([]);
}
