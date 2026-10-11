import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { Empty, Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { ApiError } from '../../lib/api';
import { isNativeModule, reApi, type FunctionPage, type FunctionRow, type FunctionSort } from '../../lib/re';
import { useApi, useResource } from '../../lib/store';

const SORTS: { id: FunctionSort; label: string }[] = [
  { id: 'addr', label: 'Address' },
  { id: 'size', label: 'Size (largest first)' },
  { id: 'name', label: 'Name' },
];
const PAGE_SIZES = [50, 100, 200];
export const SEARCH_DEBOUNCE_MS = 250;

const n = (v: number) => v.toLocaleString('en-US');
const bytes = (v: number) => `${n(v)} byte${v === 1 ? '' : 's'}`;

/** R6 piece 1: the analysed functions of a native module. Selecting a row only highlights it for now (later pieces add the
 * code views, cross-references and editing). Backed by the same operation as the MCP list_functions tool. */
export function FunctionsTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const modules = useResource(() => api.modules(caseId), [api, caseId]);
  const native = useMemo(() => (modules.data ?? []).filter(isNativeModule), [modules.data]);
  const [moduleId, setModuleId] = useState('');
  useEffect(() => {
    if (native.length && !native.some((m) => m.module_id === moduleId)) setModuleId(native[0].module_id);
  }, [native, moduleId]);

  if (modules.error) return <ErrorCallout error={modules.error} title="Modules could not be loaded" onRetry={modules.reload} />;
  if (!modules.data) return <Loading what="modules" />;
  if (native.length === 0) {
    return (
      <Empty title="No native code in this project" action={<Link className="btn" to={`/projects/${encodeURIComponent(caseId)}/overview`}>Go to Overview</Link>}>
        {modules.data.length === 0
          ? 'The project has not been scanned yet, so there are no modules. Start the project from the header; native programs and libraries appear here once the scan finds them.'
          : 'The function list is for machine code (.exe, .dll, .so, Mach-O). This project’s modules are managed or script code; their recovered source is under Overview and Plan.'}
      </Empty>
    );
  }
  const module = native.find((m) => m.module_id === moduleId) ?? native[0];
  return (
    <section className="stack" aria-labelledby="fn-h" data-testid="functions-tab">
      <div className="row fn-head">
        <h2 id="fn-h" className="section-title">
          Functions
        </h2>
        {native.length > 1 ? (
          <label className="small">
            Module{' '}
            <select value={module.module_id} onChange={(e) => setModuleId(e.target.value)} aria-label="Module" data-testid="fn-module">
              {native.map((m) => (
                <option key={m.module_id} value={m.module_id}>
                  {m.rel_path || m.module_id}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span className="small muted" data-testid="fn-module-name">
            Module: <span className="mono">{module.rel_path}</span>
          </span>
        )}
      </div>
      <FunctionList key={module.module_id} caseId={caseId} moduleId={module.module_id} moduleName={module.rel_path} />
    </section>
  );
}

function FunctionList({ caseId, moduleId, moduleName }: { caseId: string; moduleId: string; moduleName: string }) {
  const api = useApi();
  const re = useMemo(() => reApi(api), [api]);
  const [search, setSearch] = useState('');
  const [contains, setContains] = useState('');
  const [sort, setSort] = useState<FunctionSort>('addr');
  const [limit, setLimit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [analyze, setAnalyze] = useState(false);
  const [page, setPage] = useState<FunctionPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [active, setActive] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [announce, setAnnounce] = useState('');
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([]);
  const focusRow = useRef(false);

  // debounced search; any change of the query goes back to the first page
  useEffect(() => {
    const t = setTimeout(() => {
      setContains(search.trim());
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    re.functions(caseId, moduleId, { contains: contains || undefined, sort, offset, limit, analyze }).then(
      (p) => {
        if (!alive) return;
        setPage(p);
        setError(null);
        setLoading(false);
        setActive(0);
      },
      (e) => {
        if (!alive) return;
        setError(e);
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
  }, [re, caseId, moduleId, contains, sort, offset, limit, analyze, tick]);

  const rows = page?.functions ?? [];
  useEffect(() => {
    if (focusRow.current) rowRefs.current[active]?.focus();
    focusRow.current = false;
  }, [active, rows]);

  const select = useCallback((f: FunctionRow) => {
    setSelected(f.addr);
    setAnnounce(`Selected ${f.name} at ${f.addr}, ${bytes(f.size)}.`);
  }, []);

  const onKey = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    if (!rows.length) return;
    let next = active;
    switch (e.key) {
      case 'ArrowDown': next = Math.min(rows.length - 1, active + 1); break;
      case 'ArrowUp': next = Math.max(0, active - 1); break;
      case 'Home': next = 0; break;
      case 'End': next = rows.length - 1; break;
      case 'PageDown': next = Math.min(rows.length - 1, active + 10); break;
      case 'PageUp': next = Math.max(0, active - 10); break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        select(rows[active]);
        return;
      default:
        return;
    }
    e.preventDefault();
    focusRow.current = true;
    setActive(next);
  };

  const code = error instanceof ApiError ? error.code : null;
  const total = page?.total ?? 0;
  const first = total ? offset + 1 : 0;
  const last = offset + rows.length;
  const pages = Math.max(1, Math.ceil(total / limit));
  const pageNo = Math.floor(offset / limit) + 1;
  const atStart = offset === 0;
  const atEnd = !page?.more;

  return (
    <>
      <div className="filters" role="search" aria-label="Find functions">
        <label className="field">
          <span className="small">Search by name</span>
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="e.g. main, fcn.1400, Parse" maxLength={120} data-testid="fn-search" />
        </label>
        <label className="field">
          <span className="small">Sort by</span>
          <select value={sort} onChange={(e) => { setSort(e.target.value as FunctionSort); setOffset(0); }} data-testid="fn-sort">
            {SORTS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="small">Rows per page</span>
          <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }} data-testid="fn-limit">
            {PAGE_SIZES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && code === 'not_analyzed' ? (
        <div className="callout warn" role="alert" data-testid="fn-not-analyzed">
          <div className="ttl">Not analysed yet</div>
          <div>{(error as ApiError).message}</div>
          <div>
            <strong>Next: </strong>
            {(error as ApiError).nextAction}
          </div>
          <div>
            <button type="button" className="btn primary sm" onClick={() => { setAnalyze(true); setTick((t) => t + 1); }} disabled={loading} data-tooltip={loading ? 'Analysis is running' : undefined} data-testid="fn-analyze">
              {loading ? 'Analysing…' : 'Analyse now'}
            </button>
          </div>
        </div>
      ) : error ? (
        <>
          <ErrorCallout error={error} title="Functions could not be listed" onRetry={() => setTick((t) => t + 1)} />
          {code === 'engine_missing' && (
            <p className="small">
              <Link to="/tools" data-testid="fn-open-tools">Open Tools to install rizin</Link>
            </p>
          )}
        </>
      ) : !page ? (
        <Loading what={`functions of ${moduleName}${analyze ? ' (analysing; this can take a minute)' : ''}`} />
      ) : (
        <>
          <div className="row small muted fn-summary" aria-live="polite" data-testid="fn-summary">
            {total === 0
              ? contains
                ? `No function name contains “${contains}”.`
                : 'The analysis found no functions in this module.'
              : `Showing ${n(first)}–${n(last)} of ${n(total)} function${total === 1 ? '' : 's'}${contains ? ` matching “${contains}”` : ''}.`}
            {loading && <span> Updating…</span>}
            {page.truncated && <span> The list is capped; narrow it with the search box.</span>}
          </div>
          {total === 0 ? (
            contains ? (
              <Empty title="No matches" action={<button type="button" className="btn sm" onClick={() => setSearch('')}>Clear the search</button>}>
                Try part of a name, such as “main” or an address fragment like “1400”.
              </Empty>
            ) : (
              <Empty title="No functions found">The analysis engine found no functions. The file may be packed or data only; see Overview for what the analysis reported.</Empty>
            )
          ) : (
            <div className="table-wrap">
              <table className="table fn-table" role="grid" aria-label={`Functions in ${moduleName}`} aria-rowcount={total + 1} aria-describedby="fn-keys" data-testid="fn-table">
                <thead>
                  <tr>
                    <th scope="col">Address</th>
                    <th scope="col">Name</th>
                    <th scope="col" className="num">
                      Size
                    </th>
                    <th scope="col" className="num col-blocks">
                      Blocks
                    </th>
                    <th scope="col" className="col-sig">
                      Signature
                    </th>
                  </tr>
                </thead>
                <tbody onKeyDown={onKey}>
                  {rows.map((f, i) => {
                    const isSel = selected === f.addr;
                    return (
                      <tr
                        key={f.addr}
                        ref={(el) => {
                          rowRefs.current[i] = el;
                        }}
                        aria-rowindex={offset + i + 2}
                        aria-selected={isSel}
                        tabIndex={i === active ? 0 : -1}
                        className={isSel ? 'selected' : undefined}
                        onClick={() => {
                          setActive(i);
                          select(f);
                        }}
                        onFocus={() => setActive(i)}
                        data-testid={`fn-row-${f.addr}`}
                      >
                        <td className="mono">{f.addr}</td>
                        <td className="wrap-any">
                          <span className="mono">{f.name}</span>
                          {f.signature && <div className="xs muted mono fn-sig-inline">{f.signature}</div>}
                        </td>
                        <td className="num">
                          {f.span !== undefined ? (
                            <span title={`${bytes(f.size)} of code spread over an address range of ${bytes(f.span)}`}>
                              {n(f.size)}
                              <span className="xs muted fn-span" data-testid={`fn-span-${f.addr}`}>
                                span {n(f.span)}
                              </span>
                            </span>
                          ) : (
                            n(f.size)
                          )}
                        </td>
                        <td className="num col-blocks">{f.nbbs ?? '—'}</td>
                        <td className="mono small wrap-any col-sig">{f.signature ?? '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p id="fn-keys" className="xs muted">
            Click a row, or use the arrow keys (Home, End, Page Up, Page Down) and press Enter or Space to select a function. Size is code bytes; when the
            function’s code is spread out, the address span is shown under it.
          </p>
          {total > 0 && (
            <nav className="row fn-pager" aria-label="Function pages">
              <button type="button" className="btn sm" onClick={() => setOffset(Math.max(0, offset - limit))} disabled={atStart || loading} data-tooltip={atStart ? 'Already on the first page' : loading ? 'Loading' : undefined} data-testid="fn-prev">
                ‹ Previous
              </button>
              <span className="small" data-testid="fn-page">
                Page {n(pageNo)} of {n(pages)}
              </span>
              <button type="button" className="btn sm" onClick={() => setOffset(offset + limit)} disabled={atEnd || loading} data-tooltip={atEnd ? 'Already on the last page' : loading ? 'Loading' : undefined} data-testid="fn-next">
                Next ›
              </button>
            </nav>
          )}
        </>
      )}
      <div className="sr-only" role="status" aria-live="polite" data-testid="fn-announce">
        {announce}
      </div>
    </>
  );
}
