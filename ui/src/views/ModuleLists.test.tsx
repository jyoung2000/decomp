import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';

const appCss = readFileSync(resolve(__dirname, '../styles/app.css'), 'utf8');

// R6 piece 4: the Strings, Imports, Exports and Sections views beside the Functions list — search, paging with totals, keyboard,
// the "Used by" pane whose functions open in Functions (URL + pane), plain-text rendering of program text, errors and the narrow
// layout. The fake controller applies the same filter / page rules as the shared helpers behind the routes.
const exe: Module = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'a', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const MAIN = '0x140001190';
const PARSE = '0x140001d63';
const FUNCS = [
  { addr: '0x140001000', name: 'entry0', size: 40, nbbs: 3 },
  { addr: MAIN, name: 'main', size: 900, nbbs: 40 },
  { addr: PARSE, name: 'fcn.140001d63', size: 300, nbbs: 12 },
];
const fnList = { functions: FUNCS, total: 3, offset: 0, returned: 3, more: false, session_id: 'res_' + '0'.repeat(22), sort: 'addr', contains: '' };
const detail = (r: (typeof FUNCS)[number]) => ({ addr: r.addr, header: r, blocks: [], variables: [], decompilers: [{ id: 'rizin', available: true, real_decompiler: true, label: 'rizin' }], session_id: fnList.session_id, untrusted: true });

const MARKUP = '<b>bold</b><script>alert(1)</script><img src=x onerror=alert(2)>';
const LONG = 'L'.repeat(400) + 'END';
type Str = { addr: string; length: number; section: string; type: string; string: string };
const STRINGS: Str[] = [
  { addr: '0x14000a000', length: MARKUP.length, section: '.rdata', type: 'ascii', string: MARKUP },
  { addr: '0x14000a100', length: LONG.length, section: '.rdata', type: 'ascii', string: LONG },
  { addr: '0x14000a378', length: 26, section: '.rdata', type: 'ascii', string: 'error: key not found: %s\n' },
  { addr: '0x14000b000', length: 12, section: '.data', type: 'utf16le', string: 'Hello world!' },
  ...Array.from({ length: 126 }, (_, i) => ({ addr: '0x' + (0x14000c000 + i * 16).toString(16), length: 8, section: '.rdata', type: 'ascii', string: `item-${String(i).padStart(3, '0')}` })),
];
const IMPORTS = [
  { name: 'CreateFileW', libname: 'KERNEL32.dll', type: 'FUNC', addr: '0x140010000' },
  { name: 'EnterCriticalSection', libname: 'KERNEL32.dll', type: 'FUNC', addr: '0x140010008' },
  { name: 'printf', libname: 'msvcrt.dll', type: 'FUNC', addr: '0x140010100' },
];
const EXPORTS = [{ name: 'Open', realname: 'ItemStore_Open', type: 'FUNC', addr: MAIN }, { name: 'Version', type: 'OBJ', addr: '0x140009000' }];
const SECTIONS = {
  sections: [
    { name: '.text', vaddr: '0x140001000', paddr: '0x400', size: 31232, vsize: 32768, perm: '-r-x', entropy: 6.37, high_entropy: false },
    { name: '.rdata', vaddr: '0x14000a000', paddr: '0x8000', size: 4608, vsize: 4512, perm: '-r--', entropy: 4.9, high_entropy: false },
    { name: '.packed', vaddr: '0x140020000', paddr: '0x9000', size: 65536, vsize: 65536, perm: '-rwx', entropy: 7.93, high_entropy: true },
    { name: '.bss', vaddr: '0x140030000', paddr: '0x0', size: 0, vsize: 4096, perm: '-rw-' },
  ],
  segments: [],
  entrypoints: [{ addr: '0x140001000', type: 'program', vaddr: 0x140001000 }],
  entropy_source: 'packer report',
  untrusted: true,
};
const USED: Record<string, object> = {
  '0x14000a378': {
    addr: '0x14000a378',
    refs: [
      { from: '0x140002078', to: '0x14000a378', type: 'DATA', kind: 'data', fcn_addr: PARSE, fcn_name: 'fcn.140001d63' },
      { from: '0x140002143', to: '0x14000a378', type: 'DATA', kind: 'data', fcn_addr: PARSE, fcn_name: 'fcn.140001d63' },
      { from: '0x140009000', to: '0x14000a378', type: 'DATA', kind: 'data', fcn_addr: null, fcn_name: null },
    ],
    total: 3,
    more: false,
    limit: 200,
    functions: [{ addr: PARSE, name: 'fcn.140001d63' }],
    untrusted: true,
  },
  '0x140010008': {
    addr: '0x140010008',
    refs: [{ from: '0x1400011a0', to: '0x140010008', type: 'CALL', kind: 'call', fcn_addr: MAIN, fcn_name: 'main' }],
    total: 1,
    more: false,
    limit: 200,
    functions: [{ addr: MAIN, name: 'main' }],
    untrusted: true,
  },
};
const errorResponse = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

function lastQuery(calls: { path: string }[], tail: string) {
  const hit = calls.filter((c) => c.path.split('?')[0].endsWith(tail));
  return new URL(hit[hit.length - 1].path, 'http://t').searchParams;
}
const page = <T,>(key: string, rows: T[], q: URLSearchParams) => {
  const offset = Number(q.get('offset') ?? 0);
  const limit = Number(q.get('limit') ?? 100);
  const p = rows.slice(offset, offset + limit);
  return { [key]: p, total: rows.length, offset, returned: p.length, more: offset + limit < rows.length, untrusted: true };
};

function routes(box: { calls: { path: string }[] }): Routes {
  const out: Routes = {
    '/cases/c1/modules': [exe],
    '/cases/c1/modules/mod1/functions': fnList,
    '/cases/c1/modules/mod1/strings': () => {
      const q = lastQuery(box.calls, '/strings');
      const contains = (q.get('contains') ?? '').toLowerCase();
      const rx = q.get('regex');
      const min = Number(q.get('min_length') ?? 4);
      const section = q.get('section');
      if (rx === '(a+)+') return errorResponse(400, 'invalid_regex', 'The pattern cannot be used: nested quantifiers and backreferences are not allowed in string regexes.', 'Simplify the pattern.');
      const rows = STRINGS.filter((s) => s.length >= min && (!section || s.section === section) && (!contains || s.string.toLowerCase().includes(contains)) && (!rx || new RegExp(rx).test(s.string)));
      return { ...page('strings', rows, q), scanned: STRINGS.length, count_truncated: false, sections: [{ section: '.data', count: 1 }, { section: '.rdata', count: 129 }] };
    },
    '/cases/c1/modules/mod1/imports': () => {
      const q = lastQuery(box.calls, '/imports');
      const c = (q.get('contains') ?? '').toLowerCase();
      return page('imports', IMPORTS.filter((i) => !c || `${i.name} ${i.libname}`.toLowerCase().includes(c)), q);
    },
    '/cases/c1/modules/mod1/exports': () => page('exports', EXPORTS, lastQuery(box.calls, '/exports')),
    '/cases/c1/modules/mod1/sections': SECTIONS,
  };
  for (const [a, u] of Object.entries(USED)) out[`/cases/c1/modules/mod1/used-by/${a}`] = u;
  for (const f of FUNCS) {
    const b = `/cases/c1/modules/mod1/functions/${f.addr}`;
    out[b] = detail(f);
    out[`${b}/decompiled`] = { function: f.name, addr: f.addr, decompiler: 'rz-ghidra(pdg)', is_real_decompiler: true, engine: 'rizin', requested: 'rizin', decompiled: { text: 'int f(void);' }, untrusted: true };
  }
  return out;
}

beforeEach(() => localStorage.clear());

async function open(hash = '#/projects/c1/functions?list=strings', over: Routes = {}) {
  window.location.hash = hash;
  const box: { calls: { path: string }[] } = { calls: [] };
  const m = makeStore(caseRoutes({ ...routes(box), ...over }));
  box.calls = m.calls;
  render(<App store={m.store} />);
  const tab = await screen.findByTestId('functions-tab');
  return { ...m, tab };
}
const listCalls = (calls: { path: string }[], tail: string) => calls.filter((c) => c.path.split('?')[0].endsWith(tail)).map((c) => c.path);

describe('Module lists (R6 piece 4)', () => {
  it('adds Functions | Strings | Imports | Exports | Sections tabs that keep the module and the URL in step', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions');
    const tabs = within(tab).getByRole('tablist', { name: 'Views of pecli.exe' });
    expect(within(tabs).getAllByRole('tab').map((t) => t.textContent)).toEqual(['Functions', 'Strings', 'Imports', 'Exports', 'Sections']);
    expect(within(tabs).getByRole('tab', { name: 'Functions' })).toHaveAttribute('aria-selected', 'true');
    await user.click(within(tabs).getByRole('tab', { name: 'Imports' }));
    expect(await within(tab).findByTestId('imp-table')).toBeInTheDocument();
    expect(window.location.hash).toContain('list=imports');
    expect(within(tab).getByRole('heading', { level: 2 })).toHaveTextContent('Imports');
    // arrow keys move between the views (roving focus)
    within(tabs).getByRole('tab', { name: 'Imports' }).focus();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(window.location.hash).toContain('list=exports'));
    expect(within(tabs).getByRole('tab', { name: 'Exports' })).toHaveFocus();
    await user.keyboard('{Home}');
    await waitFor(() => expect(window.location.hash).not.toContain('list='));
    expect(await within(tab).findByTestId('fn-table')).toBeInTheDocument();
    store.stop();
  });

  it('searches strings (debounced), pages with totals, and sends the regex, minimum length and section filters', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open();
    expect(await within(tab).findByTestId('str-summary')).toHaveTextContent('Showing 1–100 of 130 strings.');
    expect(within(tab).getByTestId('str-page')).toHaveTextContent('Page 1 of 2');
    expect(within(tab).getByTestId('str-prev')).toBeDisabled();
    await user.click(within(tab).getByTestId('str-next'));
    await waitFor(() => expect(within(tab).getByTestId('str-summary')).toHaveTextContent('Showing 101–130 of 130 strings.'));
    expect(within(tab).getByTestId('str-next')).toBeDisabled();
    expect(within(tab).getByTestId('str-next')).toHaveAttribute('data-tooltip', 'Already on the last page');

    await user.type(within(tab).getByTestId('str-search'), 'ERROR');
    await waitFor(() => expect(within(tab).getByTestId('str-summary')).toHaveTextContent('Showing 1–2 of 2 strings matching “ERROR”.')); // “error: key…” and the markup's onerror
    expect(lastQuery(calls, '/strings').get('contains')).toBe('ERROR');
    expect(lastQuery(calls, '/strings').get('offset')).toBe('0');
    expect(listCalls(calls, '/strings').filter((p) => p.includes('contains=E&')).length).toBe(0); // debounced, not per key

    await user.click(within(tab).getByTestId('str-regex'));
    await user.clear(within(tab).getByTestId('str-search'));
    await user.type(within(tab).getByTestId('str-search'), '^item-01\\d');
    await waitFor(() => expect(within(tab).getByTestId('str-summary')).toHaveTextContent('of 10 strings'));
    expect(lastQuery(calls, '/strings').get('regex')).toBe('^item-01\\d');
    expect(lastQuery(calls, '/strings').get('contains')).toBeNull();

    await user.click(within(tab).getByTestId('str-regex'));
    await user.clear(within(tab).getByTestId('str-search'));
    await user.selectOptions(within(tab).getByTestId('str-section'), '.data');
    await waitFor(() => expect(lastQuery(calls, '/strings').get('section')).toBe('.data'));
    await user.selectOptions(within(tab).getByTestId('str-min'), '20');
    await waitFor(() => expect(within(tab).getByText('No strings at this length')).toBeInTheDocument());
    expect(lastQuery(calls, '/strings').get('min_length')).toBe('20');
    store.stop();
  });

  it('renders program text as plain text: markup stays text, long strings are cut with Show all, control characters are visible', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    const markup = await within(tab).findByTestId('str-text-0x14000a000');
    expect(markup).toHaveTextContent(MARKUP);
    expect(within(tab).getByTestId('str-table').querySelector('b, script, img')).toBeNull();
    const long = within(tab).getByTestId('str-text-0x14000a100');
    expect(long.textContent).not.toContain('END');
    const more = within(long).getByRole('button', { name: 'Show all 403 characters' });
    expect(more).toHaveAttribute('aria-expanded', 'false');
    await user.click(more);
    expect(long).toHaveTextContent('END');
    expect(within(long).getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true');
    expect(window.location.hash).not.toContain('item='); // the toggle does not select the row
    expect(within(tab).getByTestId('str-text-0x14000a378')).toHaveTextContent('error: key not found: %s\\n');
    expect(within(tab).getByTestId('str-row-0x14000b000')).toHaveTextContent('UTF-16 LE');

    // the Used by heading shows the same plain text
    await user.click(within(tab).getByTestId('str-row-0x14000a000'));
    const pane = await within(tab).findByTestId('ub-pane');
    expect(within(pane).getByTestId('ub-item')).toHaveTextContent(MARKUP);
    expect(pane.querySelector('b, script, img')).toBeNull();
    store.stop();
  });

  it('shows Used by for a string and opens a containing function in Functions (URL, table, pane, focus)', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open();
    await user.click(await within(tab).findByTestId('str-row-0x14000a378'));
    expect(window.location.hash).toContain('item=0x14000a378');
    const pane = await within(tab).findByTestId('ub-pane');
    expect(await within(pane).findByTestId('ub-summary')).toHaveTextContent('3 references from 1 function');
    expect(within(pane).getByTestId('ub-item')).toHaveTextContent('0x14000a378 · ASCII · section .rdata · 26 characters');
    expect(within(pane).getByTestId('ub-ref-0')).toHaveTextContent('0x140002078datafcn.140001d63');
    expect(within(pane).getByTestId('ub-ref-2')).toHaveTextContent('0x140009000datanot inside an analysed function');
    expect(calls.some((c) => c.path === '/cases/c1/modules/mod1/used-by/0x14000a378?limit=200')).toBe(true);

    const link = within(pane).getByTestId('ub-fn-0');
    expect(link.getAttribute('href')).toContain(`fn=${PARSE}`);
    expect(link.getAttribute('href')).not.toContain('list=');
    await user.click(link);
    const fnPane = await within(tab).findByTestId('fn-pane');
    expect(window.location.hash).toContain(`fn=${PARSE}`);
    expect(window.location.hash).not.toContain('list=');
    expect(window.location.hash).not.toContain('item=');
    expect(within(tab).getByRole('tab', { name: 'Functions' })).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(within(tab).getByTestId(`fn-row-${PARSE}`)).toHaveAttribute('aria-selected', 'true'));
    await waitFor(() => expect(within(fnPane).getByTestId('fn-pane-name')).toHaveFocus());
    await waitFor(() => expect(within(fnPane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140001d63'));
    store.stop();
  });

  it('works by keyboard: arrows and Enter select an import, Escape closes Used by and returns to the row', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions?list=imports');
    const first = await within(tab).findByTestId('imp-row-0x140010000');
    expect(first).toHaveAttribute('tabindex', '0');
    first.focus();
    await user.keyboard('{ArrowDown}');
    const second = within(tab).getByTestId('imp-row-0x140010008');
    expect(second).toHaveFocus();
    await user.keyboard('{End}');
    expect(within(tab).getByTestId('imp-row-0x140010100')).toHaveFocus();
    await user.keyboard('{Home}{ArrowDown}{Enter}');
    expect(window.location.hash).toContain('item=0x140010008');
    expect(second).toHaveAttribute('aria-selected', 'true');
    expect(within(tab).getByTestId('imp-announce')).toHaveTextContent('Selected KERNEL32.dll!EnterCriticalSection');
    const pane = await within(tab).findByTestId('ub-pane');
    expect(within(pane).getByTestId('ub-item')).toHaveTextContent('KERNEL32.dll!EnterCriticalSection');
    expect(await within(pane).findByTestId('ub-ref-0')).toHaveTextContent('0x1400011a0callmain');
    within(pane).getByTestId('ub-fn-0').focus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(within(tab).queryByTestId('ub-pane')).toBeNull());
    expect(window.location.hash).not.toContain('item=');
    await waitFor(() => expect(within(tab).getByTestId('imp-row-0x140010008')).toHaveFocus());

    // search + clear
    await user.type(within(tab).getByTestId('imp-search'), 'msvcrt');
    await waitFor(() => expect(within(tab).getByTestId('imp-summary')).toHaveTextContent('Showing 1–1 of 1 imports matching “msvcrt”.'));
    await user.clear(within(tab).getByTestId('imp-search'));
    await user.type(within(tab).getByTestId('imp-search'), 'nothing-like-this');
    await user.click(await within(tab).findByTestId('imp-clear'));
    await waitFor(() => expect(within(tab).getByTestId('imp-summary')).toHaveTextContent('of 3 imports'));
    store.stop();
  });

  it('asks before analysing for Used by, and explains errors with a next action (engine missing, bad pattern)', async () => {
    const user = userEvent.setup();
    let analysed = false;
    const holder: { calls: { path: string }[] } = { calls: [] };
    const usedBy = () => {
      // the first request can arrive before open() returns the call log: it is the one without analyze=true
      if (holder.calls.length && lastQuery(holder.calls, '/used-by/0x14000a378').get('analyze') === 'true') analysed = true;
      return analysed ? USED['0x14000a378'] : errorResponse(409, 'not_analyzed', 'pecli.exe has not been analysed yet.', "Choose 'Analyse now' (about a minute for a small program).");
    };
    const { store, tab, calls } = await open('#/projects/c1/functions?list=strings&item=0x14000a378', { '/cases/c1/modules/mod1/used-by/0x14000a378': usedBy });
    holder.calls = calls;
    // deep link: the pane opens from the URL; the analysis is offered, not started
    const pane = await within(tab).findByTestId('ub-pane');
    // (the module list does not need the analysis)
    expect(await within(tab).findByTestId('str-table')).toBeInTheDocument();
    const callout = await within(pane).findByTestId('ub-not-analyzed');
    expect(callout).toHaveTextContent('Analyse now');
    expect(calls.filter((c) => c.path.includes('analyze=true')).length).toBe(0);
    await user.click(within(callout).getByTestId('ub-analyze'));
    expect(await within(pane).findByTestId('ub-summary')).toHaveTextContent('3 references');
    expect(calls.some((c) => c.path === '/cases/c1/modules/mod1/used-by/0x14000a378?limit=200&analyze=true')).toBe(true);
    store.stop();
  });

  it('shows the engine-missing and invalid-pattern errors with what to do next', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions?list=imports', {
      '/cases/c1/modules/mod1/imports': errorResponse(503, 'engine_missing', 'The analysis engine (rizin) is not installed, so imports cannot be read.', 'Install rizin on the Tools page, then come back to this tab.'),
    });
    const alert = await within(tab).findByRole('alert');
    expect(alert).toHaveTextContent('The imports could not be listed');
    expect(alert).toHaveTextContent('Install rizin on the Tools page');
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(within(tab).getByTestId('imp-open-tools')).toHaveAttribute('href', '#/tools');

    await user.click(within(tab).getByRole('tab', { name: 'Strings' }));
    await within(tab).findByTestId('str-table');
    await user.click(within(tab).getByTestId('str-regex'));
    await user.type(within(tab).getByTestId('str-search'), '(a+)+');
    const bad = await within(tab).findByTestId('str-regex-error');
    expect(bad).toHaveTextContent('nested quantifiers');
    expect(bad).toHaveTextContent('Simplify the pattern.');
    await user.click(within(bad).getByTestId('str-regex-off'));
    await waitFor(() => expect(within(tab).getByTestId('str-summary')).toHaveTextContent('No strings match “(a+)+”.'));
    expect(within(tab).getByTestId('str-regex')).not.toBeChecked();
    store.stop();
  });

  it('lists exports as links into Functions, and sections with entropy, a high-entropy warning and the entry point', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions?list=exports');
    const link = await within(tab).findByTestId(`exp-fn-${MAIN}`);
    expect(link).toHaveTextContent('Open');
    expect(within(tab).getByTestId('exp-row-0x140009000')).toHaveTextContent('Version0x140009000OBJ');
    expect(within(within(tab).getByTestId('exp-row-0x140009000')).queryByRole('link')).toBeNull();
    await user.click(link);
    expect(await within(tab).findByTestId('fn-pane')).toBeInTheDocument();
    expect(window.location.hash).toContain(`fn=${MAIN}`);

    await user.click(within(tab).getByRole('tab', { name: 'Sections' }));
    const table = await within(tab).findByTestId('sec-table');
    expect(within(table).getByTestId('sec-row-.text@0x140001000')).toHaveTextContent('.text0x14000100032,76831,232read, execute6.37');
    expect(within(table).getByTestId('sec-entropy-.packed')).toHaveTextContent('7.93 high');
    expect(within(table).getByTestId('sec-row-.bss@0x140030000')).toHaveTextContent('—');
    expect(within(tab).getByTestId('sec-high')).toHaveTextContent('.packed looks compressed or encrypted (entropy 7.93 of 8)');
    expect(within(tab).getByTestId('sec-summary')).toHaveTextContent('Showing 1–4 of 4 sections.');
    fireEvent.change(within(tab).getByTestId('sec-search'), { target: { value: 'text' } });
    await waitFor(() => expect(within(tab).getByTestId('sec-summary')).toHaveTextContent('Showing 1–1 of 1 sections matching “text”.'));
    await user.click(within(tab).getByTestId('sec-entry-0x140001000'));
    expect(await within(tab).findByTestId('fn-pane')).toBeInTheDocument();
    expect(window.location.hash).toContain('fn=0x140001000');
    store.stop();
  });

  it('stacks Used by under the list and folds secondary columns at narrow widths', async () => {
    const { store, tab } = await open('#/projects/c1/functions?list=strings&item=0x14000a378');
    expect(await within(tab).findByTestId('str-layout')).toHaveClass('fn-layout', 'with-pane');
    // a deep link names the string once its row is on screen
    await waitFor(() => expect(within(tab).getByTestId('ub-item')).toHaveTextContent('error: key not found: %s\\n0x14000a378 · ASCII'));
    // jsdom has no layout engine: check the stylesheet rules (one column under 1100px; optional columns fold under 820px)
    expect(appCss).toMatch(/@media \(max-width: 1100px\) \{\s*\.fn-layout\.with-pane \{ grid-template-columns: minmax\(0, 1fr\); \}/);
    expect(appCss).toMatch(/@media \(max-width: 820px\) \{[^}]*\}[\s\S]*?\.ml-table \.col-opt \{ display: none; \}\s*\.ml-table \.ml-inline \{ display: block; \}/);
    const row = await within(tab).findByTestId('str-row-0x14000b000');
    expect(row.querySelector('.ml-inline')).toHaveTextContent('UTF-16 LE · .data · 12 chars');
    expect(row.querySelectorAll('td.col-opt').length).toBe(3);
    store.stop();
  });
});
