import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { caseRoutes, makeStore, type Routes } from '../test-utils';
import type { Module } from '../lib/types';
import { describeChange, whoLabel } from '../lib/re';

// R6 piece 5: editing in the function pane — rename (inline, validated as you type), prototype, comments (header and disassembly
// rows), variables (rename / retype), the Types dialog, and the History tab (who made each revision, Undo, Roll back, Accept /
// Reject). The routes below behave like the controller's: each edit stores a new revision and the views reflect it.
const exe: Module = { module_id: 'mod1', rel_path: 'pecli.exe', sha256: 'a', size: 1, format: 'pe', profile: 'native_pe', arch: 'x86_64' };
const FN = '0x140001190';
const err = (status: number, code: string, message: string, next_action: string) =>
  new Response(JSON.stringify({ error: { code, message, next_action } }), { status, headers: { 'Content-Type': 'application/json' } });

const EX = { name: 'parse_args', type: 'uint32_t, char *, struct point *', prototype: 'int parse_args(int argc, char **argv)', comment: 'Opens the settings file', declaration: 'struct point { int x; int y; };' };
function grammar(what: keyof typeof EX, v: string): string | null {
  if (what === 'name') return /^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(v) ? null : /^[0-9]/.test(v) ? `invalid name '${v}': it starts with a digit.` : `invalid name '${v}': it contains characters that are not allowed.`;
  if (what === 'type') return /^((struct|unsigned|const)\s+)*[A-Za-z_]\w*(\s*\*{1,3})?$/.test(v) ? null : `invalid C type '${v}': no array sizes or function pointers.`;
  if (what === 'prototype') return /^[\w *,()]+\)$/.test(v) && (v.match(/\(/g) ?? []).length === 1 ? null : `invalid prototype '${v}': it needs exactly one '(' and one ')'.`;
  if (what === 'comment') return /[\n\r]/.test(v) ? 'comment must be a single line without control characters' : null;
  return /^\s*(struct|union|enum|typedef)\b/.test(v) && !v.includes('#') ? null : "declaration contains characters that are not allowed ('#')";
}

type Rev = { evidence_id: string; revision: number; change: Record<string, unknown>; who: string };
function model() {
  const st = {
    name: 'main',
    signature: 'int main(int argc, char **argv);',
    vars: [
      { name: 'argc', type: 'int', is_arg: true, storage: 'reg' },
      { name: 'var_38h', type: 'int64_t', is_arg: false, storage: 'stack' },
    ],
    comments: { '0x140001191': 'set up the frame' } as Record<string, string>,
    types: [] as { decl: string; names: { kind: string; name: string }[]; sha256: string }[],
    history: [
      { evidence_id: 'ev1', revision: 1, change: { action: 'rename', kind: 'function', addr: '0x140001000', old: 'entry0', new: 'crt_start', author: 'model', label: 'model-proposed' }, who: 'model-proposed' },
      { evidence_id: 'ev2', revision: 2, change: { action: 'review', decision: 'confirm', kind: 'comment', key: '0x140001000', value: 'x', author: 'test' }, who: 'confirmed by test' },
    ] as Rev[],
    proposals: [
      { kind: 'function', key: FN, local: null, value: 'parse_command_line', status: 'proposed', label: 'model-proposed', model: 'qwen2.5-coder:14b', analysis_name: 'main' },
      { kind: 'comment', key: '0x140001000', local: null, value: 'Entry', status: 'confirmed', label: 'confirmed by a test' },
    ],
  };
  const bump = (change: Record<string, unknown>) => {
    const revision = st.history.length + 1;
    st.history.push({ evidence_id: `ev${revision}`, revision, change: { ...change, author: 'user' }, who: 'person' });
    return { revision, change, author: 'user', who: 'person', untrusted: true };
  };
  const row = () => ({ addr: FN, name: st.name, size: 900, nbbs: 40, signature: st.signature });
  const routes: Routes = {
    '/cases/c1/modules': [exe],
    '/cases/c1/modules/mod1/functions': () => ({ functions: [{ addr: '0x140001000', name: 'entry0', size: 40, nbbs: 3 }, row()], total: 2, offset: 0, returned: 2, more: false, session_id: 'res_x', sort: 'addr', contains: '' }),
    [`/cases/c1/modules/mod1/functions/${FN}`]: () => ({
      addr: FN,
      header: { ...row(), calltype: 'ms', range: { start: FN, end: '0x140001514' } },
      blocks: [],
      variables: st.vars,
      annotations: { function: st.name !== 'main' ? { name: st.name, prov: { origin: 'user', status: 'accepted' } } : null, comments: Object.entries(st.comments).map(([addr, text]) => ({ addr, text })) },
      decompilers: [{ id: 'rizin', available: true, real_decompiler: true, label: 'rizin (rz-ghidra)' }],
      session_id: 'res_x',
      untrusted: true,
    }),
    [`/cases/c1/modules/mod1/functions/${FN}/decompiled`]: () => ({
      function: st.name, addr: FN, decompiler: 'rz-ghidra(pdg)', is_real_decompiler: true, engine: 'rizin', requested: 'rizin', untrusted: true,
      decompiled: { text: `${Object.entries(st.comments).map(([a, t]) => `// [annotation @${a}] ${t}\n`).join('')}${st.signature.replace(';', '')}\n{\n    ${st.vars[1].type} ${st.vars[1].name};\n}\n` },
    }),
    [`/cases/c1/modules/mod1/functions/${FN}/disassembly`]: () => ({
      function: st.name, addr: FN, untrusted: true, total: 3, offset: 0, returned: 3, more: false,
      ops: [FN, '0x140001191', '0x140001194'].map((a, i) => ({ addr: a, bytes: ['55', '4889e5', 'c3'][i], disasm: ['push rbp', 'mov rbp, rsp', 'ret'][i], user_comment: st.comments[a] })),
    }),
    'POST /cases/c1/modules/mod1/annotations/check': (b: unknown) => {
      const { what, value } = b as { what: keyof typeof EX; value: string };
      const m = grammar(what, value);
      return m ? { ok: false, what, message: m, example: EX[what] } : { ok: true, what, normalized: value.replace(/\s+/g, ' ').trim(), example: EX[what] };
    },
    'POST /cases/c1/modules/mod1/annotations/rename': (b: unknown) => {
      const body = b as { kind: string; address: string; variable?: string; new_name: string };
      if (grammar('name', body.new_name)) return err(400, 'invalid_name', grammar('name', body.new_name)!, `Fix the name and retry; a valid one looks like: ${EX.name}`);
      if (body.new_name === 'taken') return err(400, 'edit_refused', "rizin did not accept the name (function is still 'main')", 'Read the message, fix the value and retry.');
      if (body.kind === 'local') {
        const v = st.vars.find((x) => x.name === body.variable)!;
        const old = v.name;
        v.name = body.new_name;
        return bump({ action: 'rename', kind: 'local', function: FN, old, new: body.new_name });
      }
      const old = st.name;
      st.name = body.new_name;
      st.signature = st.signature.replace(/\w+\(/, `${body.new_name}(`);
      return bump({ action: 'rename', kind: 'function', addr: FN, old, new: body.new_name });
    },
    'POST /cases/c1/modules/mod1/annotations/set-type': (b: unknown) => {
      const body = b as { kind: string; prototype?: string; variable?: string; type?: string };
      if (body.prototype && grammar('prototype', body.prototype)) return err(400, 'invalid_prototype', grammar('prototype', body.prototype)!, `Fix the prototype and retry; a valid one looks like: ${EX.prototype}`);
      if (body.kind === 'local') {
        if (body.type === 'struct nope *') return err(400, 'type_not_accepted', "rizin did not accept type 'struct nope *' for 'var_38h'", 'Declare the struct, union or enum first under Types (e.g. struct point { int x; int y; };), then set the type.');
        st.vars.find((x) => x.name === body.variable)!.type = body.type!;
        return bump({ action: 'set_type', kind: 'local', function: FN, variable: body.variable, type: body.type });
      }
      st.signature = `${body.prototype};`;
      st.name = /(\w+)\s*\(/.exec(body.prototype!)![1];
      return bump({ action: 'set_type', kind: 'function', addr: FN, prototype: body.prototype });
    },
    'POST /cases/c1/modules/mod1/annotations/comment': (b: unknown) => {
      const body = b as { address: string; text: string };
      if (body.text) st.comments[body.address] = body.text;
      else delete st.comments[body.address];
      return bump({ action: 'comment', addr: body.address, text: body.text });
    },
    'POST /cases/c1/modules/mod1/annotations/declare': (b: unknown) => {
      const d = (b as { declaration: string }).declaration;
      const names = [...d.matchAll(/(struct|union|enum)\s+(\w+)\s*\{/g)].map((m) => ({ kind: m[1], name: m[2] }));
      st.types.push({ decl: d, names, sha256: String(st.types.length) });
      return bump({ action: 'apply_struct', names });
    },
    '/cases/c1/modules/mod1/annotations': () => ({
      revision: st.history.length,
      annotations: { functions: {}, locals: {}, globals: {}, comments: {}, types: st.types },
      proposals: st.proposals,
      open_proposals: st.proposals.filter((p) => p.status === 'proposed').length,
      history: st.history,
    }),
    'POST /cases/c1/modules/mod1/annotations/rollback': (b: unknown) => {
      const to = (b as { revision: number }).revision;
      st.history.push({ evidence_id: `ev${st.history.length + 1}`, revision: st.history.length + 1, change: { action: 'rollback', to_revision: to, author: 'user' }, who: 'person' });
      return { revision: st.history.length, change: { action: 'rollback', to_revision: to } };
    },
    'POST /cases/c1/modules/mod1/annotations/review': (b: unknown) => {
      const body = b as { kind: string; key: string; decision: string };
      const p = st.proposals.find((x) => x.kind === body.kind && x.key === body.key)!;
      if (body.decision === 'accept') Object.assign(p, { status: 'accepted', label: 'accepted by you' });
      else st.proposals = st.proposals.filter((x) => x !== p);
      st.history.push({ evidence_id: `ev${st.history.length + 1}`, revision: st.history.length + 1, change: { action: 'review', decision: body.decision, kind: body.kind, key: body.key, author: 'user' }, who: 'person' });
      return { revision: st.history.length };
    },
  };
  return { st, routes };
}

beforeEach(() => localStorage.clear());

async function open(over: Routes = {}, view = '') {
  const m = model();
  window.location.hash = `#/projects/c1/functions?fn=${FN}${view ? `&view=${view}` : ''}`;
  const s = makeStore(caseRoutes({ ...m.routes, ...over }));
  render(<App store={s.store} />);
  const tab = await screen.findByTestId('functions-tab');
  const pane = await within(tab).findByTestId('fn-pane');
  await within(pane).findByTestId('fn-edit-bar');
  return { ...s, ...m, tab, pane };
}
const posts = (calls: { method: string; path: string; body: unknown }[], end: string) => calls.filter((c) => c.method === 'POST' && c.path.endsWith(end));

describe('Function pane editing (R6 piece 5)', () => {
  it('renames inline: validated as you type, Escape cancels, Enter saves, then the header, list row and code refresh', async () => {
    const user = userEvent.setup();
    const { store, tab, pane, calls } = await open();
    await user.click(within(pane).getByTestId('fn-edit-name'));
    const input = within(pane).getByTestId('fn-name-edit-input');
    expect(input).toHaveFocus();
    expect(input).toHaveValue('main');
    await user.clear(input);
    await user.type(input, '2nd');
    await waitFor(() => expect(within(pane).getByTestId('fn-name-edit-check')).toHaveTextContent("invalid name '2nd': it starts with a digit. Example: parse_args"));
    expect(input).toHaveAttribute('aria-invalid', 'true');
    await user.keyboard('{Enter}');                                          // refused locally: nothing is sent
    expect(within(pane).getByTestId('fn-name-edit-refused')).toHaveTextContent('Not saved: invalid name');
    expect(posts(calls, '/annotations/rename')).toHaveLength(0);
    await user.keyboard('{Escape}');                                         // cancels the editor, not the pane
    expect(within(pane).queryByTestId('fn-name-edit')).not.toBeInTheDocument();
    expect(within(tab).getByTestId('fn-pane')).toBeInTheDocument();
    expect(within(pane).getByTestId('fn-edit-name')).toHaveFocus();

    within(pane).getByTestId('fn-pane-name').focus();
    await user.keyboard('{F2}');                                             // F2 opens the same editor
    const again = within(pane).getByTestId('fn-name-edit-input');
    await user.clear(again);
    await user.type(again, 'parse_args');
    await waitFor(() => expect(within(pane).getByTestId('fn-name-edit-check')).toHaveTextContent('Valid.'));
    const decBefore = calls.filter((c) => c.path.includes('/decompiled')).length;
    await user.keyboard('{Enter}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-name')).toHaveTextContent('parse_args'));
    expect(posts(calls, '/annotations/rename')[0].body).toEqual({ kind: 'function', address: FN, new_name: 'parse_args' });
    expect(within(pane).getByTestId('fn-edit-status')).toHaveTextContent('Renamed main to parse_args (revision 3).');
    await waitFor(() => expect(within(tab).getByTestId(`fn-row-${FN}`)).toHaveTextContent('parse_args'));
    await waitFor(() => expect(calls.filter((c) => c.path.includes('/decompiled')).length).toBeGreaterThan(decBefore));
    expect(await within(pane).findByTestId('fn-dec-code')).toHaveTextContent('int parse_args(int argc, char **argv)');
    expect(within(pane).getByTestId('fn-pane-name-from')).toHaveTextContent('a person');
    await waitFor(() => expect(within(pane).getByTestId('fn-edit-name')).toHaveFocus());
    store.stop();
  });

  it('keeps the editor open with the controller’s message when a save is refused', async () => {
    const user = userEvent.setup();
    const { store, pane } = await open();
    await user.click(within(pane).getByTestId('fn-edit-name'));
    const input = within(pane).getByTestId('fn-name-edit-input');
    await user.clear(input);
    await user.type(input, 'taken{Enter}');
    const box = await within(pane).findByTestId('fn-name-edit-error');
    expect(box).toHaveTextContent("Not saved: rizin did not accept the name (function is still 'main')");
    expect(box).toHaveTextContent('Next: Read the message, fix the value and retry.');
    expect(input).toHaveFocus();
    store.stop();
  });

  it('edits the prototype (prefilled without the semicolon) and renames and retypes variables', async () => {
    const user = userEvent.setup();
    const { store, pane, calls } = await open();
    await user.click(within(pane).getByTestId('fn-edit-proto'));
    const proto = within(pane).getByTestId('fn-proto-edit-input');
    expect(proto).toHaveValue('int main(int argc, char **argv)');
    await user.clear(proto);
    await user.type(proto, 'int run_cli(int argc, char **argv');
    await waitFor(() => expect(within(pane).getByTestId('fn-proto-edit-check')).toHaveTextContent("needs exactly one '(' and one ')'"));
    await user.keyboard('{Enter}');
    expect(within(pane).getByTestId('fn-proto-edit-refused')).toHaveTextContent('Not saved: invalid prototype');
    expect(posts(calls, '/annotations/set-type')).toHaveLength(0);
    await user.type(proto, '){Enter}');
    await waitFor(() => expect(within(pane).getByTestId('fn-pane-sig')).toHaveTextContent('int run_cli(int argc, char **argv);'));
    expect(posts(calls, '/annotations/set-type')[0].body).toEqual({ kind: 'function', address: FN, prototype: 'int run_cli(int argc, char **argv)' });

    const vars = within(pane).getByTestId('fn-vars');
    await user.click(within(vars).getByText(/Variables \(2\)/));
    await user.click(within(vars).getByRole('button', { name: 'Rename variable var_38h' }));
    const vin = within(vars).getByTestId('fn-var-edit-input');
    await user.clear(vin);
    await user.type(vin, 'start_ptr{Enter}');
    await waitFor(() => expect(within(vars).getByTestId('fn-var-start_ptr')).toBeInTheDocument());
    expect(posts(calls, '/annotations/rename')[0].body).toEqual({ kind: 'local', address: FN, variable: 'var_38h', new_name: 'start_ptr' });
    await user.click(within(vars).getByRole('button', { name: 'Change the type of start_ptr' }));
    const tin = within(vars).getByTestId('fn-var-edit-input');
    expect(tin).toHaveValue('int64_t');
    await user.clear(tin);
    await user.type(tin, 'struct nope *{Enter}');
    expect(await within(vars).findByTestId('fn-var-edit-error')).toHaveTextContent('Next: Declare the struct, union or enum first under Types');
    await user.clear(tin);
    await user.type(tin, 'char *{Enter}');
    await waitFor(() => expect(within(vars).getByTestId('fn-var-start_ptr')).toHaveTextContent('char *'));
    expect(within(pane).getByTestId('fn-edit-status')).toHaveTextContent('Set the type of start_ptr to char *');
    expect(await within(pane).findByTestId('fn-dec-code')).toHaveTextContent('char * start_ptr;');
    store.stop();
  });

  it('adds, changes and removes comments from Disassembly rows (by keyboard too) and from the header', async () => {
    const user = userEvent.setup();
    const { store, pane, calls } = await open({}, 'disassembly');
    const table = await within(pane).findByTestId('fn-disasm-table');
    const note = within(table).getByRole('button', { name: `Add a comment at ${FN}` });
    note.focus();
    await user.keyboard(';');                                                // ; on the row's note button opens its editor
    const input = within(table).getByTestId('fn-op-comment-input');
    expect(input).toHaveFocus();
    await user.type(input, 'save the frame pointer{Enter}');
    await waitFor(() => expect(within(pane).getByTestId(`fn-op-${FN}`)).toHaveTextContent('push rbp ; save the frame pointer'));
    expect(posts(calls, '/annotations/comment')[0].body).toEqual({ address: FN, text: 'save the frame pointer' });
    await waitFor(() => expect(within(pane).getByRole('button', { name: `Edit the comment at ${FN}` })).toHaveFocus());

    await user.click(within(pane).getByRole('button', { name: 'Edit the comment at 0x140001191' }));
    expect(within(pane).getByTestId('fn-op-comment-input')).toHaveValue('set up the frame');
    await user.click(within(pane).getByTestId('fn-op-comment-remove'));
    await waitFor(() => expect(within(pane).getByTestId('fn-op-0x140001191')).not.toHaveTextContent('set up the frame'));
    expect(posts(calls, '/annotations/comment')[1].body).toEqual({ address: '0x140001191', text: '' });

    // header: the comment at the function's own address, prefilled
    expect(within(pane).getByTestId('fn-edit-comment')).toHaveTextContent('Edit comment');
    await user.click(within(pane).getByTestId('fn-edit-comment'));
    const head = within(pane).getByTestId('fn-head-comment-input');
    expect(head).toHaveValue('save the frame pointer');
    await user.keyboard('{Escape}');
    expect(within(pane).getByTestId('fn-edit-status')).toHaveTextContent('Not changed.');
    store.stop();
  });

  it('applies a C declaration from the Types dialog and lists what is declared', async () => {
    const user = userEvent.setup();
    const { store, pane, calls } = await open();
    await user.click(within(pane).getByTestId('fn-edit-types'));
    const dialog = await screen.findByRole('dialog', { name: 'Types' });
    expect(await within(dialog).findByTestId('fn-types-none')).toHaveTextContent('None yet.');
    const area = within(dialog).getByTestId('fn-types-edit-input');
    expect(area).toHaveFocus();
    await user.type(area, '#include <x.h>');
    await waitFor(() => expect(within(dialog).getByTestId('fn-types-edit-check')).toHaveTextContent("('#') Example: struct point { int x; int y; };"));
    await user.clear(area);
    await user.type(area, 'struct rs_point {{ int x; int y; };');
    await waitFor(() => expect(within(dialog).getByTestId('fn-types-edit-check')).toHaveTextContent('Valid'));
    await user.keyboard('{Control>}{Enter}{/Control}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(posts(calls, '/annotations/declare')[0].body).toEqual({ declaration: 'struct rs_point { int x; int y; };' });
    expect(within(pane).getByTestId('fn-edit-status')).toHaveTextContent('Declared struct rs_point (revision 3).');
    expect(within(pane).getByTestId('fn-edit-types')).toHaveFocus();
    await user.click(within(pane).getByTestId('fn-edit-types'));
    expect(await within(await screen.findByRole('dialog', { name: 'Types' })).findByTestId('fn-types-list')).toHaveTextContent('struct rs_point');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    store.stop();
  });

  it('shows the History with who made each revision, undoes, rolls back and reviews model proposals', async () => {
    const user = userEvent.setup();
    const { store, pane, calls } = await open({}, 'history');
    const hist = await within(pane).findByTestId('fn-history');
    expect(within(pane).getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
    expect(within(hist).getByTestId('fn-rev-1')).toHaveTextContent('Revision 1 · model-proposed Renamed function 0x140001000 from entry0 to crt_start');
    expect(within(hist).getByTestId('fn-rev-2')).toHaveTextContent('confirmed by a test');
    expect(within(hist).getByTestId('fn-rev-2')).toHaveTextContent('(current)');
    expect(within(hist).queryByTestId('fn-rollback-2')).not.toBeInTheDocument();
    // open proposals: this function's first, Accept / Reject through the R5 review route
    const props = within(hist).getByTestId('fn-proposals');
    expect(within(props).getAllByRole('listitem')).toHaveLength(1);
    expect(props).toHaveTextContent('Function 0x140001190: parse_command_line (was main) model-proposed · qwen2.5-coder:14b · this function');
    await user.click(within(props).getByRole('button', { name: 'Accept Function 0x140001190 parse_command_line' }));
    await waitFor(() => expect(within(pane).getByTestId('fn-proposals-none')).toBeInTheDocument());
    expect(posts(calls, '/annotations/review')[0].body).toEqual({ kind: 'function', key: FN, local: null, decision: 'accept' });
    const h2 = within(pane).getByTestId('fn-history');
    await waitFor(() => expect(within(h2).getByTestId('fn-rev-3')).toHaveTextContent('person Accepted the model\'s function 0x140001190'));
    // Undo last change = roll back to the revision before the newest
    await user.click(within(h2).getByTestId('fn-undo'));
    await waitFor(() => expect(posts(calls, '/annotations/rollback')).toHaveLength(1));
    expect(posts(calls, '/annotations/rollback')[0].body).toEqual({ revision: 2 });
    await waitFor(() => expect(within(pane).getByTestId('fn-rev-4')).toHaveTextContent('Rolled back to revision 2'));
    expect(within(pane).getByTestId('fn-edit-status')).toHaveTextContent('Undid “Accepted the model\'s function 0x140001190” (rolled back to revision 2).');
    // Roll back to revision N, and to before any change
    await user.click(within(pane).getByTestId('fn-rollback-1'));
    await waitFor(() => expect(posts(calls, '/annotations/rollback')[1].body).toEqual({ revision: 1 }));
    await user.click(await within(pane).findByTestId('fn-rollback-0'));
    await waitFor(() => expect(posts(calls, '/annotations/rollback')[2].body).toEqual({ revision: 0 }));
    // Ctrl+Z in the History view undoes too
    await waitFor(() => expect(within(pane).getByTestId('fn-rev-6')).toBeInTheDocument());
    within(pane).getByTestId('fn-undo').focus();
    await user.keyboard('{Control>}z{/Control}');
    await waitFor(() => expect(posts(calls, '/annotations/rollback')[3].body).toEqual({ revision: 5 }));
    store.stop();
  });

  it('explains when the history cannot be loaded, and that there is nothing to undo in a new module', async () => {
    const user = userEvent.setup();
    const empty = { revision: 0, annotations: { functions: {}, locals: {}, globals: {}, comments: {}, types: [] }, proposals: [], open_proposals: 0, history: [] };
    let fail = true;
    const { store, pane, calls } = await open({ '/cases/c1/modules/mod1/annotations': () => (fail ? err(502, 'backend', 'The analysis engine reported an error', 'see Tools > rizin') : empty) }, 'history');
    const alert = await within(pane).findByRole('alert');
    expect(alert).toHaveTextContent('The history could not be loaded');
    expect(alert).toHaveTextContent('Next: see Tools > rizin');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(pane).findByTestId('fn-history-summary')).toHaveTextContent('No changes yet in this module.');
    await user.click(within(pane).getByTestId('fn-undo'));                 // never disabled without a reason: it explains
    expect(within(pane).getByTestId('fn-history-note')).toHaveTextContent('Nothing to undo yet');
    expect(posts(calls, '/annotations/rollback')).toHaveLength(0);
    store.stop();
  });

  it('describes every kind of change in plain words and labels who made it', () => {
    expect(describeChange({ action: 'rename', kind: 'local', function: FN, old: 'var_38h', new: 'start' })).toBe('Renamed variable var_38h to start in function 0x140001190');
    expect(describeChange({ action: 'rename', kind: 'global', addr: '0x1', new: 'g' })).toBe('Named global 0x1 g');
    expect(describeChange({ action: 'set_type', kind: 'function', addr: FN, prototype: 'int f(void)' })).toBe('Set the prototype of function 0x140001190 to int f(void)');
    expect(describeChange({ action: 'comment', addr: FN, text: '' })).toBe('Removed the comment at 0x140001190');
    expect(describeChange({ action: 'apply_struct', names: [{ kind: 'struct', name: 'p' }] })).toBe('Declared struct p');
    expect(describeChange({ action: 'rollback', to_revision: 0 })).toBe('Rolled back to before any change');
    expect([whoLabel('person').text, whoLabel('model-proposed').text, whoLabel('confirmed by test').text]).toEqual(['person', 'model-proposed', 'confirmed by a test']);
  });
});
