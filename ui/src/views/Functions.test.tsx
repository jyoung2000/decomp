import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';

// R6 piece 1: the Functions tab (search, sort, paging, keyboard selection, error states). The fake controller below applies the
// same filter / sort / page rules as the shared helper behind GET /cases/{c}/modules/{m}/functions.
const exe: Module = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'a', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const dll: Module = { module_id: 'mod2', rel_path: 'core.dll', sha256: 'b', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const managed: Module = { module_id: 'mod3', rel_path: 'App.dll', sha256: 'c', size: 1, format: 'pe', profile: 'dotnet', arch: null };

type Row = { addr: string; name: string; size: number; span?: number; nbbs?: number; signature?: string; _off: number };
const BASE: Row[] = [
  { addr: '0x140001000', name: 'entry0', size: 40, nbbs: 3, signature: 'void entry0(void);', _off: 0x140001000 },
  { addr: '0x140001190', name: 'main', size: 900, nbbs: 40, signature: 'int main(int argc, char **argv);', _off: 0x140001190 },
  { addr: '0x140002b70', name: 'fcn.140002b70', size: 18, span: 23718, nbbs: 2, _off: 0x140002b70 },
];
const many = (n: number): Row[] => Array.from({ length: n }, (_, i) => ({ addr: '0x' + (0x140010000 + i * 16).toString(16), name: `fcn.${(0x140010000 + i * 16).toString(16)}`, size: 10 + i, nbbs: 1, _off: 0x140010000 + i * 16 }));

function fakeFunctions(rows: Row[], last: () => string, moduleId = 'mod1') {
  return () => {
    const q = new URL(last(), 'http://t').searchParams;
    const contains = (q.get('contains') ?? '').toLowerCase();
    const sort = q.get('sort') ?? 'addr';
    const offset = Number(q.get('offset') ?? 0);
    const limit = Number(q.get('limit') ?? 100);
    const key = { addr: (f: Row) => f._off, size: (f: Row) => -f.size, name: (f: Row) => f.name }[sort as 'addr']!;
    const all = rows.filter((f) => !contains || f.name.toLowerCase().includes(contains)).sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
    const page = all.slice(offset, offset + limit).map(({ _off: _o, ...f }) => f);
    return { functions: page, total: all.length, offset, returned: page.length, more: offset + limit < all.length, session_id: 'res_' + '0'.repeat(22), sort, contains: q.get('contains') ?? '', module_id: moduleId };
  };
}

const errorResponse = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => localStorage.clear());

async function open(over: Routes = {}, modules: Module[] = [exe]) {
  window.location.hash = '#/projects/c1/functions';
  const box: { calls: { method: string; path: string }[] } = { calls: [] };
  const last = () => {
    const fn = box.calls.filter((c) => c.path.includes('/functions?'));
    return fn[fn.length - 1].path;
  };
  const m = makeStore(caseRoutes({ '/cases/c1/modules': modules, '/cases/c1/modules/mod1/functions': fakeFunctions(BASE, last), ...over }));
  box.calls = m.calls;
  render(<App store={m.store} />);
  const tab = await screen.findByTestId('functions-tab');
  return { ...m, tab, last };
}
const fnCalls = (calls: { path: string }[]) => calls.filter((c) => c.path.includes('/functions?')).map((c) => c.path);

describe('Functions tab (R6)', () => {
  it('appears only for projects with native modules', async () => {
    const { store } = await open();
    expect(await screen.findByTestId('tab-functions')).toHaveTextContent('Functions');
    store.stop();
  });

  it('is not offered when the project has no native code', async () => {
    window.location.hash = '#/projects/c1/overview';
    const m = makeStore(caseRoutes({ '/cases/c1/modules': [managed] }));
    render(<App store={m.store} />);
    await screen.findByTestId('tab-overview');
    await waitFor(() => expect(m.calls.some((c) => c.path === '/cases/c1/modules')).toBe(true));
    expect(screen.queryByTestId('tab-functions')).not.toBeInTheDocument();
    m.store.stop();
  });

  it('lists address, name, code size (span only when it differs), blocks and signature', async () => {
    const { store, tab } = await open();
    const table = await within(tab).findByRole('grid', { name: 'Functions in pecli.exe' });
    expect(within(tab).getByTestId('fn-summary')).toHaveTextContent('Showing 1–3 of 3 functions.');
    expect(within(tab).getByTestId('fn-module-name')).toHaveTextContent('pecli.exe');
    const main = within(table).getByTestId('fn-row-0x140001190');
    expect(main).toHaveTextContent('0x140001190');
    expect(main).toHaveTextContent('main');
    expect(main).toHaveTextContent('900');
    expect(main).toHaveTextContent('40');
    expect(main).toHaveTextContent('int main(int argc, char **argv);');
    expect(within(main).queryByTestId('fn-span-0x140001190')).not.toBeInTheDocument();
    const spread = within(table).getByTestId('fn-row-0x140002b70');
    expect(within(spread).getByTestId('fn-span-0x140002b70')).toHaveTextContent('span 23,718');
    expect(within(spread).getByTitle('18 bytes of code spread over an address range of 23,718 bytes')).toBeInTheDocument();
    store.stop();
  });

  it('searches with a debounce, then sorts, going back to the first page', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open();
    await within(tab).findByTestId('fn-table');
    await user.type(within(tab).getByLabelText('Search by name'), 'MAI');
    await waitFor(() => expect(within(tab).getByTestId('fn-summary')).toHaveTextContent('Showing 1–1 of 1 function matching “MAI”.'));
    const searched = fnCalls(calls).filter((p) => p.includes('contains='));
    expect(searched).toEqual(['/cases/c1/modules/mod1/functions?contains=MAI&sort=addr&offset=0&limit=100']);  // one request, not one per key
    await user.clear(within(tab).getByLabelText('Search by name'));
    await user.selectOptions(within(tab).getByLabelText('Sort by'), 'size');
    await waitFor(() => expect(fnCalls(calls).at(-1)).toBe('/cases/c1/modules/mod1/functions?sort=size&offset=0&limit=100'));
    await waitFor(() => {
      const rows = within(within(tab).getByTestId('fn-table')).getAllByRole('row').slice(1);
      expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual(['fn-row-0x140001190', 'fn-row-0x140001000', 'fn-row-0x140002b70']);
    });
    store.stop();
  });

  it('shows an empty state with a way out when nothing matches', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    await within(tab).findByTestId('fn-table');
    await user.type(within(tab).getByLabelText('Search by name'), 'zzz');
    expect(await within(tab).findByText('No matches')).toBeInTheDocument();
    await user.click(within(tab).getByRole('button', { name: 'Clear the search' }));
    await within(tab).findByTestId('fn-table');
    store.stop();
  });

  it('pages through the list with the total count; Previous explains why it is unavailable', async () => {
    const user = userEvent.setup();
    let last = () => '';
    const { store, tab, calls } = await open({ '/cases/c1/modules/mod1/functions': (() => fakeFunctions(many(250), () => last())()) });
    last = () => fnCalls(calls).at(-1)!;
    expect(await within(tab).findByTestId('fn-summary')).toHaveTextContent('Showing 1–100 of 250 functions.');
    expect(within(tab).getByTestId('fn-page')).toHaveTextContent('Page 1 of 3');
    const prev = within(tab).getByTestId('fn-prev');
    expect(prev).toBeDisabled();
    expect(prev).toHaveAttribute('data-tooltip', 'Already on the first page');
    await user.click(within(tab).getByRole('button', { name: /Next/ }));
    await waitFor(() => expect(within(tab).getByTestId('fn-summary')).toHaveTextContent('Showing 101–200 of 250 functions.'));
    expect(fnCalls(calls).at(-1)).toContain('offset=100');
    await user.click(within(tab).getByRole('button', { name: /Next/ }));
    await waitFor(() => expect(within(tab).getByTestId('fn-page')).toHaveTextContent('Page 3 of 3'));
    expect(within(tab).getByTestId('fn-next')).toHaveAttribute('data-tooltip', 'Already on the last page');
    await user.selectOptions(within(tab).getByLabelText('Rows per page'), '50');
    await waitFor(() => expect(within(tab).getByTestId('fn-summary')).toHaveTextContent('Showing 1–50 of 250 functions.'));
    store.stop();
  });

  it('moves with the arrow keys, selects with Enter or Space and announces the selection', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    const table = await within(tab).findByTestId('fn-table');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveAttribute('tabindex', '0');
    expect(rows[1]).toHaveAttribute('tabindex', '-1');
    rows[0].focus();
    await user.keyboard('{ArrowDown}');
    expect(rows[1]).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(rows[1]).toHaveAttribute('aria-selected', 'true');
    expect(rows[0]).toHaveAttribute('aria-selected', 'false');
    expect(within(tab).getByTestId('fn-announce')).toHaveTextContent('Selected main at 0x140001190, 900 bytes.');
    await user.keyboard('{End}');
    expect(rows[2]).toHaveFocus();
    await user.keyboard(' ');
    expect(rows[2]).toHaveAttribute('aria-selected', 'true');
    expect(rows[1]).toHaveAttribute('aria-selected', 'false');
    await user.keyboard('{Home}{ArrowUp}');
    expect(rows[0]).toHaveFocus();
    await user.click(rows[1]);
    expect(rows[1]).toHaveAttribute('aria-selected', 'true');
    expect(within(tab).getByTestId('fn-announce')).toHaveTextContent('Selected main');
    store.stop();
  });

  it('offers "Analyse now" when the module has not been analysed, then lists the functions', async () => {
    const user = userEvent.setup();
    let last = () => '';
    const ok = fakeFunctions(BASE, () => last());
    const { store, tab, calls } = await open({
      '/cases/c1/modules/mod1/functions': () =>
        last().includes('analyze=true') ? ok() : errorResponse(409, 'not_analyzed', 'pecli.exe has not been analysed yet.', "Choose 'Analyse now', or start the project's analysis from Overview."),
    });
    last = () => fnCalls(calls).at(-1)!;
    const box = await within(tab).findByTestId('fn-not-analyzed');
    expect(box).toHaveTextContent('pecli.exe has not been analysed yet.');
    expect(box).toHaveTextContent("Next: Choose 'Analyse now'");
    await user.click(within(box).getByRole('button', { name: 'Analyse now' }));
    expect(await within(tab).findByTestId('fn-table')).toBeInTheDocument();
    expect(fnCalls(calls).at(-1)).toContain('analyze=true');
    store.stop();
  });

  it('explains a missing analysis engine with the next action and a link to Tools', async () => {
    const { store, tab } = await open({
      '/cases/c1/modules/mod1/functions': () => errorResponse(503, 'engine_missing', 'The analysis engine (rizin) is not installed, so functions cannot be listed.', 'Install rizin on the Tools page, then come back to this tab.'),
    });
    const alert = await within(tab).findByRole('alert');
    expect(alert).toHaveTextContent('What happened: The analysis engine (rizin) is not installed');
    expect(alert).toHaveTextContent('Next: Install rizin on the Tools page');
    expect(within(tab).getByTestId('fn-open-tools')).toHaveAttribute('href', '#/tools');
    store.stop();
  });

  it('has a module picker when there are several native modules', async () => {
    const user = userEvent.setup();
    let last = () => '';
    const { store, tab, calls } = await open({ '/cases/c1/modules/mod2/functions': () => fakeFunctions(many(5), () => last(), 'mod2')() }, [exe, dll, managed]);
    last = () => fnCalls(calls).at(-1)!;
    const picker = within(tab).getByLabelText('Module');
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual(['pecli.exe', 'core.dll']);
    await within(tab).findByTestId('fn-table');
    await user.selectOptions(picker, 'mod2');
    await waitFor(() => expect(within(tab).getByTestId('fn-summary')).toHaveTextContent('Showing 1–5 of 5 functions.'));
    expect(within(tab).getByRole('grid')).toHaveAccessibleName('Functions in core.dll');
    store.stop();
  });
});
