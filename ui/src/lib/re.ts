// R6 RE workbench in the app. Piece 1: the Functions list of a native module, from GET /cases/{c}/modules/{m}/functions — the
// same workbench operation and shaping helper as the MCP list_functions tool. Kept in its own module like aiQuality.ts.
import type { Api } from './api';
import type { Module } from './types';
import type { Proposal } from './aiQuality';

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
  /** this function's annotations (piece 5 shows who made them) */
  annotations?: { function?: { name?: string; prototype?: string; prov?: { origin?: string; status?: string } } | null; comments?: { addr: string; text: string }[] };
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
  /** set when the comment is a model's: model-proposed / accepted by you / confirmed by a test */
  comment_status?: string;
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

// ---- piece 4: module-level lists (strings, imports, exports, sections + entry points) and "used by" of one string or import.
// Same workbench calls and shaping as the MCP strings / imports / exports / sections / entry_points tools (re_workbench.shape_*),
// and "used by" = MCP xrefs_to with scope=address. All text is untrusted program data: rendered as plain text only.
export type ModuleList = 'functions' | 'strings' | 'imports' | 'exports' | 'sections' | 'search';
export const MODULE_LISTS: { id: ModuleList; label: string }[] = [
  { id: 'functions', label: 'Functions' },
  { id: 'strings', label: 'Strings' },
  { id: 'imports', label: 'Imports' },
  { id: 'exports', label: 'Exports' },
  { id: 'sections', label: 'Sections' },
  { id: 'search', label: 'Search' },
];

export interface StringRow {
  addr: string;
  vaddr?: number;
  paddr?: number;
  /** characters */
  length: number;
  section?: string;
  /** encoding as reported by rizin: ascii, utf8, utf16le, utf32le, … */
  type?: string;
  /** the text, clipped by the controller (1,000 characters) */
  string: string;
}

export interface StringPage {
  strings: StringRow[];
  total: number;
  offset: number;
  returned: number;
  more: boolean;
  scanned?: number;
  count_truncated?: boolean;
  sections: { section: string; count: number }[];
  truncated?: boolean;
  untrusted: true;
}

export interface StringQuery {
  contains?: string;
  regex?: string;
  minLength?: number;
  section?: string;
  offset?: number;
  limit?: number;
}

export interface ImportRow {
  addr?: string;
  name: string;
  libname?: string;
  type?: string;
  ordinal?: number;
  bind?: string;
  plt?: number;
}

export interface ExportRow {
  addr?: string;
  name: string;
  realname?: string;
  flagname?: string;
  type?: string;
  vaddr?: number;
  size?: number;
}

interface TablePageBase {
  total: number;
  offset: number;
  returned: number;
  more: boolean;
  truncated?: boolean;
  untrusted: true;
}
export interface ImportPage extends TablePageBase {
  imports: ImportRow[];
}
export interface ExportPage extends TablePageBase {
  exports: ExportRow[];
}

export interface SectionRow {
  name: string;
  vaddr?: string;
  paddr?: string;
  /** bytes in the file */
  size?: number;
  /** bytes in memory */
  vsize?: number;
  /** rizin permission string, e.g. -r-x */
  perm?: string;
  flags?: string[];
  /** Shannon entropy 0..8 from the packer report, when it has this section */
  entropy?: number;
  high_entropy?: boolean;
}

export interface EntryPoint {
  addr: string;
  type?: string;
  vaddr?: number;
}

export interface SectionsInfo {
  sections: SectionRow[];
  segments: SectionRow[];
  entrypoints: EntryPoint[];
  entropy_source?: string | null;
  untrusted: true;
}

export interface UsedBy {
  addr: string;
  refs: XrefRow[];
  total: number;
  more: boolean;
  limit: number;
  /** the distinct containing functions of the references shown */
  functions: { addr: string; name?: string | null }[];
  untrusted: true;
}

export const STRING_PAGE_SIZES = [50, 100, 200, 500];
export const TABLE_PAGE_SIZES = [50, 100, 200];
export const STRING_MIN_LENGTHS = [1, 4, 6, 8, 12, 20];

export const reListApi = (api: Api) => {
  const base = (c: string, m: string) => `/cases/${enc(c)}/modules/${enc(m)}`;
  return {
    strings: (caseId: string, moduleId: string, q: StringQuery = {}) => {
      const p = new URLSearchParams();
      if (q.contains) p.set('contains', q.contains);
      if (q.regex) p.set('regex', q.regex);
      p.set('min_length', String(q.minLength ?? 4));
      if (q.section) p.set('section', q.section);
      p.set('offset', String(q.offset ?? 0));
      p.set('limit', String(q.limit ?? 100));
      return api.get<StringPage>(`${base(caseId, moduleId)}/strings?${p.toString()}`);
    },
    table: <T extends 'imports' | 'exports'>(kind: T, caseId: string, moduleId: string, q: { contains?: string; offset?: number; limit?: number } = {}) => {
      const p = new URLSearchParams();
      if (q.contains) p.set('contains', q.contains);
      p.set('offset', String(q.offset ?? 0));
      p.set('limit', String(q.limit ?? 100));
      return api.get<T extends 'imports' ? ImportPage : ExportPage>(`${base(caseId, moduleId)}/${kind}?${p.toString()}`);
    },
    sections: (caseId: string, moduleId: string) => api.get<SectionsInfo>(`${base(caseId, moduleId)}/sections`),
    usedBy: (caseId: string, moduleId: string, addr: string, limit = XREF_LIMIT, analyze = false) =>
      api.get<UsedBy>(`${base(caseId, moduleId)}/used-by/${enc(addr)}?limit=${limit}${analyze ? '&analyze=true' : ''}`),
  };
};

// ---- piece 6: search the module for a byte pattern (hex, ?? = any byte) or a text pattern (a regex over the extracted strings).
// GET .../pattern-search runs ReWorkbench.search, the call behind the MCP search_bytes tool (same rules, refusals and rows).
export type SearchKind = 'hex' | 'text';

export interface SearchMatch {
  /** virtual address; null when the bytes are outside every mapped section (e.g. the file header) */
  addr: string | null;
  /** file offset */
  paddr: string | null;
  section?: string | null;
  /** containing function, once the module has been analysed */
  fcn_addr: string | null;
  fcn_name: string | null;
  /** raw bytes as hex ("4d 5a 90 00"), never decoded */
  before?: string;
  bytes?: string;
  after?: string;
  /** set when the hit is longer than the bytes shown */
  bytes_clipped?: number;
  /** text hits: the string (untrusted program text), its encoding and length in characters */
  string?: string;
  encoding?: string;
  length?: number;
}

export interface SearchPage {
  kind: SearchKind;
  /** the pattern as searched (hex in canonical form) */
  pattern: string;
  matches: SearchMatch[];
  /** hits listed (at most max_hits) */
  listed: number;
  offset: number;
  returned: number;
  more: boolean;
  /** hits found: exact for text; for hex the listed hits (found_exact false when capped) */
  found: number;
  found_exact: boolean;
  /** more hits exist than the max_hits listed */
  capped: boolean;
  max_hits: number;
  functions_known: boolean;
  scanned?: number | null;
  truncated?: boolean;
  untrusted: true;
}

export const SEARCH_PAGE_SIZES = [50, 100, 200];
export const BYTE_PATTERN_EXAMPLE = '48 8b ?? 24';
export const TEXT_PATTERN_EXAMPLE = '[Uu]sage';

export const reSearchApi = (api: Api) => ({
  search: (caseId: string, moduleId: string, q: { kind: SearchKind; pattern: string; offset?: number; limit?: number; analyze?: boolean }) => {
    const p = new URLSearchParams();
    p.set('kind', q.kind);
    p.set('pattern', q.pattern);
    p.set('offset', String(q.offset ?? 0));
    p.set('limit', String(q.limit ?? 100));
    if (q.analyze) p.set('analyze', 'true');
    return api.get<SearchPage>(`/cases/${enc(caseId)}/modules/${enc(moduleId)}/pattern-search?${p.toString()}`);
  },
});

/** What is wrong with a byte pattern, in the controller's words (re_workbench.check_byte_pattern), or null when it can be used.
 * The controller checks again; this only lets the form say it while typing. */
export function checkBytePattern(pattern: string): string | null {
  if (!pattern.trim()) return 'Type the bytes to find.';
  if (pattern.length > 800) return 'It is longer than 800 characters.';
  if (/0[xX]/.test(pattern)) return 'Remove the 0x prefixes (write 48 8b, not 0x48 0x8b).';
  const bad = /[^0-9a-fA-F?\s]/.exec(pattern);
  if (bad) return `'${bad[0]}' is not a hex digit (0-9, a-f) or a ?? wildcard.`;
  const p = pattern.replace(/\s+/g, '');
  if (p.length % 2) return `It has an odd number of characters (${p.length}); every byte needs two hex digits (write 0f, not f).`;
  const pairs = p.match(/../g) ?? [];
  const half = pairs.find((x) => x.includes('?') && x !== '??');
  if (half) return `'${half}' mixes a digit and '?'; a wildcard is a whole byte (??).`;
  if (pairs.every((x) => x === '??')) return 'It has only wildcards; give at least one fixed byte.';
  if (pairs.length > 256) return `It is ${pairs.length} bytes long; at most 256 bytes.`;
  return null;
}

/** What is wrong with a text pattern (a regex unless `plain`), or null. Mirrors ReWorkbench.compile_regex's limits. */
export function checkTextPattern(pattern: string, plain: boolean): string | null {
  if (!pattern) return 'Type the text to find.';
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(pattern)) return 'It contains control characters.';
  const rx = plain ? escapeRegex(pattern) : pattern;
  if (rx.length > 200) return plain ? 'It is too long (at most about 200 characters).' : 'A regular expression can have at most 200 characters.';
  if (plain) return null;
  if ((/\)[*+?{]/.test(rx) && /[*+}]\)/.test(rx)) || /\\[1-9]/.test(rx)) return 'Nested quantifiers and backreferences are not allowed in string regexes.';
  try {
    new RegExp(rx);
  } catch {
    return 'It is not a valid regular expression; turn off “Regular expression” to search for the text as typed.';
  }
  return null;
}

/** Plain text as a regex that matches it literally (Python re syntax for these characters is the same). */
export function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, (c) => `\\${c}`);
}

/** The pattern sent for a text search: the regex as typed, or plain text escaped. */
export const textPatternFor = (pattern: string, plain: boolean) => (plain ? escapeRegex(pattern) : pattern);

/** rizin's permission string (-r-x, -rw-) as words. */
export function permWords(perm?: string): string {
  if (!perm) return 'unknown';
  const w = [perm.includes('r') && 'read', perm.includes('w') && 'write', perm.includes('x') && 'execute'].filter(Boolean);
  return w.length ? w.join(', ') : 'none';
}

// ---- piece 5: edits (a person's annotations). POST .../annotations/{rename,set-type,comment,declare} go through the same
// workbench path and value grammar as the MCP rename / set_type / add_comment / apply_struct tools (which store model proposals);
// .../annotations/check runs that grammar without storing anything (validate as you type). History, review (accept / reject) and
// roll back are the R5 routes (GET .../annotations, POST .../annotations/review and .../rollback).
export type CheckWhat = 'name' | 'type' | 'prototype' | 'comment' | 'declaration';

export interface CheckResult {
  ok: boolean;
  what: CheckWhat;
  /** the value as it will be stored (whitespace normalised), when ok */
  normalized?: string;
  /** what is wrong, when not ok */
  message?: string;
  /** one valid value of this kind */
  example: string;
}

export interface EditResult {
  revision: number;
  change: AnnotationChange;
  author: 'user';
  who: 'person';
}

export interface AnnotationChange {
  action?: string;
  kind?: string;
  addr?: string;
  function?: string;
  old?: string | null;
  new?: string;
  original?: string;
  prototype?: string;
  variable?: string;
  type?: string;
  text?: string;
  names?: { kind: string; name: string }[];
  decision?: string;
  key?: string;
  local?: string | null;
  value?: string | null;
  to_revision?: number;
  from_revision?: number;
  author?: string;
  label?: string;
  at?: string;
}

export interface AnnotationRevision {
  evidence_id: string;
  revision: number;
  change?: AnnotationChange | null;
  /** person | model-proposed | confirmed by test */
  who?: string;
}

export interface AnnotationState {
  functions: Record<string, { name?: string; prototype?: string }>;
  locals: Record<string, Record<string, { name?: string; type?: string }>>;
  globals: Record<string, { name: string }>;
  comments: Record<string, { text: string }>;
  types: { decl: string; names: { kind: string; name: string }[]; sha256: string }[];
}

export interface AnnotationsHistory {
  revision: number;
  annotations: AnnotationState;
  proposals: Proposal[];
  open_proposals: number;
  history?: AnnotationRevision[];
}

export const HISTORY_LIMIT = 50;
export const CHECK_DEBOUNCE_MS = 150;

export const reEditApi = (api: Api) => {
  const base = (c: string, m: string) => `/cases/${enc(c)}/modules/${enc(m)}/annotations`;
  return {
    check: (caseId: string, moduleId: string, what: CheckWhat, value: string) => api.post<CheckResult>(`${base(caseId, moduleId)}/check`, { what, value }),
    rename: (caseId: string, moduleId: string, b: { kind: 'function' | 'global' | 'local'; address?: string; variable?: string; new_name: string }) =>
      api.post<EditResult>(`${base(caseId, moduleId)}/rename`, b),
    setType: (caseId: string, moduleId: string, b: { kind: 'function' | 'local'; address: string; prototype?: string; variable?: string; type?: string }) =>
      api.post<EditResult>(`${base(caseId, moduleId)}/set-type`, b),
    comment: (caseId: string, moduleId: string, address: string, text: string) => api.post<EditResult>(`${base(caseId, moduleId)}/comment`, { address, text }),
    declare: (caseId: string, moduleId: string, declaration: string) => api.post<EditResult>(`${base(caseId, moduleId)}/declare`, { declaration }),
    history: (caseId: string, moduleId: string) => api.get<AnnotationsHistory>(`${base(caseId, moduleId)}?history=${HISTORY_LIMIT}`),
    review: (caseId: string, moduleId: string, p: Pick<Proposal, 'kind' | 'key' | 'local'>, decision: 'accept' | 'reject') =>
      api.post<{ revision: number }>(`${base(caseId, moduleId)}/review`, { kind: p.kind, key: p.key, local: p.local ?? null, decision }),
    rollback: (caseId: string, moduleId: string, revision: number) => api.post<{ revision: number }>(`${base(caseId, moduleId)}/rollback`, { revision }),
  };
};

/** Who made a revision, in plain words. */
export function whoLabel(who?: string): { text: string; tone: 'ok' | 'warn' | 'outline' } {
  if (who === 'model-proposed') return { text: 'model-proposed', tone: 'warn' };
  if (who === 'confirmed by test') return { text: 'confirmed by a test', tone: 'ok' };
  return { text: 'person', tone: 'outline' };
}

/** One plain sentence for a stored change ("Renamed function 0x401000 from fcn.00401000 to parse_args"). */
export function describeChange(c?: AnnotationChange | null): string {
  if (!c) return 'Change';
  switch (c.action) {
    case 'rename':
      if (c.kind === 'local') return `Renamed variable ${c.old} to ${c.new} in function ${c.function}`;
      if (c.kind === 'global') return c.old ? `Renamed global ${c.addr} from ${c.old} to ${c.new}` : `Named global ${c.addr} ${c.new}`;
      return c.old ? `Renamed function ${c.addr} from ${c.old} to ${c.new}` : `Renamed function ${c.addr} to ${c.new}`;
    case 'set_type':
      if (c.kind === 'local') return `Set the type of ${c.variable} in function ${c.function} to ${c.type}`;
      return `Set the prototype of function ${c.addr} to ${c.prototype}`;
    case 'comment':
      return c.text ? `Comment at ${c.addr}: ${c.text}` : `Removed the comment at ${c.addr}`;
    case 'apply_struct':
      return `Declared ${(c.names ?? []).map((n) => `${n.kind} ${n.name}`).join(', ') || 'C types'}`;
    case 'review': {
      const verb = c.decision === 'accept' ? 'Accepted' : c.decision === 'reject' ? 'Rejected' : 'Confirmed';
      return `${verb} the model's ${c.kind} ${c.kind === 'local' ? `${c.local} in ${c.key}` : c.key}${c.value ? `: ${c.value}` : ''}`;
    }
    case 'rollback':
      return c.to_revision === 0 ? 'Rolled back to before any change' : `Rolled back to revision ${c.to_revision}`;
    default:
      return c.action ?? 'Change';
  }
}
