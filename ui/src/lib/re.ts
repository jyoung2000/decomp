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
