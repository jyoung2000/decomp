import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { App } from '../App';
import { checkBytePattern, checkTextPattern, escapeRegex } from '../lib/re';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';

const appCss = readFileSync(resolve(__dirname, '../styles/app.css'), 'utf8');

// R6 piece 6: the Search view of the Functions area — hex / text toggle with inline validation (the controller's words), results
// with links into Functions (piece-3 navigation: URL, pane, history), paging, keyboard, loading / empty / error states, plain-text
// rendering of program bytes and strings, the search kept in the URL, and the narrow layout. The fake controller pages like
// re_workbench.page_search.
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

type Match = Record<string, unknown>;
const hexHit = (i: number): Match => {
  const addr = 0x140001190 + i * 0x40;
  return { addr: '0x' + addr.toString(16), paddr: '0x' + (addr - 0x140000c00).toString(16), section: '.text', before: '90 90 90', bytes: '48 8b 4c 24', after: '08 e8 00', fcn_addr: i % 2 ? PARSE : MAIN, fcn_name: i % 2 ? 'fcn.140001d63' : 'main' };
};
const HEX_HITS: Match[] = [
  { addr: null, paddr: '0x0', section: null, before: '', bytes: '48 8b 4c 24', after: '00 00', fcn_addr: null, fcn_name: null },
  ...Array.from({ length: 129 }, (_, i) => hexHit(i)),
];
const MARKUP = '<img src=x onerror=alert(1)>usage';
const TEXT_HITS: Match[] = [
  { addr: '0x14000a000', paddr: '0x9400', section: '.rdata', string: 'usage: pecli <file>\n', encoding: 'ascii', length: 20, before: '00 00', bytes: '75 73 61 67 65 3a', after: '', bytes_clipped: 20, fcn_addr: null, fcn_name: null },
  { addr: '0x14000a100', paddr: '0x9500', section: '.rdata', string: MARKUP, encoding: 'ascii', length: MARKUP.length, before: '', bytes: '3c 69 6d 67', after: '', fcn_addr: null, fcn_name: null },
];
const errorResponse = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

function lastQuery(calls: { path: string }[], tail: string) {
  const hit = calls.filter((c) => c.path.split('?')[0].endsWith(tail));
  return new URL(hit[hit.length - 1].path, 'http://t').searchParams;
}

function routes(box: { calls: { path: string }[]; analysed: boolean; fail?: Response }): Routes {
  const out: Routes = {
    '/cases/c1/modules': [exe],
    '/cases/c1/modules/mod1/functions': fnList,
    '/cases/c1/modules/mod1/pattern-search': () => {
      if (box.fail) return box.fail;
      const q = lastQuery(box.calls, '/pattern-search');
      if (q.get('analyze') === 'true') box.analysed = true;
      const kind = q.get('kind');
      const pattern = q.get('pattern') ?? '';
      if (kind === 'hex' && pattern === 'cc cc')   // a refusal only the controller knows about
        return errorResponse(400, 'invalid_pattern', 'The byte pattern cannot be used: the controller refused it.', 'Write pairs of hex digits, with ?? for any byte, e.g. 48 8b ?? 24');
      let rows = kind === 'hex' ? (pattern.replace(/\s/g, '') === '488b4c24' || pattern.replace(/\s/g, '') === '488b??24' ? HEX_HITS : []) : TEXT_HITS.filter((m) => new RegExp(pattern).test(String(m.string)));
      if (!box.analysed) rows = rows.map((m) => ({ ...m, fcn_addr: null, fcn_name: null }));
      const offset = Number(q.get('offset') ?? 0);
      const limit = Number(q.get('limit') ?? 100);
      const p = rows.slice(offset, offset + limit);
      return { matches: p, listed: rows.length, offset, returned: p.length, more: offset + limit < rows.length, found: rows.length, found_exact: true, capped: false, max_hits: 1000, kind, pattern: kind === 'hex' ? '48 8b 4c 24' : pattern, limit, functions_known: box.analysed, untrusted: true };
    },
  };
  for (const f of FUNCS) {
    const b = `/cases/c1/modules/mod1/functions/${f.addr}`;
    out[b] = detail(f);
    out[`${b}/decompiled`] = { function: f.name, addr: f.addr, decompiler: 'rz-ghidra(pdg)', is_real_decompiler: true, engine: 'rizin', requested: 'rizin', decompiled: { text: 'int f(void);' }, untrusted: true };
  }
  return out;
}

beforeEach(() => localStorage.clear());

async function open(hash = '#/projects/c1/functions?list=search', analysed = true) {
  window.location.hash = hash;
  const box: { calls: { path: string }[]; analysed: boolean; fail?: Response } = { calls: [], analysed };
  const m = makeStore(caseRoutes(routes(box)));
  box.calls = m.calls;
  render(<App store={m.store} />);
  const tab = await screen.findByTestId('functions-tab');
  return { ...m, tab, box };
}
const searchCalls = (calls: { path: string }[]) => calls.filter((c) => c.path.split('?')[0].endsWith('/pattern-search'));

describe('pattern checks (mirror re_workbench.check_byte_pattern / compile_regex)', () => {
  it('explains a bad byte or text pattern in the controller’s words', () => {
    expect(checkBytePattern('48 8B ??24')).toBeNull();
    expect(checkBytePattern('')).toMatch(/Type the bytes/);
    expect(checkBytePattern('0x48')).toMatch(/0x prefixes/);
    expect(checkBytePattern('48 zz')).toMatch(/'z' is not a hex digit/);
    expect(checkBytePattern('48 8')).toMatch(/odd number of characters \(3\)/);
    expect(checkBytePattern('4? 00')).toMatch(/'4\?' mixes/);
    expect(checkBytePattern('?? ??')).toMatch(/only wildcards/);
    expect(checkBytePattern('00'.repeat(257))).toMatch(/at most 256 bytes/);
    expect(checkTextPattern('(a+)+', false)).toMatch(/Nested quantifiers/);
    expect(checkTextPattern('(', false)).toMatch(/not a valid regular expression/);
    expect(checkTextPattern('(', true)).toBeNull(); // plain text is escaped
    expect(escapeRegex('a.b(c)*')).toBe('a\\.b\\(c\\)\\*');
  });
});

describe('Search view (R6 piece 6)', () => {
  it('validates inline, searches on Enter, pages the hits and keeps the search in the URL', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open();
    expect(within(tab).getByRole('tab', { name: 'Search' })).toHaveAttribute('aria-selected', 'true');
    expect(within(tab).getByRole('heading', { level: 2 })).toHaveTextContent('Search');
    expect(await within(tab).findByText('Search this module')).toBeInTheDocument(); // empty state before any search
    const input = within(tab).getByTestId('ms-pattern');
    await user.type(input, '48 8');
    expect(within(tab).getByTestId('ms-error')).toHaveTextContent('odd number of characters (3)');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input.getAttribute('aria-describedby')).toContain('ms-error');
    await user.keyboard('{Enter}');
    expect(searchCalls(calls)).toHaveLength(0); // nothing is sent while the pattern is wrong
    expect(input).toHaveFocus();
    await user.clear(input);
    await user.type(input, '48 8b 4c 24{Enter}');
    expect(await within(tab).findByTestId('ms-summary')).toHaveTextContent('Showing 1–100 of 130 matches for “48 8b 4c 24”.');
    expect(input).toHaveAttribute('aria-invalid', 'false');
    expect(within(tab).queryByTestId('ms-error')).toBeNull();
    const q = lastQuery(calls, '/pattern-search');
    expect([q.get('kind'), q.get('pattern'), q.get('offset'), q.get('limit')]).toEqual(['hex', '48 8b 4c 24', '0', '100']);
    expect(window.location.hash).toContain('sq=48+8b+4c+24');
    expect(window.location.hash).not.toContain('sk=');
    // a hit outside every section: not mapped, no function; others link to their function
    expect(within(tab).getByTestId('ms-row-0')).toHaveTextContent('not mapped');
    expect(within(tab).getByTestId('ms-row-0')).toHaveTextContent('file offset 0x0');
    expect(within(tab).getByTestId('ms-nofn-0')).toHaveTextContent('not inside an analysed function');
    expect(within(tab).getByTestId('ms-fn-1')).toHaveTextContent('main');
    expect(within(tab).getByTestId('ms-bytes-1')).toHaveTextContent('90 90 90 48 8b 4c 24 08 e8 00');
    expect(within(tab).getByTestId('ms-bytes-1').querySelector('mark')).toHaveTextContent('48 8b 4c 24');
    // paging
    expect(within(tab).getByTestId('ms-page')).toHaveTextContent('Page 1 of 2');
    expect(within(tab).getByTestId('ms-prev')).toBeDisabled();
    expect(within(tab).getByTestId('ms-prev')).toHaveAttribute('data-tooltip', 'Already on the first page');
    await user.click(within(tab).getByTestId('ms-next'));
    await waitFor(() => expect(within(tab).getByTestId('ms-summary')).toHaveTextContent('Showing 101–130 of 130 matches'));
    expect(lastQuery(calls, '/pattern-search').get('offset')).toBe('100');
    expect(within(tab).getByTestId('ms-next')).toBeDisabled();
    await user.selectOptions(within(tab).getByTestId('ms-limit'), '200');
    await waitFor(() => expect(within(tab).getByTestId('ms-summary')).toHaveTextContent('Showing 1–130 of 130 matches'));
    // a new search starts on page 1
    await user.selectOptions(within(tab).getByTestId('ms-limit'), '50');
    await user.click(within(tab).getByTestId('ms-next'));
    await waitFor(() => expect(lastQuery(calls, '/pattern-search').get('offset')).toBe('50'));
    await user.clear(input);
    await user.type(input, '488b??24{Enter}');
    await waitFor(() => expect(lastQuery(calls, '/pattern-search').get('pattern')).toBe('488b??24'));
    expect(lastQuery(calls, '/pattern-search').get('offset')).toBe('0');
    store.stop();
  });

  it('follows a hit into Functions (URL, table, pane, focus) and comes back to the same search', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions?list=search&sq=48+8b+4c+24');
    const link = await within(tab).findByTestId('ms-fn-2');
    expect(link).toHaveTextContent('fcn.140001d63');
    expect(link.getAttribute('href')).toContain(`fn=${PARSE}`);
    expect(link.getAttribute('href')).toContain('sq=48');
    expect(link.getAttribute('href')).not.toContain('list=');
    await user.click(link);
    const pane = await within(tab).findByTestId('fn-pane');
    expect(window.location.hash).toContain(`fn=${PARSE}`);
    expect(within(tab).getByRole('tab', { name: 'Functions' })).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(within(tab).getByTestId(`fn-row-${PARSE}`)).toHaveAttribute('aria-selected', 'true'));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveFocus());
    // back to Search: the pattern and its hits are still there
    await user.click(within(tab).getByRole('tab', { name: 'Search' }));
    expect(await within(tab).findByTestId('ms-pattern')).toHaveValue('48 8b 4c 24');
    expect(await within(tab).findByTestId('ms-summary')).toHaveTextContent('of 130 matches');
    store.stop();
  });

  it('works by keyboard: arrows, Home/End, Enter opens the containing function', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open('#/projects/c1/functions?list=search&sq=48+8b+4c+24');
    const first = await within(tab).findByTestId('ms-row-0');
    expect(first).toHaveAttribute('tabindex', '0');
    first.focus();
    await user.keyboard('{Enter}');
    expect(within(tab).getByTestId('ms-announce')).toHaveTextContent('not inside an analysed function');
    await user.keyboard('{ArrowDown}');
    expect(within(tab).getByTestId('ms-row-1')).toHaveFocus();
    await user.keyboard('{End}');
    expect(within(tab).getByTestId('ms-row-99')).toHaveFocus();
    await user.keyboard('{Home}{PageDown}');
    expect(within(tab).getByTestId('ms-row-10')).toHaveFocus();
    await user.keyboard('{ArrowUp}{Enter}'); // row 9 = hit 8 → main
    await waitFor(() => expect(window.location.hash).toContain(`fn=${MAIN}`));
    expect(await within(tab).findByTestId('fn-pane')).toBeInTheDocument();
    store.stop();
  });

  it('searches text (plain by default, regex on request) and shows program text and bytes as plain text', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open();
    await user.click(await within(tab).findByTestId('ms-kind-text'));
    const input = within(tab).getByTestId('ms-pattern');
    await user.type(input, 'usage:{Enter}');
    expect(await within(tab).findByTestId('ms-summary')).toHaveTextContent('Showing 1–1 of 1 match');
    expect(lastQuery(calls, '/pattern-search').get('kind')).toBe('text');
    expect(lastQuery(calls, '/pattern-search').get('pattern')).toBe('usage:');
    expect(window.location.hash).toContain('sk=text');
    expect(within(tab).getByTestId('ms-str-0')).toHaveTextContent('usage: pecli <file>\\n');
    expect(within(tab).getByTestId('ms-bytes-0')).toHaveTextContent('… (20 bytes)');
    // plain text with regex characters is escaped; with Regular expression it is sent as typed
    await user.clear(input);
    await user.type(input, 'a.b(');
    expect(within(tab).queryByTestId('ms-error')).toBeNull();
    await user.click(within(tab).getByTestId('ms-regex'));
    expect(within(tab).getByTestId('ms-error')).toHaveTextContent('not a valid regular expression');
    await user.clear(input);
    await user.type(input, 'onerror|usage{Enter}');
    await waitFor(() => expect(within(tab).getByTestId('ms-summary')).toHaveTextContent('of 2 matches'));
    expect(lastQuery(calls, '/pattern-search').get('pattern')).toBe('onerror|usage');
    expect(window.location.hash).toContain('sre=1');
    expect(within(tab).getByTestId('ms-str-1')).toHaveTextContent(MARKUP);
    expect(within(tab).getByTestId('ms-table').querySelector('img, script')).toBeNull();
    store.stop();
  });

  it('offers Analyse now for containing functions, and explains no matches, a refused pattern and a missing engine', async () => {
    const user = userEvent.setup();
    const { store, tab, calls, box } = await open('#/projects/c1/functions?list=search&sq=48+8b+4c+24', false);
    const callout = await within(tab).findByTestId('ms-not-analyzed');
    expect(within(tab).getByTestId('ms-nofn-1')).toHaveTextContent('analyse to see');
    expect(calls.some((c) => c.path.includes('analyze=true'))).toBe(false); // offered, not started
    await user.click(within(callout).getByTestId('ms-analyze'));
    expect(await within(tab).findByTestId('ms-fn-1')).toHaveTextContent('main');
    expect(lastQuery(calls, '/pattern-search').get('analyze')).toBe('true');
    expect(within(tab).queryByTestId('ms-not-analyzed')).toBeNull();

    const input = within(tab).getByTestId('ms-pattern');
    await user.clear(input);
    await user.type(input, 'de ad be ef{Enter}');
    expect(await within(tab).findByText('No matches')).toBeInTheDocument();
    expect(within(tab).getByTestId('ms-summary')).toHaveTextContent('No match for the bytes');
    expect(within(tab).getByText(/little-endian/)).toBeInTheDocument();
    await user.click(within(tab).getByTestId('ms-edit'));
    expect(input).toHaveFocus();

    // the controller refuses (its words + example), shown at the field
    window.location.hash = '#/projects/c1/functions?list=search&sq=cc+cc';
    expect(await within(tab).findByTestId('ms-error')).toHaveTextContent('the controller refused it');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(within(tab).getByTestId('ms-error')).toHaveTextContent('e.g. 48 8b ?? 24');

    box.fail = errorResponse(503, 'engine_missing', 'The analysis engine (rizin) is not installed, so the module cannot be searched.', 'Install rizin on the Tools page.');
    window.location.hash = '#/projects/c1/functions?list=search&sq=48+8b+4c+24';
    expect(await within(tab).findByText('The search could not run')).toBeInTheDocument();
    expect(within(tab).getByTestId('ms-open-tools')).toHaveAttribute('href', '#/tools');
    box.fail = undefined;
    await user.click(within(tab).getByRole('button', { name: /Retry/ }));
    expect(await within(tab).findByTestId('ms-summary')).toHaveTextContent('of 130 matches');
    store.stop();
  });

  it('folds the section column into the address at narrow widths and wraps the pattern field', () => {
    const narrow = appCss.slice(appCss.lastIndexOf('@media (max-width: 820px)'));
    expect(narrow).toMatch(/\.filters \.field\.ms-field \{ flex-basis: 100%; max-width: none; \}/);
    expect(appCss).toMatch(/\.ml-table \.col-opt \{ display: none; \}/);
    expect(appCss).toMatch(/\.ml-table \.ml-inline \{ display: block; \}/);
    expect(appCss).toMatch(/\.ms-bytes \{ overflow-wrap: anywhere;/);
    // found by the real-controller e2e at 1366x800: in the height-capped pane the code-view tab strip shrank to nothing
    expect(appCss).toMatch(/\.fn-pane > \* \{ flex-shrink: 0; \}/);
  });
});
