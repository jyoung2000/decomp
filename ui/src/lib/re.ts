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
