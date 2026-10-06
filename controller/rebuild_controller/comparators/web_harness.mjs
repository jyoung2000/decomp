// Rebuild Studio web capture harness. Usage: node web_harness.mjs <spec.json> <out.json>
// Spec: {url, viewport:{width,height}, actions:[{type:'click'|'fill'|'press'|'goto'|'wait'|'offline'|'online'|'reload'|'eval', ...}],
//        capture:{text_selectors:[..], storage:true, screenshot:'path.png', offline_reload:true}, executablePath?}
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const harnessDir = process.env.REBUILD_HARNESS_DIR || process.cwd();
const require = createRequire(path.join(harnessDir, 'package.json'));
const { chromium } = require('playwright');

const [,, specPath, outPath] = process.argv;
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const exe = spec.executablePath || process.env.REBUILD_CHROMIUM || undefined;
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const context = await browser.newContext({ viewport: spec.viewport || { width: 1280, height: 800 }, locale: spec.locale || 'en-US',
  timezoneId: spec.timezone || 'UTC', deviceScaleFactor: spec.dpr || 1, reducedMotion: 'reduce', colorScheme: spec.colorScheme || 'light' });
const page = await context.newPage();
const consoleErrors = [];
page.on('pageerror', e => consoleErrors.push(String(e)));
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
const record = { url: spec.url, steps: [], console_errors: consoleErrors, environment: { viewport: spec.viewport, locale: spec.locale || 'en-US', tz: spec.timezone || 'UTC', chromium: browser.version() } };
await page.goto(spec.url, { waitUntil: 'load' });
if (spec.wait_for) await page.waitForSelector(spec.wait_for, { timeout: 10000 });
async function snapshot(label) {
  const snap = { label };
  const sels = (spec.capture && spec.capture.text_selectors) || ['body'];
  snap.text = {};
  for (const s of sels) { try { snap.text[s] = (await page.locator(s).first().innerText({ timeout: 3000 })).replace(/\r\n/g, '\n').trim(); } catch { snap.text[s] = null; } }
  if (spec.capture && spec.capture.storage) {
    snap.localStorage = await page.evaluate(() => Object.fromEntries(Object.keys(localStorage).sort().map(k => [k, localStorage.getItem(k)])));
    snap.hash = await page.evaluate(() => location.hash);
  }
  if (spec.capture && spec.capture.sw) {
    snap.serviceWorker = await page.evaluate(async () => { if (!('serviceWorker' in navigator)) return { supported: false }; const r = await navigator.serviceWorker.getRegistration(); return { supported: true, registered: !!r, scope: r ? r.scope : null, caches: 'caches' in window ? await caches.keys() : [] }; });
    snap.manifest = await page.evaluate(() => { const l = document.querySelector('link[rel=manifest]'); return l ? l.getAttribute('href') : null; });
  }
  return snap;
}
record.steps.push(await snapshot('initial'));
for (const a of spec.actions || []) {
  try {
    if (a.type === 'click') await page.click(a.selector, { timeout: 5000 });
    else if (a.type === 'fill') await page.fill(a.selector, a.value, { timeout: 5000 });
    else if (a.type === 'press') await page.press(a.selector || 'body', a.key, { timeout: 5000 });
    else if (a.type === 'goto') await page.goto(String(a.url).replace('{url}', spec.url), { waitUntil: 'load' });
    else if (a.type === 'wait') await page.waitForTimeout(a.ms || 200);
    else if (a.type === 'wait_for') await page.waitForSelector(a.selector, { timeout: a.timeout || 10000 });
    else if (a.type === 'offline') await context.setOffline(true);
    else if (a.type === 'online') await context.setOffline(false);
    else if (a.type === 'reload') await page.reload({ waitUntil: 'load' }).catch(e => { record.steps.push({ label: 'reload_error', error: String(e) }); });
    else if (a.type === 'eval') await page.evaluate(a.js);
    else if (a.type === 'wait_sw') await page.evaluate(async () => { if ('serviceWorker' in navigator) { const r = await navigator.serviceWorker.ready; await new Promise(res => setTimeout(res, 300)); return r.scope; } });
    if (a.snapshot !== false) record.steps.push(await snapshot(a.label || a.type));
  } catch (e) {
    record.steps.push({ label: a.label || a.type, error: String(e).split('\n')[0] });
  }
}
if (spec.capture && spec.capture.screenshot) {
  await page.screenshot({ path: spec.capture.screenshot, fullPage: false, animations: 'disabled' });
  record.screenshot = spec.capture.screenshot;
}
await browser.close();
fs.writeFileSync(outPath, JSON.stringify(record, null, 1));
