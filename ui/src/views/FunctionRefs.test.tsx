import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { App } from '../App';
import { buildCallTree, type CallGraph } from '../lib/re';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';

const appCss = readFileSync(resolve(__dirname, '../styles/app.css'), 'utf8');

// R6 piece 3: the References and Call graph views of the function pane — links that select another function (table, pane
// and URL), Back/Forward (buttons and Alt+Left/Alt+Right), the tree's expand/collapse and keyboard, caps, cycles and errors.
// Response shapes follow the controller's /xrefs and /callgraph routes.
const exe: Module = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'a', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const ENTRY = '0x140001000';
const MAIN = '0x140001190';
const HELPER = '0x140002b70';
const FAR = '0x140003000'; // not on the list page: selecting it still works through the URL
const ROWS = [
  { addr: ENTRY, name: 'entry0', size: 40, nbbs: 3 },
  { addr: MAIN, name: 'main', size: 900, nbbs: 40 },
  { addr: HELPER, name: 'fcn.140002b70', size: 18, nbbs: 2 },
];
const ALL = [...ROWS, { addr: FAR, name: 'fcn.140003000', size: 64, nbbs: 4 }];
const list = { functions: ROWS, total: 3, offset: 0, returned: 3, more: false, session_id: 'res_' + '0'.repeat(22), sort: 'addr', contains: '' };
const detail = (r: (typeof ALL)[number]) => ({
  addr: r.addr,
  header: { ...r, range: { start: r.addr, end: '0x' + (parseInt(r.addr, 16) + r.size).toString(16) } },
  blocks: [],
  variables: [],
  decompilers: [{ id: 'rizin', available: true, real_decompiler: true, label: 'rizin (rz-ghidra)' }],
  session_id: list.session_id,
  untrusted: true,
});
const dir = (addr: string, d: 'to' | 'from', refs: object[], total = refs.length) => ({ addr, direction: d, refs, total, more: total > refs.length });
const xrefs = (addr: string, name: string, to: object[], from: object[], extra: object = {}) => ({
  addr,
  function: name,
  internal_jumps: 0,
  to: dir(addr, 'to', to),
  from: dir(addr, 'from', from),
  limit: 200,
  untrusted: true,
  ...extra,
});
const XREFS: Record<string, unknown> = {
  [MAIN]: xrefs(
    MAIN,
    'main',
    [
      { from: '0x140001010', to: MAIN, type: 'CALL', kind: 'call', fcn_addr: ENTRY, fcn_name: 'entry0' },
      { from: '0x14005e010', to: MAIN, type: 'DATA', kind: 'data', fcn_addr: null, fcn_name: null },
    ],
    [
      { from: '0x1400011a0', to: HELPER, type: 'CALL', kind: 'call', to_fcn_addr: HELPER, to_fcn_name: 'fcn.140002b70' },
      { from: '0x1400011b0', to: '0x14005a000', type: 'DATA', kind: 'string', to_fcn_addr: null, to_fcn_name: null, string: '<b>usage</b>: %s' },
      { from: '0x1400011c0', to: '0x14005e1a8', type: 'DATA', kind: 'data', to_fcn_addr: null, to_fcn_name: null, name: 'KERNEL32.dll!GetLastError' },
      { from: '0x1400011d0', to: '0x140003010', type: 'CODE', kind: 'jump', to_fcn_addr: FAR, to_fcn_name: 'fcn.140003000' },
    ],
    { internal_jumps: 3 },
  ),
  [ENTRY]: xrefs(ENTRY, 'entry0', [], [{ from: '0x140001010', to: MAIN, type: 'CALL', kind: 'call', to_fcn_addr: MAIN, to_fcn_name: 'main' }]),
  [HELPER]: xrefs(HELPER, 'fcn.140002b70', [{ from: '0x1400011a0', to: HELPER, type: 'CALL', kind: 'call', fcn_addr: MAIN, fcn_name: 'main' }], [{ from: '0x140002b74', to: MAIN, type: 'CALL', kind: 'call', to_fcn_addr: MAIN, to_fcn_name: 'main' }]),
  [FAR]: xrefs(FAR, 'fcn.140003000', [], []),
};
// main -> helper -> main (a cycle); main -> far -> 0x140004000 (at the depth limit); helper -> far (listed earlier); capped
const GRAPH: CallGraph = {
  root: MAIN,
  direction: 'callees',
  depth: 2,
  max_nodes: 200,
  node_limit_hit: true,
  nodes: [
    { addr: MAIN, name: 'main', depth: 0 },
    { addr: HELPER, name: 'fcn.140002b70', depth: 1 },
    { addr: FAR, name: 'fcn.140003000', depth: 1 },
    { addr: '0x140004000', name: 'fcn.140004000', depth: 2 },
  ],
  edges: [
    { from: MAIN, to: HELPER },
    { from: MAIN, to: FAR },
    { from: HELPER, to: MAIN },
    { from: HELPER, to: FAR },
    { from: FAR, to: '0x140004000' },
  ],
  untrusted: true,
};
const errorResponse = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

function fnRoutes(): Routes {
  const out: Routes = { '/cases/c1/modules': [exe], '/cases/c1/modules/mod1/functions': list };
  for (const r of ALL) {
    const b = `/cases/c1/modules/mod1/functions/${r.addr}`;
    out[b] = detail(r);
    out[`${b}/decompiled`] = { function: r.name, addr: r.addr, decompiler: 'rz-ghidra(pdg)', is_real_decompiler: true, engine: 'rizin', requested: 'rizin', decompiled: { text: 'int f(void);' }, untrusted: true };
    out[`${b}/xrefs`] = XREFS[r.addr];
    out[`${b}/callgraph`] = r.addr === MAIN ? GRAPH : { ...GRAPH, root: r.addr, nodes: [{ addr: r.addr, name: r.name, depth: 0 }], edges: [], node_limit_hit: false };
  }
  return out;
}

beforeEach(() => localStorage.clear());

async function open(over: Routes = {}, hash = `#/projects/c1/functions?fn=${MAIN}&view=references`) {
  window.location.hash = hash;
  const m = makeStore(caseRoutes({ ...fnRoutes(), ...over }));
  render(<App store={m.store} />);
  const tab = await screen.findByTestId('functions-tab');
  const pane = await within(tab).findByTestId('fn-pane');
  return { ...m, tab, pane };
}

describe('References view (R6 piece 3)', () => {
  it('lists Called from and Calls / uses with address, kind and the containing function; program text stays plain text', async () => {
    const { store, pane, calls } = await open();
    expect(within(pane).getByRole('tab', { name: 'References' })).toHaveAttribute('aria-selected', 'true');
    const to = await within(pane).findByTestId('fn-refs-to');
    const caller = within(to).getByTestId('fn-ref-to-0');
    expect(caller).toHaveTextContent('0x140001010');
    expect(caller).toHaveTextContent('call');
    expect(within(caller).getByRole('link', { name: `Open entry0 at ${ENTRY}` })).toBeInTheDocument();
    expect(within(to).getByTestId('fn-ref-to-1')).toHaveTextContent('0x14005e010datanot inside an analysed function');
    const from = within(pane).getByTestId('fn-refs-from');
    expect(within(from).getByTestId('fn-ref-from-0')).toHaveTextContent(`${HELPER}callfcn.140002b70`);
    const str = within(from).getByTestId('fn-ref-from-1');
    expect(str).toHaveTextContent('string“<b>usage</b>: %s”');
    expect(str.querySelector('b')).toBeNull(); // never parsed as HTML
    expect(within(from).getByTestId('fn-ref-from-2')).toHaveTextContent('KERNEL32.dll!GetLastError import');
    expect(within(from).getByTestId('fn-ref-from-3')).toHaveTextContent('jumpfcn.140003000 (inside it, at 0x140003010)');
    expect(within(pane).getByTestId('fn-refs-internal')).toHaveTextContent('3 jumps inside this function are not listed');
    expect(calls.some((c) => c.path === `/cases/c1/modules/mod1/functions/${MAIN}/xrefs?limit=200`)).toBe(true);
    store.stop();
  });

  it('links select another function in the table, the pane and the URL, with Back/Forward buttons and Alt+Left/Alt+Right', async () => {
    const user = userEvent.setup();
    const { store, tab, pane } = await open({}, `#/projects/c1/functions?fn=${MAIN}&view=references`);
    expect(await within(pane).findByTestId('fn-pane-name')).toHaveTextContent('main');
    expect(within(tab).getByTestId(`fn-row-${MAIN}`)).toHaveAttribute('aria-selected', 'true');
    expect(within(pane).getByTestId('fn-back')).toBeDisabled();
    expect(within(pane).getByTestId('fn-back')).toHaveAttribute('data-tooltip', 'No earlier function opened in this pane');

    const link = within(await within(pane).findByTestId('fn-refs-to')).getByRole('link', { name: /Open entry0/ });
    expect(link.getAttribute('href')).toContain(`fn=${ENTRY}`); // a real link: can be opened in a new window
    await user.click(link);
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('entry0'));
    expect(window.location.hash).toContain(`fn=${ENTRY}`);
    expect(window.location.hash).toContain('view=references'); // the view is kept
    expect(within(tab).getByTestId(`fn-row-${ENTRY}`)).toHaveAttribute('aria-selected', 'true');
    expect(within(tab).getByTestId(`fn-row-${MAIN}`)).toHaveAttribute('aria-selected', 'false');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveFocus()); // focus follows the navigation
    expect(await within(pane).findByTestId('fn-refs-to-empty')).toHaveTextContent('Nothing in the analysed code calls or references this function');

    // a function that is not on the current page of the table opens too
    await user.click(within(within(pane).getByTestId('fn-refs-from')).getByRole('link', { name: /Open main/ }));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('main'));
    await user.click(within(await within(pane).findByTestId('fn-ref-from-3')).getByRole('link', { name: /Open fcn.140003000/ }));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140003000'));
    expect(window.location.hash).toContain(`fn=${FAR}`);

    // history: main -> entry0 -> main -> far
    await user.click(within(pane).getByTestId('fn-back'));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('main'));
    expect(within(pane).getByTestId('fn-back')).toHaveFocus(); // repeated Back keeps the focus on the button
    await user.click(within(pane).getByTestId('fn-back'));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('entry0'));
    expect(window.location.hash).toContain(`fn=${ENTRY}`);
    expect(within(pane).getByTestId('fn-forward')).toBeEnabled();
    within(pane).getByTestId('fn-pane-name').focus();
    await user.keyboard('{Alt>}{ArrowLeft}{/Alt}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('main'));
    expect(within(pane).getByTestId('fn-back')).toBeDisabled();
    await user.keyboard('{Alt>}{ArrowLeft}{/Alt}'); // nothing earlier: said, not an error
    expect(within(pane).getByTestId('fn-pane-announce')).toHaveTextContent('No earlier function in this pane.');
    await user.keyboard('{Alt>}{ArrowRight}{/Alt}{Alt>}{ArrowRight}{/Alt}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('main'));
    await user.keyboard('{Alt>}{ArrowRight}{/Alt}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140003000'));
    expect(within(pane).getByTestId('fn-forward')).toBeDisabled();

    // opening a new function from the table after going back drops the forward entries
    await user.click(within(pane).getByTestId('fn-back'));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('main'));
    await user.click(within(tab).getByTestId(`fn-row-${HELPER}`));
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140002b70'));
    expect(within(pane).getByTestId('fn-forward')).toBeDisabled();
    store.stop();
  });

  it('caps long lists with a way to see more, and shows errors with the next action and Retry', async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 200 }, (_, i) => ({ from: '0x' + (0x140010000 + i * 16).toString(16), to: MAIN, type: 'CALL', kind: 'call', fcn_addr: ENTRY, fcn_name: 'entry0' }));
    let fail = true;
    let big = false;
    const { store, pane, calls } = await open({
      [`/cases/c1/modules/mod1/functions/${MAIN}/xrefs`]: () =>
        big
          ? { ...(XREFS[MAIN] as object), to: dir(MAIN, 'to', [...many, ...many].slice(0, 350), 350), limit: 1000 }
          : { ...(XREFS[MAIN] as object), to: dir(MAIN, 'to', many, 350) },
      [`/cases/c1/modules/mod1/functions/${HELPER}/xrefs`]: () =>
        fail
          ? errorResponse(504, 'timeout', 'The analysis took too long and was stopped.', 'Retry; if it keeps happening, lower the analysis depth in the project settings.')
          : XREFS[HELPER],
    });
    const notice = await within(pane).findByTestId('fn-refs-more-to');
    expect(notice).toHaveTextContent('Showing the first 200 of 350.');
    big = true;
    await user.click(within(notice).getByTestId('fn-refs-all-to'));
    await waitFor(() => expect(within(pane).getAllByTestId(/^fn-ref-to-/)).toHaveLength(350));
    expect(calls.some((c) => c.path.endsWith('/xrefs?limit=1000'))).toBe(true);

    window.location.hash = `#/projects/c1/functions?fn=${HELPER}&view=references`;
    const alert = await within(pane).findByRole('alert');
    expect(alert).toHaveTextContent('References could not be listed');
    expect(alert).toHaveTextContent('Next: Retry; if it keeps happening');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(pane).findByTestId('fn-refs-to')).toHaveTextContent('main');
    store.stop();
  });
});

describe('Call graph view (R6 piece 3)', () => {
  it('builds a bounded tree: each function expanded once, cycles and repeats marked, depth-limit leaves flagged', () => {
    const t = buildCallTree(GRAPH);
    expect(t.addr).toBe(MAIN);
    expect(t.children.map((c) => [c.name, c.repeat])).toEqual([
      ['fcn.140002b70', undefined],
      ['fcn.140003000', undefined],
    ]);
    const helper = t.children[0];
    expect(helper.children.map((c) => [c.name, c.repeat])).toEqual([
      ['main', 'cycle'],
      ['fcn.140003000', 'seen'],
    ]);
    expect(t.children[1].children[0]).toMatchObject({ name: 'fcn.140004000', frontier: true, relation: 'calls' });
    // callers: edges are followed backwards
    const up = buildCallTree({ ...GRAPH, direction: 'callers', root: FAR });
    expect(up.children.map((c) => [c.name, c.relation])).toEqual([
      ['main', 'called-by'],
      ['fcn.140002b70', 'called-by'],
    ]);
    // both: an item's children are what it calls and what calls it, without walking back the edge it came through
    const both = buildCallTree({ ...GRAPH, direction: 'both', root: FAR });
    expect(both.children.map((c) => [c.name, c.relation])).toEqual([
      ['fcn.140004000', 'calls'],
      ['main', 'called-by'],
      ['fcn.140002b70', 'called-by'],
    ]);
    const viaMain = both.children[1];
    expect(viaMain.children.map((c) => [c.name, c.relation, c.repeat])).toEqual([['fcn.140002b70', 'calls', 'seen'], ['fcn.140002b70', 'called-by', 'seen']]);
  });

  it('renders the tree with expand/collapse by mouse and keyboard, Enter opens a function, and shows the cap and depth notices', async () => {
    const user = userEvent.setup();
    const { store, pane } = await open({}, `#/projects/c1/functions?fn=${MAIN}&view=callgraph`);
    const tree = await within(pane).findByRole('tree', { name: 'Call graph of main' });
    expect(within(pane).getByTestId('fn-graph-summary')).toHaveTextContent('4 functions and 5 calls within depth 2.');
    expect(within(pane).getByTestId('fn-graph-capped')).toHaveTextContent('The graph stopped at 200 functions');
    expect(within(pane).getByTestId('fn-graph-frontier')).toHaveTextContent('1 function is at depth 2 and was not followed further');
    const root = within(tree).getByTestId(`fn-tree-${MAIN}`);
    expect(root).toHaveAttribute('aria-level', '1');
    expect(root).toHaveAttribute('aria-expanded', 'true');
    const helper = within(tree).getByTestId(`fn-tree-${MAIN}>${HELPER}`);
    expect(helper).toHaveAttribute('aria-level', '2');
    expect(helper).toHaveAttribute('aria-expanded', 'true'); // first level open by default
    expect(within(helper).getByTestId('fn-tree-cycle')).toHaveTextContent('cycle: already open above');
    const far = within(tree).getByTestId(`fn-tree-${MAIN}>${FAR}`);
    expect(far).toHaveAttribute('aria-expanded', 'true');
    expect(far).toHaveTextContent('depth limit');

    // keyboard: Down to the first child, Left closes it, Right opens it, Right again goes into it, Left goes back up
    root.focus();
    await user.keyboard('{ArrowDown}');
    expect(helper).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    expect(helper).toHaveAttribute('aria-expanded', 'false');
    expect(within(helper).queryByRole('group')).toBeNull();
    await user.keyboard('{ArrowRight}');
    expect(helper).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard('{ArrowRight}');
    expect(within(tree).getByTestId(`fn-tree-${MAIN}>${HELPER}>${MAIN}`)).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    expect(helper).toHaveFocus();
    await user.keyboard('{End}');
    expect(within(tree).getByTestId(`fn-tree-${MAIN}>${FAR}>0x140004000`)).toHaveFocus();
    await user.keyboard('{Home}');
    expect(root).toHaveFocus();

    // mouse: collapse all, expand all, and the branch toggle
    await user.click(within(pane).getByTestId('fn-graph-collapse-all'));
    expect(root).toHaveAttribute('aria-expanded', 'true'); // the root stays open, every branch under it closes
    expect(within(tree).getByTestId(`fn-tree-${MAIN}>${HELPER}`)).toHaveAttribute('aria-expanded', 'false');
    expect(within(tree).queryByTestId(`fn-tree-${MAIN}>${HELPER}>${MAIN}`)).toBeNull();
    await user.click(within(pane).getByTestId('fn-graph-expand-all'));
    expect(within(tree).getByTestId(`fn-tree-${MAIN}>${FAR}>0x140004000`)).toBeInTheDocument();
    const toggle = within(tree).getByTestId(`fn-tree-${MAIN}>${FAR}`).querySelector('.fn-tree-toggle') as HTMLElement;
    await user.click(toggle);
    expect(within(tree).getByTestId(`fn-tree-${MAIN}>${FAR}`)).toHaveAttribute('aria-expanded', 'false');

    // Enter on an item opens that function in the pane (and the URL), keeping the Call graph view
    within(tree).getByTestId(`fn-tree-${MAIN}>${HELPER}`).focus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140002b70'));
    expect(window.location.hash).toContain(`fn=${HELPER}`);
    expect(window.location.hash).toContain('view=callgraph');
    expect(await within(pane).findByTestId('fn-graph-summary')).toHaveTextContent('This function calls no other analysed function.');
    store.stop();
  });

  it('sends direction, depth and the node cap, keeps them while navigating, and raises the cap from the notice', async () => {
    const user = userEvent.setup();
    const { store, pane, calls } = await open({}, `#/projects/c1/functions?fn=${MAIN}&view=callgraph`);
    await within(pane).findByRole('tree');
    const graphCalls = () => calls.filter((c) => c.path.includes('/callgraph')).map((c) => c.path);
    expect(graphCalls().at(-1)).toBe(`/cases/c1/modules/mod1/functions/${MAIN}/callgraph?direction=callees&depth=2&max_nodes=200`);
    await user.click(within(pane).getByTestId('fn-graph-raise'));
    await waitFor(() => expect(graphCalls().at(-1)).toContain('max_nodes=500'));
    await user.selectOptions(within(pane).getByTestId('fn-graph-direction'), 'both');
    await user.selectOptions(within(pane).getByTestId('fn-graph-depth'), '4');
    await waitFor(() => expect(graphCalls().at(-1)).toBe(`/cases/c1/modules/mod1/functions/${MAIN}/callgraph?direction=both&depth=4&max_nodes=500`));
    // the settings follow the user to another function
    await user.click(within(await within(pane).findByRole('tree')).getAllByRole('link', { name: `Open fcn.140003000 at ${FAR}` })[0]);
    await waitFor(() => expect(graphCalls().at(-1)).toBe(`/cases/c1/modules/mod1/functions/${FAR}/callgraph?direction=both&depth=4&max_nodes=500`));
    store.stop();
  });

  it('shows an error with the next action and Retry, and stacks at narrow widths', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { store, pane } = await open(
      {
        [`/cases/c1/modules/mod1/functions/${MAIN}/callgraph`]: () =>
          fail ? errorResponse(404, 'function_not_found', `No analysed function contains ${MAIN} in this module.`, 'Pick a function from the list; the list may be out of date, so refresh it.') : GRAPH,
      },
      `#/projects/c1/functions?fn=${MAIN}&view=callgraph`,
    );
    const alert = await within(pane).findByRole('alert');
    expect(alert).toHaveTextContent('The call graph could not be built');
    expect(alert).toHaveTextContent('Next: Pick a function from the list');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(pane).findByRole('tree')).toBeInTheDocument();
    // jsdom has no layout: check the narrow-width rules (pane stacks; the "At" column and tree indent shrink)
    expect(appCss).toMatch(/@media \(max-width: 1100px\) \{\s*\.fn-layout\.with-pane \{ grid-template-columns: minmax\(0, 1fr\); \}/);
    expect(appCss).toMatch(/@media \(max-width: 820px\) \{[^}]*\}[^@]*\.fn-refs-table \.col-at \{ display: none; \}/);
    store.stop();
  });
});
