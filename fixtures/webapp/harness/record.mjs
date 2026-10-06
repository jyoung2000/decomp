// Records (default) or checks (--check) the Pocket Notes oracle with Playwright + preinstalled Chromium.
//   npm test                         record against ../original and ../original-electron (asar extracted), write ../expected/
//   npm run check -- [--site DIR]    run the same scenarios against DIR (default ../original), diff vs ../expected
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import * as asar from '@electron/asar';
import { startServer } from './server.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FX = path.resolve(HERE, '..');
const EXPECTED_DIR = path.join(FX, 'expected');
const SCREENS = path.join(EXPECTED_DIR, 'screens');
const args = process.argv.slice(2);
const CHECK = args.includes('--check');
const siteArg = args.includes('--site') ? path.resolve(args[args.indexOf('--site') + 1]) : path.join(FX, 'original');
const LS_KEY = 'pocket-notes:v1';
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

async function snap(page, extra = {}) {
  const s = await page.evaluate(() => ({
    title: document.title,
    h1: document.querySelector('h1').innerText,
    nav: [...document.querySelectorAll('nav a')].map((a) => ({ text: a.innerText, active: a.className === 'active', href: a.getAttribute('href') })),
    hash: location.hash,
    app_text: document.getElementById('app').innerText,
    notes: [...document.querySelectorAll('#note-list li .text')].map((n) => n.innerText),
    banner: { hidden: document.getElementById('banner').hidden, text: document.getElementById('banner').innerText },
    error: { hidden: document.getElementById('error').hidden, text: document.getElementById('error').innerText, role: document.getElementById('error').getAttribute('role') },
    sw_status: document.getElementById('sw-status').innerText,
    localStorage: Object.fromEntries(Object.keys(localStorage).sort().map((k) => [k, localStorage.getItem(k)])),
  }));
  return { ...s, ...extra };
}

const swReady = (page) => page.waitForFunction(() => document.getElementById('sw-status').textContent === 'offline support: ready' && !!navigator.serviceWorker.controller);
const cacheKeys = (page) => page.evaluate(async () => (await caches.keys()).sort());
const addNote = async (page, text, how = 'click') => {
  await page.fill('#note-input', text);
  if (how === 'click') await page.click('#add-btn'); else await page.press('#note-input', 'Enter');
};

/** Scenarios that work for both http (original) and file:// (electron asar) targets. */
async function commonScenarios(browser, urlFor, withSW) {
  const out = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(urlFor('#/notes'));
  if (withSW) await swReady(page);
  out.push({ id: 'initial_load', feature: 'webapp.render_notes', ...(await snap(page)), cache_keys: withSW ? await cacheKeys(page) : null });
  await addNote(page, 'Buy milk', 'click');
  await addNote(page, 'Walk dog', 'enter');
  await page.fill('#note-input', '   ');
  await page.press('#note-input', 'Enter');
  out.push({ id: 'add_notes', feature: 'webapp.add_note', ...(await snap(page)) });
  await page.reload();
  if (withSW) await swReady(page);
  out.push({ id: 'persistence_after_reload', feature: 'webapp.localstorage_state', ...(await snap(page)) });
  await page.click('#nav-about');
  await page.waitForSelector('#version');
  if (withSW) await page.waitForFunction(() => (document.getElementById('cache-info') || { innerText: 'Cache: none' }).innerText !== 'Cache: none');
  out.push({ id: 'route_about_click', feature: 'webapp.hash_routes', ...(await snap(page)), cache_info_text: await page.innerText('#cache-info') });
  await page.goto(urlFor('#/notes'));
  await page.reload();
  out.push({ id: 'route_back_notes_direct', feature: 'webapp.hash_routes', ...(await snap(page)) });
  await page.click('li[data-id="1"] .delete');
  out.push({ id: 'delete_note', feature: 'webapp.delete_note', ...(await snap(page)) });
  await ctx.close();

  // storage quota failure (real DOMException thrown by Storage.setItem) in a fresh context
  const q = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  await q.addInitScript((key) => {
    const orig = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === key && window.__quota) throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
      return orig.call(this, k, v);
    };
  }, LS_KEY);
  const qp = await q.newPage();
  await qp.goto(urlFor('#/notes'));
  if (withSW) await swReady(qp);
  await addNote(qp, 'kept note');
  await qp.evaluate(() => { window.__quota = true; });
  await addNote(qp, 'overflow note');
  out.push({ id: 'quota_error_real_setitem', feature: 'webapp.quota_error_state', ...(await snap(qp)) });
  if (withSW) { /* screenshot taken in caller */ }
  await qp.evaluate(() => { window.__quota = false; });
  await addNote(qp, 'after recovery');
  out.push({ id: 'quota_recovery', feature: 'webapp.quota_error_state', ...(await snap(qp)) });
  await q.close();

  // simulated quota through the app's own flag
  const f = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const fp = await f.newPage();
  await fp.goto(urlFor('#/notes', '?simulate-quota=1'));
  if (withSW) await swReady(fp);
  await addNote(fp, 'cannot save');
  out.push({ id: 'quota_error_flag', feature: 'webapp.quota_error_state', ...(await snap(fp)) });
  await f.close();
  return out;
}

async function screenshots(browser, urlFor, withSW, dir, prefix) {
  const shots = [];
  const take = async (name, page) => {
    await page.mouse.move(0, 0);
    const buf = await page.screenshot({ clip: { x: 0, y: 0, width: 1280, height: 800 } });
    const file = path.join(dir, `${prefix}${name}_1280x800.png`);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(file, buf);
    shots.push({ name, file: path.relative(EXPECTED_DIR, file).split(path.sep).join('/'), width: 1280, height: 800, sha256: sha(buf), bytes: buf.length });
  };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  await p.goto(urlFor('#/notes'));
  if (withSW) await swReady(p);
  await addNote(p, 'Buy milk');
  await addNote(p, 'Walk dog');
  await p.evaluate(() => document.activeElement && document.activeElement.blur());
  await take('notes', p);
  await p.click('#nav-about');
  if (withSW) await p.waitForFunction(() => (document.getElementById('cache-info') || { innerText: 'Cache: none' }).innerText !== 'Cache: none');
  await take('about', p);
  await ctx.close();
  const q = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const qp = await q.newPage();
  await qp.goto(urlFor('#/notes', '?simulate-quota=1'));
  if (withSW) await swReady(qp);
  await addNote(qp, 'cannot save');
  await qp.evaluate(() => document.activeElement && document.activeElement.blur());
  await take('quota_error', qp);
  await q.close();
  return shots;
}

async function offlineScenarios(browser, srv) {
  const out = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(`${srv.base}/#/notes`);
  await swReady(page);
  await addNote(page, 'offline survivor');
  const online = await snap(page, { cache_keys: await cacheKeys(page) });
  await ctx.setOffline(true);
  srv.state.down = true; // Playwright's setOffline does not cover SW fetches: also make the server unreachable
  const resp = await page.reload();
  await page.waitForSelector('#note-list li');
  out.push({ id: 'offline_reload', feature: 'webapp.offline_reload', online_before: online, from_service_worker: resp.fromServiceWorker(), status: resp.status(),
             ...(await snap(page)), navigator_onLine: await page.evaluate(() => navigator.onLine) });
  await page.goto(`${srv.base}/#/about`);
  await page.reload();
  await page.waitForFunction(() => document.getElementById('cache-info') && (document.getElementById('cache-info') || { innerText: 'Cache: none' }).innerText !== 'Cache: none');
  out.push({ id: 'offline_route_about', feature: 'webapp.offline_reload', ...(await snap(page)), cache_info_text: await page.innerText('#cache-info') });
  const missing = await page.goto(`${srv.base}/does-not-exist.html`);
  out.push({ id: 'offline_navigation_fallback', feature: 'webapp.offline_reload', status: missing.status(), from_service_worker: missing.fromServiceWorker(), title: await page.title(), h1: await page.innerText('h1') });
  await page.goto(`${srv.base}/#/notes`);
  const fetchResult = await page.evaluate(() => fetch('/never-cached.txt').then((r) => ({ ok: r.ok, status: r.status })).catch((e) => ({ error: e.name })));
  out.push({ id: 'offline_uncached_asset_fails', feature: 'webapp.offline_reload', fetch_result: fetchResult });
  srv.state.down = false;
  await ctx.setOffline(false);
  await ctx.close();
  return out;
}

async function upgradeScenario(browser, srv) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(`${srv.base}/#/notes`);
  await swReady(page);
  const versionViaMessage = () => page.evaluate(() => new Promise((res) => {
    navigator.serviceWorker.addEventListener('message', (e) => res(e.data), { once: true });
    navigator.serviceWorker.controller.postMessage({ type: 'GET_VERSION' });
  }));
  const before = { cache_keys: await cacheKeys(page), sw_message: await versionViaMessage() };
  const original = fs.readFileSync(path.join(srv.root, 'sw.js'), 'utf8');
  const bumped = original.replace("const CACHE_VERSION = 'notes-cache-v1'", "const CACHE_VERSION = 'notes-cache-v2'");
  if (bumped === original) throw new Error('could not bump CACHE_VERSION in sw.js');
  srv.overrides.set('/sw.js', bumped);
  await page.evaluate(() => navigator.serviceWorker.getRegistration().then((r) => r.update()));
  // poll from node: the old cache is removed by the new worker's activate handler, which takes a moment
  for (let i = 0; ; i++) {
    if ((await cacheKeys(page)).join(',') === 'notes-cache-v2') break;
    if (i > 100) throw new Error('old cache was not removed after SW upgrade');
    await page.waitForTimeout(100);
  }
  await page.waitForFunction(() => !document.getElementById('banner').hidden);
  const after = { cache_keys: await cacheKeys(page), sw_message: await versionViaMessage(), ...(await snap(page)) };
  await page.click('#nav-about');
  await page.waitForFunction(() => (document.getElementById('cache-info') || { innerText: '' }).innerText.includes('v2'));
  srv.overrides.delete('/sw.js');
  const res = { id: 'sw_cache_upgrade', feature: 'webapp.sw_cache_upgrade', before, after, about_cache_info: await page.innerText('#cache-info') };
  await ctx.close();
  return res;
}

async function runAll(siteDir, electronAsar, screenDir, screenPrefix, quick) {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const result = { browser: { name: 'chromium', version: browser.version() }, viewport: { width: 1280, height: 800 } };
  try {
    const srv = await startServer(siteDir);
    srv.root = siteDir;
    const urlFor = (hash, search = '') => `${srv.base}/${search}${hash}`;
    result.original = { site: 'original', scenarios: await commonScenarios(browser, urlFor, true) };
    if (!quick) {
      result.original.scenarios.push(...(await offlineScenarios(browser, srv)));
      result.original.scenarios.push(await upgradeScenario(browser, srv));
      result.original.screenshots = await screenshots(browser, urlFor, true, screenDir, screenPrefix);
    }
    await srv.close();
    if (!quick && electronAsar) {
      const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'asar_'));
      asar.extractAll(electronAsar, tmp);
      const fileUrl = (hash, search = '') => `${pathToFileURL(path.join(tmp, 'index.html')).href}${search}${hash}`;
      result.electron_asar = {
        site: 'original-electron/resources/app.asar (extracted, loaded via file:// like Electron loadFile)',
        asar_entries: asar.listPackage(electronAsar).sort(),
        package_json: JSON.parse(fs.readFileSync(path.join(tmp, 'package.json'), 'utf8')),
        scenarios: await commonScenarios(browser, fileUrl, false),
        screenshots: await screenshots(browser, fileUrl, false, screenDir, `${screenPrefix}electron_`),
      };
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  } finally {
    await browser.close();
  }
  return result;
}

function stripShots(r) {
  const c = JSON.parse(JSON.stringify(r));
  for (const k of ['original', 'electron_asar']) if (c[k]) delete c[k].screenshots;
  return c;
}

function diff(a, b, p = '') {
  if (JSON.stringify(a) === JSON.stringify(b)) return [];
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap((k) => diff(a[k], b[k], `${p}/${k}`));
  }
  return [`${p}: expected ${JSON.stringify(a)} got ${JSON.stringify(b)}`];
}

const summary = (r) => Object.fromEntries(['original', 'electron_asar'].filter((k) => r[k]).map((k) => [k, { scenarios: r[k].scenarios.length, screenshots: (r[k].screenshots || []).length }]));

if (!CHECK) {
  const asarPath = path.join(FX, 'original-electron', 'resources', 'app.asar');
  fs.rmSync(SCREENS, { recursive: true, force: true });
  const run1 = await runAll(path.join(FX, 'original'), asarPath, SCREENS, '', false);
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run2_'));
  const run2 = await runAll(path.join(FX, 'original'), asarPath, scratch, '', false);
  fs.rmSync(scratch, { recursive: true, force: true });
  const d = diff(stripShots(run1), stripShots(run2));
  if (d.length) { console.error('NON-DETERMINISTIC scenario output between two runs:\n' + d.join('\n')); process.exit(1); }
  const shotDiffs = [];
  for (const k of ['original', 'electron_asar']) run1[k].screenshots.forEach((s, i) => { if (s.sha256 !== run2[k].screenshots[i].sha256) shotDiffs.push(s.file); });
  run1.screenshot_determinism = { identical_across_two_runs: shotDiffs.length === 0, differing: shotDiffs };
  run1.notes = [
    'Recorded by `npm test` in fixtures/webapp/harness against the pre-installed Chromium via Playwright 1.56.1; every value was observed, none typed by hand.',
    'Screenshots depend on the host font stack and Chromium build: compare with a declared tolerance, never by hash across machines.',
    'The electron_asar target is loaded via file:// in plain Chromium (no Electron binary here), so service-worker features are unavailable there by design.',
  ];
  fs.mkdirSync(EXPECTED_DIR, { recursive: true });
  fs.writeFileSync(path.join(EXPECTED_DIR, 'web_scenarios.json'), JSON.stringify(run1, null, 2) + '\n');
  console.log('recorded', JSON.stringify(summary(run1)), 'screenshots deterministic:', run1.screenshot_determinism.identical_across_two_runs);
} else {
  const expected = JSON.parse(fs.readFileSync(path.join(EXPECTED_DIR, 'web_scenarios.json'), 'utf8'));
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'chk_'));
  const got = await runAll(siteArg, null, scratch, '', false);
  const d = diff(stripShots(expected).original, stripShots(got).original);
  const shots = (got.original.screenshots || []).map((s, i) => ({ name: s.name, size_ok: s.width === 1280 && s.height === 800, exact_hash_match: s.sha256 === expected.original.screenshots[i].sha256 }));
  fs.rmSync(scratch, { recursive: true, force: true });
  console.log(JSON.stringify({ site: siteArg, scenario_diffs: d.length, screenshots: shots }, null, 2));
  d.slice(0, 40).forEach((x) => console.log('DIFF', x));
  process.exit(d.length ? 1 : 0);
}
