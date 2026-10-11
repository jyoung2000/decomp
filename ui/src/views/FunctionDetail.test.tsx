import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const appCss = readFileSync(resolve(__dirname, '../styles/app.css'), 'utf8');

// R6 piece 2: the side pane of the function selected in the Functions table (header, Decompiled with the rizin/Ghidra switch,
// Disassembly), its error states, Escape, deep links and the narrow layout. Response shapes follow the controller routes.
const exe: Module = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'a', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const ROWS = [
  { addr: '0x140001000', name: 'entry0', size: 40, nbbs: 3, signature: 'void entry0(void);' },
  { addr: '0x140001190', name: 'main', size: 900, nbbs: 40, signature: 'int main(int argc, char **argv);' },
  { addr: '0x140002b70', name: 'fcn.140002b70', size: 18, span: 23718, nbbs: 2 },
];
const list = { functions: ROWS, total: 3, offset: 0, returned: 3, more: false, session_id: 'res_' + '0'.repeat(22), sort: 'addr', contains: '' };
const decompilers = [
  { id: 'rizin', available: true, real_decompiler: true, label: 'rizin (rz-ghidra)' },
  { id: 'ghidra', available: false, real_decompiler: true, label: 'Ghidra', reason: 'GHIDRA_INSTALL_DIR is not set' },
];
const detail = (r: (typeof ROWS)[number]) => ({
  addr: r.addr,
  header: { ...r, calltype: 'ms', range: { start: r.addr, end: '0x' + (parseInt(r.addr, 16) + (r.span ?? r.size)).toString(16) } },
  blocks: [],
  variables: [],
  decompilers,
  session_id: list.session_id,
  untrusted: true,
});
const decompiled = (r: (typeof ROWS)[number], real = true) => ({
  function: r.name,
  addr: r.addr,
  decompiler: real ? 'rz-ghidra(pdg)' : 'pdc(pseudo)',
  is_real_decompiler: real,
  engine: 'rizin',
  requested: 'rizin',
  decompiled: { text: `${r.signature ?? 'int ' + r.name + '(void)'}\n{\n    // "<b>not html</b>"\n    return 0;\n}\n`, truncated: false },
  untrusted: true,
});
const disasm = (r: (typeof ROWS)[number]) => ({
  function: r.name,
  addr: r.addr,
  ops: [
    { addr: r.addr, bytes: '55', disasm: 'push rbp' },
    { addr: '0x' + (parseInt(r.addr, 16) + 1).toString(16), bytes: '4889e5', disasm: 'mov rbp, rsp', user_comment: 'set up the frame' },
    { addr: '0x' + (parseInt(r.addr, 16) + 4).toString(16), bytes: 'c3', disasm: 'ret' },
  ],
  total: 3,
  offset: 0,
  returned: 3,
  more: false,
  untrusted: true,
});
const errorResponse = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

function fnRoutes(): Routes {
  const out: Routes = { '/cases/c1/modules': [exe], '/cases/c1/modules/mod1/functions': list };
  for (const r of ROWS) {
    const b = `/cases/c1/modules/mod1/functions/${r.addr}`;
    out[b] = detail(r);
    out[`${b}/decompiled`] = decompiled(r, !r.span);
    out[`${b}/disassembly`] = disasm(r);
  }
  return out;
}

beforeEach(() => localStorage.clear());

async function open(over: Routes = {}, hash = '#/projects/c1/functions') {
  window.location.hash = hash;
  const m = makeStore(caseRoutes({ ...fnRoutes(), ...over }));
  render(<App store={m.store} />);
  const tab = await screen.findByTestId('functions-tab');
  return { ...m, tab };
}

describe('Function detail pane (R6 piece 2)', () => {
  it('opens on select with the header and the decompiled code as plain text, and puts the selection in the URL', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    expect(within(tab).queryByTestId('fn-pane')).not.toBeInTheDocument();
    await user.click(await within(tab).findByTestId('fn-row-0x140001190'));
    const pane = await within(tab).findByTestId('fn-pane');
    expect(await within(pane).findByTestId('fn-pane-name')).toHaveTextContent('main');
    const header = await within(pane).findByTestId('fn-pane-header');
    expect(header).toHaveTextContent('0x140001190');
    expect(within(header).getByTestId('fn-pane-size')).toHaveTextContent('900 bytes');
    expect(within(header).getByTestId('fn-pane-sig')).toHaveTextContent('int main(int argc, char **argv);');
    expect(within(pane).getByRole('tab', { name: 'Decompiled' })).toHaveAttribute('aria-selected', 'true');
    const code = await within(pane).findByTestId('fn-dec-code');
    expect(code).toHaveTextContent('<b>not html</b>');                      // shown as text, never parsed as HTML
    expect(code.querySelector('b')).toBeNull();
    expect(within(pane).getByTestId('fn-dec-engine')).toHaveTextContent('Produced by rz-ghidra(pdg) real decompiler');
    expect(window.location.hash).toContain('fn=0x140001190');
    expect(within(tab).getByTestId('fn-row-0x140001190')).toHaveAttribute('aria-selected', 'true');
    store.stop();
  });

  it('labels the rizin pseudo-code fallback and shows the address span of a spread-out function', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    await user.click(await within(tab).findByTestId('fn-row-0x140002b70'));
    const pane = await within(tab).findByTestId('fn-pane');
    expect(await within(pane).findByTestId('fn-pane-size')).toHaveTextContent('18 bytes spread over an address span of 23,718 bytes');
    expect(await within(pane).findByTestId('fn-dec-engine')).toHaveTextContent('Produced by pdc(pseudo) fallback rizin pseudo-code, not a real decompiler');
    store.stop();
  });

  it('switches to Disassembly (address, bytes, instruction) and copies the shown code', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    await user.click(await within(tab).findByTestId('fn-row-0x140001190'));
    const pane = await within(tab).findByTestId('fn-pane');
    await within(pane).findByTestId('fn-dec-code');
    await user.click(within(pane).getByTestId('fn-copy-decompiled'));
    expect(await navigator.clipboard.readText()).toContain('int main(int argc, char **argv);');
    expect(within(pane).getByTestId('fn-copy-decompiled-status')).toHaveTextContent('Copied the decompiled code.');

    await user.click(within(pane).getByRole('tab', { name: 'Disassembly' }));
    const table = await within(pane).findByTestId('fn-disasm-table');
    const op = within(table).getByTestId('fn-op-0x140001191');
    expect(op).toHaveTextContent('0x140001191');
    expect(op).toHaveTextContent('4889e5');
    expect(op).toHaveTextContent('mov rbp, rsp ; set up the frame');
    expect(within(pane).getByTestId('fn-disasm-summary')).toHaveTextContent('Instructions 1–3 of 3.');
    expect(window.location.hash).toContain('view=disassembly');
    await user.click(within(pane).getByTestId('fn-copy-disasm'));
    expect(await navigator.clipboard.readText()).toMatch(/^0x140001190 {2}55 +push rbp\n0x140001191 {2}4889e5 +mov rbp, rsp {2}; set up the frame/);
    store.stop();
  });

  it('switches the decompiler to Ghidra, explains that it is not installed with the next action, and goes back to rizin', async () => {
    const user = userEvent.setup();
    const { store, tab, calls } = await open({
      '/cases/c1/modules/mod1/functions/0x140001190/decompiled': () => {
        const last = calls.filter((c) => c.path.includes('/decompiled')).at(-1)!.path;
        return last.includes('decompiler=ghidra')
          ? errorResponse(409, 'decompiler_unavailable', 'Ghidra is not installed (or not usable) on this computer, so this function cannot be decompiled with Ghidra.', 'Switch the decompiler back to rizin, or install Ghidra and Temurin JDK 21 on the Tools page.')
          : decompiled(ROWS[1]);
      },
    });
    await user.click(await within(tab).findByTestId('fn-row-0x140001190'));
    const pane = await within(tab).findByTestId('fn-pane');
    await within(pane).findByTestId('fn-dec-code');
    const sw = within(pane).getByTestId('fn-dec-switch');
    expect(sw).toHaveTextContent('Ghidra (not installed)');
    const ghidra = within(sw).getByRole('radio', { name: /Ghidra/ });
    expect(ghidra).toBeEnabled();                                         // not disabled: choosing it explains why it cannot run
    await user.click(ghidra);
    const box = await within(pane).findByTestId('fn-dec-unavailable');
    expect(box).toHaveTextContent('Ghidra is not installed');
    expect(box).toHaveTextContent('Next: Switch the decompiler back to rizin');
    expect(calls.some((c) => c.path === '/cases/c1/modules/mod1/functions/0x140001190/decompiled?decompiler=ghidra')).toBe(true);
    expect(window.location.hash).toContain('decompiler=ghidra');
    await user.click(within(box).getByTestId('fn-use-rizin'));
    expect(await within(pane).findByTestId('fn-dec-code')).toBeInTheDocument();
    expect(within(within(pane).getByTestId('fn-dec-switch')).getByRole('radio', { name: /rizin/ })).toBeChecked();
    store.stop();
  });

  it('closes with Escape and returns focus to the selected row; follows the selection when it changes', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    const rows = within(await within(tab).findByTestId('fn-table')).getAllByRole('row').slice(1);
    rows[0].focus();
    await user.keyboard('{ArrowDown}{Enter}');
    const pane = await within(tab).findByTestId('fn-pane');
    expect(await within(pane).findByTestId('fn-pane-name')).toHaveTextContent('main');
    await user.keyboard('{ArrowDown}{Enter}');                            // selection change: the pane follows
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('fcn.140002b70'));
    await within(pane).findByTestId('fn-dec-code');
    within(pane).getByRole('tab', { name: 'Decompiled' }).focus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(within(tab).queryByTestId('fn-pane')).not.toBeInTheDocument());
    expect(within(tab).getByTestId('fn-row-0x140002b70')).toHaveFocus();
    expect(window.location.hash).not.toContain('fn=');
    store.stop();
  });

  it('opens from a deep link with the view in the URL', async () => {
    const { store, tab } = await open({}, '#/projects/c1/functions?fn=0x140001000&view=disassembly');
    const pane = await within(tab).findByTestId('fn-pane');
    expect(await within(pane).findByTestId('fn-pane-name')).toHaveTextContent('entry0');
    expect(within(pane).getByRole('tab', { name: 'Disassembly' })).toHaveAttribute('aria-selected', 'true');
    expect(await within(pane).findByTestId('fn-op-0x140001000')).toHaveTextContent('push rbp');
    store.stop();
  });

  it('shows an error with the next action when the function cannot be opened, and when decompiling fails', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open({
      '/cases/c1/modules/mod1/functions/0x140001000': () => errorResponse(404, 'function_not_found', 'No analysed function contains 0x140001000 in this module.', 'Pick a function from the list; the list may be out of date, so refresh it.'),
      '/cases/c1/modules/mod1/functions/0x140001000/decompiled': () => errorResponse(504, 'timeout', 'The analysis took too long and was stopped.', 'Retry; if it keeps happening, lower the analysis depth in the project settings.'),
    });
    await user.click(await within(tab).findByTestId('fn-row-0x140001000'));
    const pane = await within(tab).findByTestId('fn-pane');
    const alerts = await within(pane).findAllByRole('alert');
    const text = alerts.map((a) => a.textContent).join(' | ');
    expect(text).toContain('What happened: No analysed function contains 0x140001000');
    expect(text).toContain('Next: Pick a function from the list');
    await waitFor(() => expect(within(pane).getAllByRole('alert').map((a) => a.textContent).join(' ')).toContain('The analysis took too long'));
    expect(within(pane).getAllByRole('button', { name: 'Retry' }).length).toBeGreaterThan(0);
    store.stop();
  });

  it('stacks the pane below the table at narrow widths', async () => {
    const user = userEvent.setup();
    const { store, tab } = await open();
    await user.click(await within(tab).findByTestId('fn-row-0x140001190'));
    const pane = await within(tab).findByTestId('fn-pane');
    const layout = pane.parentElement!;
    expect(layout).toHaveClass('fn-layout', 'with-pane');
    expect(layout.firstElementChild).toContainElement(within(tab).getByTestId('fn-table'));   // list first, pane after it
    // jsdom has no layout engine: check the stylesheet rule that collapses the two columns into one at narrow widths
    const narrow = /@media \(max-width: 1100px\) \{\s*\.fn-layout\.with-pane \{ grid-template-columns: minmax\(0, 1fr\); \}/;
    expect(appCss).toMatch(narrow);
    store.stop();
  });
});
