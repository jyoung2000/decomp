// R6 RE workbench in the app. Piece 1: the Functions list of a native module, from GET /cases/{c}/modules/{m}/functions — the
// same workbench operation and shaping helper as the MCP list_functions tool. Kept in its own module like aiQuality.ts.
import type { Api } from './api';
import type { Module } from './types';

export type FunctionSort = 'addr' | 'size' | 'name';

export interface FunctionRow {
  addr: string;
  name: string;
  /** code bytes (sum of basic blocks) */
  size: number;
  /** address span (last byte - first byte), only when it differs from size */
  span?: number;
  nbbs?: number;
  cc?: number;
  calltype?: string;
  signature?: string;
  noreturn?: boolean;
}

export interface FunctionPage {
  functions: FunctionRow[];
  total: number;
  offset: number;
  returned: number;
  more: boolean;
  session_id: string;
  sort: FunctionSort;
  contains: string;
  truncated?: boolean;
  cached?: boolean;
}

export interface FunctionQuery {
  contains?: string;
  sort?: FunctionSort;
  offset?: number;
  limit?: number;
  analyze?: boolean;
}

const enc = encodeURIComponent;
export const NATIVE_PROFILE = /^native_(pe|elf|macho|raw)$/;
export const isNativeModule = (m: Pick<Module, 'profile'>) => NATIVE_PROFILE.test(m.profile ?? '');

export const reApi = (api: Api) => ({
  functions: (caseId: string, moduleId: string, q: FunctionQuery = {}) => {
    const p = new URLSearchParams();
    if (q.contains) p.set('contains', q.contains);
    p.set('sort', q.sort ?? 'addr');
    p.set('offset', String(q.offset ?? 0));
    p.set('limit', String(q.limit ?? 100));
    if (q.analyze) p.set('analyze', 'true');
    return api.get<FunctionPage>(`/cases/${enc(caseId)}/modules/${enc(moduleId)}/functions?${p.toString()}`);
  },
});

// ---- piece 2: the detail pane of one function. Same workbench calls as the MCP get_function / decompile / disassemble tools.
// Every string below comes from the analysed program: untrusted, rendered as plain text only (never as HTML).
export type Decompiler = 'rizin' | 'ghidra';

export interface FunctionHeader {
  addr: string;
  name: string;
  /** code bytes (sum of basic blocks), as in the list */
  size: number;
  span?: number;
  range?: { start: string; end: string };
  signature?: string;
  cc?: number;
  calltype?: string;
  nbbs?: number;
  noreturn?: boolean;
  bits?: number;
  stackframe?: number;
  n_callrefs?: number;
  callees?: number;
}

export interface DecompilerInfo {
  id: Decompiler;
  available: boolean;
  real_decompiler: boolean;
  label: string;
  reason?: string | null;
}

export interface FunctionVariable {
  name: string;
  type?: string;
  is_arg: boolean;
  storage?: string;
}

export interface FunctionDetail {
  header: FunctionHeader;
  addr: string;
  blocks: { addr: string; size?: number; ninstr?: number }[];
  variables: FunctionVariable[];
  decompilers: DecompilerInfo[];
  session_id: string;
  untrusted: true;
}

export interface Decompiled {
  function: string;
  addr: string;
  /** engine label from the backend, e.g. rz-ghidra(pdg), pdc(pseudo), ghidra-11.4 */
  decompiler: string;
  engine: Decompiler;
  requested: Decompiler;
  is_real_decompiler: boolean;
  decompiled: { text: string; truncated?: boolean; total_bytes?: number };
  truncated?: boolean;
  annotations_note?: string;
  untrusted: true;
}

export interface DisasmOp {
  addr: string;
  offset?: number;
  size?: number;
  bytes?: string;
  disasm?: string;
  type?: string;
  comment?: string;
  user_comment?: string;
}

export interface DisasmPage {
  function: string;
  addr: string;
  ops: DisasmOp[];
  total: number;
  offset: number;
  returned: number;
  more: boolean;
  truncated?: boolean;
  untrusted: true;
}

export const DISASM_PAGE = 500;

export const reFunctionApi = (api: Api) => {
  const base = (c: string, m: string, a: string) => `/cases/${enc(c)}/modules/${enc(m)}/functions/${enc(a)}`;
  return {
    detail: (caseId: string, moduleId: string, addr: string) => api.get<FunctionDetail>(base(caseId, moduleId, addr)),
    decompiled: (caseId: string, moduleId: string, addr: string, decompiler: Decompiler = 'rizin') =>
      api.get<Decompiled>(`${base(caseId, moduleId, addr)}/decompiled?decompiler=${decompiler}`),
    disassembly: (caseId: string, moduleId: string, addr: string, offset = 0, limit = DISASM_PAGE) =>
      api.get<DisasmPage>(`${base(caseId, moduleId, addr)}/disassembly?offset=${offset}&limit=${limit}`),
  };
};

// ---- piece 3: references and the bounded call graph of one function. Same workbench calls and shaping as the MCP xrefs_to /
// xrefs_from tools with scope=function (re_workbench.shape_xrefs) and call_graph. Names and string text are untrusted program data.
export type RefKind = 'call' | 'jump' | 'data' | 'string';

export interface XrefRow {
  /** the referencing instruction (or data) address */
  from: string;
  /** the referenced address */
  to: string;
  type?: string;
  kind: RefKind;
  /** "Called from": the function containing `from` */
  fcn_addr?: string | null;
  fcn_name?: string | null;
  /** "Calls / uses": the function containing `to` */
  to_fcn_addr?: string | null;
  to_fcn_name?: string | null;
  /** import name of a data target (library!name) */
  name?: string;
  /** text of a string target (clipped by the controller) */
  string?: string;
}

export interface XrefDirection {
  addr: string;
  direction: 'to' | 'from';
  refs: XrefRow[];
  total: number;
  more: boolean;
}

export interface FunctionXrefs {
  addr: string;
  function?: string | null;
  /** jumps that stay inside the function (not listed; they are control flow) */
  internal_jumps: number;
  to: XrefDirection;
  from: XrefDirection;
  limit: number;
  truncated?: boolean;
  untrusted: true;
}

export type GraphDirection = 'callees' | 'callers' | 'both';

export interface CallGraphNode {
  addr: string;
  name?: string | null;
  depth: number;
}

export interface CallGraph {
  root: string;
  direction: GraphDirection;
  depth: number;
  max_nodes: number;
  nodes: CallGraphNode[];
  /** caller -> callee */
  edges: { from: string; to: string }[];
  node_limit_hit: boolean;
  truncated?: boolean;
  untrusted: true;
}

export const XREF_LIMIT = 200;
export const XREF_LIMIT_MAX = 1000;
export const GRAPH_NODE_CAPS = [50, 100, 200, 500];

export const reRefsApi = (api: Api) => {
  const base = (c: string, m: string, a: string) => `/cases/${enc(c)}/modules/${enc(m)}/functions/${enc(a)}`;
  return {
    xrefs: (caseId: string, moduleId: string, addr: string, limit = XREF_LIMIT) =>
      api.get<FunctionXrefs>(`${base(caseId, moduleId, addr)}/xrefs?limit=${limit}`),
    callGraph: (caseId: string, moduleId: string, addr: string, q: { direction: GraphDirection; depth: number; maxNodes: number }) =>
      api.get<CallGraph>(`${base(caseId, moduleId, addr)}/callgraph?direction=${q.direction}&depth=${q.depth}&max_nodes=${q.maxNodes}`),
  };
};

/** One item of the call-graph tree. A function is expanded once, at its shallowest place; later places are leaves marked
 * `repeat: 'cycle'` (it is one of its own ancestors: the calls loop back) or `repeat: 'seen'` (expanded elsewhere). This keeps
 * the tree no larger than the graph's nodes + edges, even with cycles and diamonds. With direction 'both' an item's children
 * are what it calls and what calls it, each labelled by `relation` (the edge it came through is not repeated backwards). */
export interface CallTreeNode {
  id: string;
  addr: string;
  name: string | null;
  /** how this item relates to its parent: the parent calls it, or it calls the parent */
  relation?: 'calls' | 'called-by';
  repeat?: 'cycle' | 'seen';
  /** at the requested depth: its own calls/callers were not followed */
  frontier?: boolean;
  children: CallTreeNode[];
}

export function buildCallTree(g: CallGraph): CallTreeNode {
  const names = new Map(g.nodes.map((n) => [n.addr, n.name ?? null]));
  const depthOf = new Map(g.nodes.map((n) => [n.addr, n.depth]));
  const callees = new Map<string, string[]>();
  const callers = new Map<string, string[]>();
  for (const e of g.edges) {
    callees.set(e.from, [...(callees.get(e.from) ?? []), e.to]);
    callers.set(e.to, [...(callers.get(e.to) ?? []), e.from]);
  }
  const root: CallTreeNode = { id: g.root, addr: g.root, name: names.get(g.root) ?? null, children: [] };
  const expanded = new Set([g.root]);
  const queue: { node: CallTreeNode; ancestors: Set<string>; parent?: string }[] = [{ node: root, ancestors: new Set([g.root]) }];
  while (queue.length) {
    const { node, ancestors, parent } = queue.shift()!;
    const links: { addr: string; relation: 'calls' | 'called-by' }[] = [];
    if (g.direction !== 'callers')
      for (const a of callees.get(node.addr) ?? []) if (!(node.relation === 'called-by' && a === parent)) links.push({ addr: a, relation: 'calls' });
    if (g.direction !== 'callees')
      for (const a of callers.get(node.addr) ?? []) if (!(node.relation === 'calls' && a === parent)) links.push({ addr: a, relation: 'called-by' });
    for (const { addr, relation } of links) {
      const child: CallTreeNode = { id: `${node.id}>${relation === 'calls' ? '' : '<'}${addr}`, addr, name: names.get(addr) ?? null, relation, children: [] };
      if (ancestors.has(addr)) child.repeat = 'cycle';
      else if (expanded.has(addr)) child.repeat = 'seen';
      else {
        expanded.add(addr);
        queue.push({ node: child, ancestors: new Set([...ancestors, addr]), parent: node.addr });
      }
      node.children.push(child);
    }
  }
  const mark = (n: CallTreeNode) => {
    if (!n.repeat && !n.children.length && depthOf.get(n.addr) === g.depth) n.frontier = true;
    n.children.forEach(mark);
  };
  root.children.forEach(mark);
  return root;
}
