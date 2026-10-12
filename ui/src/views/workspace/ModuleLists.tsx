import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Empty, Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { ApiError } from '../../lib/api';
import {
  STRING_MIN_LENGTHS,
  STRING_PAGE_SIZES,
  TABLE_PAGE_SIZES,
  XREF_LIMIT,
  XREF_LIMIT_MAX,
  permWords,
  reListApi,
  type ExportRow,
  type ImportRow,
  type ModuleList,
  type SectionRow,
  type SectionsInfo,
  type StringRow,
  type UsedBy,
} from '../../lib/re';
import { useApi } from '../../lib/store';
import { FnLink, type FnNav } from './FunctionRefs';
import { useLoad } from './useLoad';

// R6 piece 4: the module-level Strings, Imports, Exports and Sections views of the Functions area. Loaded on demand (lazy import
// from Functions.tsx) to keep the main bundle under Vite's chunk warning. Every name and string comes from the analysed program:
// it is untrusted and rendered as React text, never as HTML.

const n = (v: number) => v.toLocaleString('en-US');
export const LIST_SEARCH_DEBOUNCE_MS = 250;
/** characters of a string shown before "Show all" */
export const STRING_PREVIEW_CHARS = 120;

/** Control characters made visible (\n, \r, \t, other C0 as \xNN) so one string stays one line; still plain text. */
export function visibleText(s: string): string {
  return s.replace(/[\u0000-\u001f\u007f]/g, (c) => (c === '\n' ? '\\n' : c === '\r' ? '\\r' : c === '\t' ? '\\t' : `\\x${c.charCodeAt(0).toString(16).padStart(2, '0')}`));
}

const ENCODINGS: Record<string, string> = { ascii: 'ASCII', utf8: 'UTF-8', utf16le: 'UTF-16 LE', utf16be: 'UTF-16 BE', utf32le: 'UTF-32 LE', utf32be: 'UTF-32 BE', ibm037: 'EBCDIC (IBM037)', base64: 'Base64' };
export const encodingLabel = (t?: string) => (t ? ENCODINGS[t.toLowerCase()] ?? t : 'unknown');

/** Program text, truncated with a toggle to show it all. Plain text only. */
export function StringText({ text, limit = STRING_PREVIEW_CHARS, testId }: { text: string; limit?: number; testId?: string }) {
  const [open, setOpen] = useState(false);
  const shown = visibleText(text);
  const long = shown.length > limit;
  return (
    <span className="ml-string" data-testid={testId}>
      <span className="mono ml-string-text">{long && !open ? `${shown.slice(0, limit)}…` : shown}</span>
      {long && (
        <button
          type="button"
          className="btn-link xs ml-more"
          aria-expanded={open}
          onClick={(e) => {
            e.stopPropagation();
            setOpen(!open);
          }}
          onKeyDown={(e) => e.stopPropagation()}
          data-testid={testId ? `${testId}-more` : undefined}
        >
          {open ? 'Show less' : `Show all ${n(shown.length)} characters`}
        </button>
      )}
    </span>
  );
}

// ------------------------------------------------------------------------------------------------ one generic list
interface Column<T> {
  id: string;
  header: string;
  className?: string;
  cell: (row: T) => ReactNode;
}
export interface ListPage<T> {
  rows: T[];
  total: number;
  more: boolean;
  note?: ReactNode;
}

function ItemList<T>({
  prefix,
  what,
  label,
  searchLabel,
  placeholder,
  columns,
  load,
  sourceKey,
  rowKey,
  rowLabel,
  selected,
  onSelect,
  onEscape,
  extraFilters,
  pageSizes,
  emptyAll,
  renderError,
  keysHelp,
}: {
  prefix: string;
  /** plural noun for summaries: "strings" */
  what: string;
  label: string;
  searchLabel: string;
  placeholder: string;
  columns: Column<T>[];
  load: (q: { contains: string; offset: number; limit: number }) => Promise<ListPage<T>>;
  /** changes when the query outside the search box changes (module, filters): back to page 1 */
  sourceKey: string;
  rowKey: (row: T) => string;
  rowLabel: (row: T) => string;
  selected?: string | null;
  onSelect?: (row: T) => void;
  onEscape?: () => void;
  extraFilters?: ReactNode;
  pageSizes: number[];
  emptyAll: ReactNode;
  renderError?: (error: unknown, retry: () => void) => ReactNode | null;
  keysHelp: string;
}) {
  const [search, setSearch] = useState('');
  const [contains, setContains] = useState('');
  const [limit, setLimit] = useState(pageSizes.includes(100) ? 100 : pageSizes[0]);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ListPage<T> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [active, setActive] = useState(0);
  const [announce, setAnnounce] = useState('');
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([]);
  const focusRow = useRef(false);
  const loadRef = useRef(load);
  loadRef.current = load;
  const lastSource = useRef(sourceKey);

  useEffect(() => {
    if (search.trim() === contains) return;
    const t = setTimeout(() => {
      setContains(search.trim());
      setOffset(0);
    }, LIST_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [search, contains]);

  useEffect(() => {
    if (lastSource.current !== sourceKey) {
      lastSource.current = sourceKey;
      if (offset !== 0) {
        setOffset(0);
        return;
      }
    }
    let alive = true;
    setLoading(true);
    loadRef.current({ contains, offset, limit }).then(
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
  }, [contains, offset, limit, sourceKey, tick]);

  const rows = page?.rows ?? [];
  useEffect(() => {
    if (focusRow.current) rowRefs.current[active]?.focus();
    focusRow.current = false;
  }, [active, rows]);

  const select = (row: T) => {
    if (!onSelect) return;
    onSelect(row);
    setAnnounce(`Selected ${rowLabel(row)}.`);
  };

  const onKey = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    if (!rows.length || !(e.target instanceof HTMLTableRowElement)) return;   // keys on a link or button inside a row are theirs
    let next = active;
    switch (e.key) {
      case 'ArrowDown': next = Math.min(rows.length - 1, active + 1); break;
      case 'ArrowUp': next = Math.max(0, active - 1); break;
      case 'Home': next = 0; break;
      case 'End': next = rows.length - 1; break;
      case 'PageDown': next = Math.min(rows.length - 1, active + 10); break;
      case 'PageUp': next = Math.max(0, active - 10); break;
      case 'Escape':
        if (!selected || !onEscape) return;
        e.preventDefault();
        onEscape();
        return;
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

  const retry = () => setTick((t) => t + 1);
  const code = error instanceof ApiError ? error.code : null;
  const total = page?.total ?? 0;
  const first = total ? offset + 1 : 0;
  const last = offset + rows.length;
  const pages = Math.max(1, Math.ceil(total / limit));
  const pageNo = Math.floor(offset / limit) + 1;
  const atStart = offset === 0;
  const atEnd = !page?.more;
  const custom = error ? renderError?.(error, retry) : null;

  return (
    <>
      <div className="filters" role="search" aria-label={`Find ${what}`}>
        <label className="field">
          <span className="small">{searchLabel}</span>
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={placeholder} maxLength={120} data-testid={`${prefix}-search`} />
        </label>
        {extraFilters}
        <label className="field">
          <span className="small">Rows per page</span>
          <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }} data-testid={`${prefix}-limit`}>
            {pageSizes.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? (
        custom ?? (
          <>
            <ErrorCallout error={error} title={`The ${what} could not be listed`} onRetry={retry} />
            {code === 'engine_missing' && (
              <p className="small">
                <Link to="/tools" data-testid={`${prefix}-open-tools`}>Open Tools to install rizin</Link>
              </p>
            )}
          </>
        )
      ) : !page ? (
        <Loading what={what} />
      ) : (
        <>
          <div className="row small muted fn-summary" aria-live="polite" data-testid={`${prefix}-summary`}>
            {total === 0
              ? contains
                ? `No ${what} match “${contains}”.`
                : `No ${what} to show.`
              : `Showing ${n(first)}–${n(last)} of ${n(total)} ${what}${contains ? ` matching “${contains}”` : ''}.`}
            {loading && <span> Updating…</span>}
            {page.note && <span> {page.note}</span>}
          </div>
          {total === 0 ? (
            contains ? (
              <Empty title="No matches" action={<button type="button" className="btn sm" onClick={() => setSearch('')} data-testid={`${prefix}-clear`}>Clear the search</button>}>
                Try a shorter part of the text, or change the other filters.
              </Empty>
            ) : (
              emptyAll
            )
          ) : (
            <div className="table-wrap">
              <table className={`table fn-table ml-table ml-${prefix}`} role="grid" aria-label={label} aria-rowcount={total + 1} aria-describedby={`${prefix}-keys`} data-testid={`${prefix}-table`}>
                <thead>
                  <tr>
                    {columns.map((c) => (
                      <th key={c.id} scope="col" className={c.className}>
                        {c.header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody onKeyDown={onKey}>
                  {rows.map((row, i) => {
                    const key = rowKey(row);
                    const isSel = selected === key;
                    return (
                      <tr
                        key={`${key}|${i}`}
                        ref={(el) => {
                          rowRefs.current[i] = el;
                        }}
                        aria-rowindex={offset + i + 2}
                        aria-selected={onSelect ? isSel : undefined}
                        tabIndex={i === active ? 0 : -1}
                        className={isSel ? 'selected' : undefined}
                        onClick={() => {
                          setActive(i);
                          select(row);
                        }}
                        onFocus={(e) => {
                          if (e.target === e.currentTarget) setActive(i);
                        }}
                        data-testid={`${prefix}-row-${key}`}
                      >
                        {columns.map((c) => (
                          <td key={c.id} className={c.className}>
                            {c.cell(row)}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p id={`${prefix}-keys`} className="xs muted">
            {keysHelp}
          </p>
          {total > 0 && (
            <nav className="row fn-pager" aria-label={`Pages of ${what}`}>
              <button type="button" className="btn sm" onClick={() => setOffset(Math.max(0, offset - limit))} disabled={atStart || loading} data-tooltip={atStart ? 'Already on the first page' : loading ? 'Loading' : undefined} data-testid={`${prefix}-prev`}>
                ‹ Previous
              </button>
              <span className="small" data-testid={`${prefix}-page`}>
                Page {n(pageNo)} of {n(pages)}
              </span>
              <button type="button" className="btn sm" onClick={() => setOffset(offset + limit)} disabled={atEnd || loading} data-tooltip={atEnd ? 'Already on the last page' : loading ? 'Loading' : undefined} data-testid={`${prefix}-next`}>
                Next ›
              </button>
            </nav>
          )}
        </>
      )}
      <div className="sr-only" role="status" aria-live="polite" data-testid={`${prefix}-announce`}>
        {announce}
      </div>
    </>
  );
}

// ------------------------------------------------------------------------------------------------ "Used by" pane
function UsedByPane({ caseId, moduleId, addr, title, heading, nav, onClose }: { caseId: string; moduleId: string; addr: string; title: string; heading: ReactNode; nav: FnNav; onClose: () => void }) {
  const api = useApi();
  const re = useMemo(() => reListApi(api), [api]);
  const [limit, setLimit] = useState(XREF_LIMIT);
  const [analyze, setAnalyze] = useState(false);
  const res = useLoad<UsedBy>(`${moduleId}|${addr}|${limit}|${analyze}`, () => re.usedBy(caseId, moduleId, addr, limit, analyze));
  const code = res.error instanceof ApiError ? res.error.code : null;
  const u = res.data;
  return (
    <section
      className="fn-pane card"
      aria-labelledby="ub-h"
      onKeyDown={(e) => {
        if (e.key !== 'Escape') return;
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }}
      data-testid="ub-pane"
    >
      <div className="row fn-pane-head">
        <h3 id="ub-h" tabIndex={-1}>
          Used by
        </h3>
        <button type="button" className="btn sm" onClick={onClose} aria-label={`Close “Used by” of ${title} (Escape)`} data-testid="ub-close">
          ✕ Close
        </button>
      </div>
      <div className="ub-item" data-testid="ub-item">
        {heading}
      </div>
      {res.error && code === 'not_analyzed' ? (
        <div className="callout warn" role="alert" data-testid="ub-not-analyzed">
          <div className="ttl">The module has not been analysed yet</div>
          <div>Finding the code that uses this needs the analysis (the list itself does not).</div>
          <div>
            <strong>Next: </strong>
            {(res.error as ApiError).nextAction}
          </div>
          <div>
            <button type="button" className="btn primary sm" onClick={() => setAnalyze(true)} disabled={analyze && res.loading} data-tooltip={analyze && res.loading ? 'Analysis is running' : undefined} data-testid="ub-analyze">
              {analyze && res.loading ? 'Analysing…' : 'Analyse now'}
            </button>
          </div>
        </div>
      ) : res.error ? (
        <ErrorCallout error={res.error} title="The references could not be listed" onRetry={res.reload} />
      ) : !u ? (
        <Loading what={`references to ${addr}${analyze ? ' (analysing the module first; this can take a minute)' : ''}`} />
      ) : u.total === 0 ? (
        <Empty title="No code refers to it directly">
          Nothing in the analysed code reads this address. It may be reached through a pointer table or a computed address, used only
          by another module, or not used at all. Search for part of it in a function’s decompiled code to check.
        </Empty>
      ) : (
        <>
          <p className="small" aria-live="polite" data-testid="ub-summary">
            {n(u.total)} reference{u.total === 1 ? '' : 's'}
            {u.functions.length ? ` from ${n(u.functions.length)} function${u.functions.length === 1 ? '' : 's'}` : ''}
            {u.more ? ` (showing the first ${n(u.refs.length)})` : ''}. Open a function to see it in the Functions list.
          </p>
          <div className="table-wrap">
            <table className="table fn-refs-table" aria-label={`Code that uses ${title}`} data-testid="ub-table">
              <thead>
                <tr>
                  <th scope="col">Address</th>
                  <th scope="col">Kind</th>
                  <th scope="col">In function</th>
                </tr>
              </thead>
              <tbody>
                {u.refs.map((r, i) => (
                  <tr key={`${r.from}|${i}`} data-testid={`ub-ref-${i}`}>
                    <td className="mono">{r.from}</td>
                    <td>
                      <span className="chip ref-kind" title={r.type ? `rizin reference type ${r.type}` : undefined}>
                        {r.kind}
                      </span>
                    </td>
                    <td>
                      {r.fcn_addr ? (
                        <FnLink addr={r.fcn_addr} name={r.fcn_name} nav={nav} testId={`ub-fn-${i}`} />
                      ) : (
                        <span className="muted small" title="The referencing address is not inside a function the analysis found (for example a jump stub or data).">
                          not inside an analysed function
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {u.more && limit < XREF_LIMIT_MAX && (
            <p className="small callout info" data-testid="ub-more">
              Only the first {n(u.refs.length)} of {n(u.total)} references are shown.{' '}
              <button type="button" className="btn sm" onClick={() => setLimit(XREF_LIMIT_MAX)} data-testid="ub-more-btn">
                Show up to {n(XREF_LIMIT_MAX)}
              </button>
            </p>
          )}
        </>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------------------------ the views
interface ViewProps {
  caseId: string;
  moduleId: string;
  moduleName: string;
  item: string | null;
  onItem: (addr: string | null) => void;
  nav: FnNav;
}

/** List + optional "Used by" pane. `close` (the list's Escape, the pane's Close or Escape) returns focus to the item's row. */
function WithPane({ prefix, item, onItem, render }: { prefix: string; item: string | null; onItem: (a: string | null) => void; render: (close: () => void) => { list: ReactNode; pane: ReactNode } }) {
  const ref = useRef<HTMLDivElement>(null);
  const [refocus, setRefocus] = useState<string | null>(null);
  useEffect(() => {
    if (!refocus || item) return;
    ref.current?.querySelector<HTMLElement>(`[data-testid="${prefix}-row-${refocus}"]`)?.focus();
    setRefocus(null);
  }, [refocus, item, prefix]);
  const close = () => {
    if (item) setRefocus(item);
    onItem(null);
  };
  const { list, pane } = render(close);
  return (
    <div ref={ref} className={`fn-layout${item ? ' with-pane' : ''}`} data-testid={`${prefix}-layout`}>
      <div className="fn-list-col stack">{list}</div>
      {item && pane}
    </div>
  );
}

function StringsView({ caseId, moduleId, moduleName, item, onItem, nav }: ViewProps) {
  const api = useApi();
  const re = useMemo(() => reListApi(api), [api]);
  const [regexOn, setRegexOn] = useState(false);
  const [minLength, setMinLength] = useState(4);
  const [section, setSection] = useState('');
  const [sections, setSections] = useState<{ section: string; count: number }[]>([]);
  const [chosen, setChosen] = useState<StringRow | null>(null);
  const [shown, setShown] = useState<StringRow[]>([]);
  const load = useCallback(
    ({ contains, offset, limit }: { contains: string; offset: number; limit: number }) =>
      re
        .strings(caseId, moduleId, { contains: regexOn ? undefined : contains, regex: regexOn ? contains || undefined : undefined, minLength, section: section || undefined, offset, limit })
        .then((p) => {
          setSections(p.sections ?? []);
          setShown(p.strings);
          return {
            rows: p.strings,
            total: p.total,
            more: p.more,
            note: p.count_truncated ? `Only the first ${n(p.scanned ?? 0)} strings of the file were read; narrow the search to find others.` : undefined,
          };
        }),
    [re, caseId, moduleId, regexOn, minLength, section],
  );
  // the selected string: as clicked, or (deep link) from the page on screen
  const row = chosen && chosen.addr === item ? chosen : shown.find((s) => s.addr === item) ?? null;
  return (
    <WithPane
      prefix="str"
      item={item}
      onItem={onItem}
      render={(close) => ({
      list: (
        <ItemList<StringRow>
          prefix="str"
          what="strings"
          label={`Strings in ${moduleName}`}
          searchLabel={regexOn ? 'Regular expression' : 'Search the text'}
          placeholder={regexOn ? 'e.g. ^error: \\w+' : 'e.g. error, http, .dll'}
          columns={[
            { id: 'addr', header: 'Address', className: 'mono', cell: (s) => s.addr },
            {
              id: 'text',
              header: 'Text',
              className: 'wrap-any',
              cell: (s) => (
                <>
                  <StringText text={s.string} testId={`str-text-${s.addr}`} />
                  <div className="xs muted ml-inline">
                    {encodingLabel(s.type)} · {s.section || 'no section'} · {n(s.length)} chars
                  </div>
                </>
              ),
            },
            { id: 'enc', header: 'Encoding', className: 'small col-opt', cell: (s) => encodingLabel(s.type) },
            { id: 'sec', header: 'Section', className: 'mono small col-opt', cell: (s) => s.section || <span className="muted">none</span> },
            { id: 'len', header: 'Length', className: 'num col-opt', cell: (s) => n(s.length) },
          ]}
          load={load}
          sourceKey={`${moduleId}|${regexOn}|${minLength}|${section}`}
          rowKey={(s) => s.addr}
          rowLabel={(s) => `string at ${s.addr}; its users are shown in the Used by pane`}
          selected={item}
          onSelect={(s) => {
            setChosen(s);
            onItem(s.addr);
          }}
          onEscape={close}
          extraFilters={
            <>
              <label className="field field-check">
                <input type="checkbox" checked={regexOn} onChange={(e) => setRegexOn(e.target.checked)} data-testid="str-regex" />
                <span className="small">Regular expression</span>
              </label>
              <label className="field">
                <span className="small">Minimum length</span>
                <select value={minLength} onChange={(e) => setMinLength(Number(e.target.value))} data-testid="str-min">
                  {STRING_MIN_LENGTHS.map((m) => (
                    <option key={m} value={m}>
                      {m} character{m === 1 ? '' : 's'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="small">Section</span>
                <select value={section} onChange={(e) => setSection(e.target.value)} data-testid="str-section">
                  <option value="">All sections</option>
                  {sections
                    .filter((s) => s.section && /^[A-Za-z0-9_.$-]{1,64}$/.test(s.section))
                    .map((s) => (
                      <option key={s.section} value={s.section}>
                        {s.section} ({n(s.count)})
                      </option>
                    ))}
                </select>
              </label>
            </>
          }
          pageSizes={STRING_PAGE_SIZES}
          emptyAll={
            <Empty title="No strings at this length">
              No text of {minLength} or more characters was found{section ? ` in ${section}` : ''}. Lower the minimum length or choose All sections; a packed
              program shows few strings until it is unpacked (see Overview).
            </Empty>
          }
          renderError={(error, retry) =>
            error instanceof ApiError && error.code === 'invalid_regex' ? (
              <div className="callout warn" role="alert" data-testid="str-regex-error">
                <div className="ttl">The pattern cannot be used</div>
                <div>{error.message}</div>
                <div>
                  <strong>Next: </strong>
                  {error.nextAction ?? 'Fix the pattern.'}
                </div>
                <div className="row">
                  <button type="button" className="btn sm" onClick={() => setRegexOn(false)} data-testid="str-regex-off">
                    Search as plain text
                  </button>
                  <button type="button" className="btn sm" onClick={retry}>
                    Retry
                  </button>
                </div>
              </div>
            ) : null
          }
          keysHelp="Click a string, or use the arrow keys (Home, End, Page Up, Page Down) and Enter, to see the code that uses it; Escape closes that pane. Long strings are cut short: use “Show all” to read the whole text. Control characters are shown as \n, \r, \t."
        />
      ),
      pane: item && (
          <UsedByPane
            caseId={caseId}
            moduleId={moduleId}
            addr={item}
            title={`the string at ${item}`}
            heading={
              row ? (
                <>
                  <StringText text={row.string} limit={300} testId="ub-string" />
                  <div className="xs muted">
                    <span className="mono">{row.addr}</span> · {encodingLabel(row.type)} · section {row.section || 'none'} · {n(row.length)} characters
                  </div>
                </>
              ) : (
                <div className="small">
                  String at <span className="mono">{item}</span>
                </div>
              )
            }
            nav={nav}
            onClose={close}
          />
        ),
      })}
    />
  );
}

const importName = (i: ImportRow) => `${i.libname ? `${i.libname}!` : ''}${i.name}`;

function ImportsView({ caseId, moduleId, moduleName, item, onItem, nav }: ViewProps) {
  const api = useApi();
  const re = useMemo(() => reListApi(api), [api]);
  const [chosen, setChosen] = useState<ImportRow | null>(null);
  const [shown, setShown] = useState<ImportRow[]>([]);
  const load = useCallback(
    ({ contains, offset, limit }: { contains: string; offset: number; limit: number }) =>
      re.table('imports', caseId, moduleId, { contains, offset, limit }).then((p) => {
        setShown(p.imports);
        return { rows: p.imports, total: p.total, more: p.more };
      }),
    [re, caseId, moduleId],
  );
  const row = chosen && chosen.addr === item ? chosen : shown.find((i) => i.addr === item) ?? null;
  return (
    <WithPane
      prefix="imp"
      item={item}
      onItem={onItem}
      render={(close) => ({
      list: (
        <ItemList<ImportRow>
          prefix="imp"
          what="imports"
          label={`Imports of ${moduleName}`}
          searchLabel="Search by name or library"
          placeholder="e.g. CreateFile, KERNEL32"
          columns={[
            { id: 'name', header: 'Name', className: 'mono wrap-any', cell: (i) => (
              <>
                {i.name}
                <div className="xs muted ml-inline">{i.libname ?? 'unknown library'}</div>
              </>
            ) },
            { id: 'lib', header: 'Library', className: 'mono small col-opt', cell: (i) => i.libname ?? <span className="muted">unknown</span> },
            { id: 'addr', header: 'Address (IAT/PLT)', className: 'mono', cell: (i) => i.addr ?? <span className="muted">none</span> },
            { id: 'type', header: 'Type', className: 'small col-opt', cell: (i) => i.type ?? '—' },
          ]}
          load={load}
          sourceKey={moduleId}
          rowKey={(i) => i.addr ?? `${i.libname}!${i.name}`}
          rowLabel={(i) => `${importName(i)}; the code that calls it is shown in the Used by pane`}
          selected={item}
          onSelect={(i) => {
            if (!i.addr) return;
            setChosen(i);
            onItem(i.addr);
          }}
          onEscape={close}
          pageSizes={TABLE_PAGE_SIZES}
          emptyAll={<Empty title="No imports">This module imports nothing from other libraries (or the import table could not be read). Statically linked programs often show none.</Empty>}
          keysHelp="Click an import, or use the arrow keys and Enter, to see which functions call it; Escape closes that pane."
        />
      ),
      pane: item && (
          <UsedByPane
            caseId={caseId}
            moduleId={moduleId}
            addr={item}
            title={row ? importName(row) : `the import at ${item}`}
            heading={
              <div className="small">
                <span className="mono wrap-any">{row ? importName(row) : `Import at ${item}`}</span>
                <div className="xs muted">
                  import address <span className="mono">{item}</span>
                  {row?.type ? ` · ${row.type}` : ''}
                </div>
              </div>
            }
            nav={nav}
            onClose={close}
          />
        ),
      })}
    />
  );
}

function ExportsView({ caseId, moduleId, moduleName, nav }: ViewProps) {
  const api = useApi();
  const re = useMemo(() => reListApi(api), [api]);
  const load = useCallback(
    ({ contains, offset, limit }: { contains: string; offset: number; limit: number }) =>
      re.table('exports', caseId, moduleId, { contains, offset, limit }).then((p) => ({ rows: p.exports, total: p.total, more: p.more })),
    [re, caseId, moduleId],
  );
  return (
    <ItemList<ExportRow>
      prefix="exp"
      what="exports"
      label={`Exports of ${moduleName}`}
      searchLabel="Search by name"
      placeholder="e.g. Open, Dll"
      columns={[
        {
          id: 'name',
          header: 'Name',
          className: 'wrap-any',
          cell: (x) =>
            x.addr && (x.type ?? 'FUNC') === 'FUNC' ? (
              <FnLink addr={x.addr} name={x.name} nav={nav} testId={`exp-fn-${x.addr}`} tabIndex={-1} />
            ) : (
              <span className="mono">{x.name}</span>
            ),
        },
        { id: 'addr', header: 'Address', className: 'mono', cell: (x) => x.addr ?? <span className="muted">none</span> },
        { id: 'type', header: 'Type', className: 'small col-opt', cell: (x) => x.type ?? '—' },
      ]}
      load={load}
      sourceKey={moduleId}
      rowKey={(x) => x.addr ?? x.name}
      rowLabel={(x) => `${x.name}; opening it in Functions`}
      onSelect={(x) => {
        if (x.addr && (x.type ?? 'FUNC') === 'FUNC') nav.open(x.addr);
      }}
      pageSizes={TABLE_PAGE_SIZES}
      emptyAll={
        <Empty title="No exports">
          This module exports nothing. Programs (.exe) usually export nothing; libraries (.dll, .so) list the functions other programs can call here.
        </Empty>
      }
      keysHelp="Exported functions are links: click one, or use the arrow keys and Enter, to open it in Functions."
    />
  );
}

function SectionsView({ caseId, moduleId, moduleName, nav }: ViewProps) {
  const api = useApi();
  const re = useMemo(() => reListApi(api), [api]);
  const res = useLoad<SectionsInfo>(moduleId, () => re.sections(caseId, moduleId));
  const info = res.data;
  const load = useCallback(
    ({ contains, offset, limit }: { contains: string; offset: number; limit: number }) => {
      const all = [...(info?.sections ?? []), ...(info?.segments ?? []).map((s) => ({ ...s, _segment: true }))].filter(
        (s) => !contains || s.name.toLowerCase().includes(contains.toLowerCase()),
      );
      return Promise.resolve({ rows: all.slice(offset, offset + limit), total: all.length, more: offset + limit < all.length });
    },
    [info],
  );
  if (res.error)
    return (
      <>
        <ErrorCallout error={res.error} title="The sections could not be read" onRetry={res.reload} />
        {res.error instanceof ApiError && res.error.code === 'engine_missing' && (
          <p className="small">
            <Link to="/tools" data-testid="sec-open-tools">Open Tools to install rizin</Link>
          </p>
        )}
      </>
    );
  if (!info) return <Loading what={`sections of ${moduleName}`} />;
  const hasEntropy = info.sections.some((s) => s.entropy !== undefined);
  const highs = info.sections.filter((s) => s.high_entropy);
  return (
    <div className="stack">
      <div className="card ml-entry" data-testid="sec-entry">
        <h3 className="fn-sub-h">Entry points</h3>
        {info.entrypoints.length ? (
          <ul className="ml-entry-list">
            {info.entrypoints.map((e) => (
              <li key={e.addr}>
                <FnLink addr={e.addr} nav={nav} testId={`sec-entry-${e.addr}`} /> <span className="small muted">{e.type === 'program' ? 'where the program starts' : e.type ?? ''}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="small muted">No entry point is recorded (common for data-only files and some libraries).</p>
        )}
      </div>
      {highs.length > 0 && (
        <div className="callout warn" role="note" data-testid="sec-high">
          <div className="ttl">High entropy</div>
          {highs.map((s) => s.name).join(', ')} {highs.length === 1 ? 'looks' : 'look'} compressed or encrypted (entropy {highs.map((s) => s.entropy?.toFixed(2)).join(', ')} of 8). If that is code, the program may be packed: see Overview for
          the packer check and the unpack option.
        </div>
      )}
      <ItemList<SectionRow & { _segment?: boolean }>
        prefix="sec"
        what="sections"
        label={`Sections of ${moduleName}`}
        searchLabel="Search by name"
        placeholder="e.g. .text, .rsrc"
        columns={[
          { id: 'name', header: 'Name', className: 'mono wrap-any', cell: (s) => (
            <>
              {s.name || <span className="muted">(no name)</span>}
              {s._segment && <span className="xs muted"> segment</span>}
            </>
          ) },
          { id: 'addr', header: 'Address', className: 'mono', cell: (s) => s.vaddr ?? '—' },
          { id: 'vsize', header: 'Size in memory', className: 'num', cell: (s) => (s.vsize !== undefined ? n(s.vsize) : '—') },
          { id: 'size', header: 'Size in file', className: 'num col-opt', cell: (s) => (s.size !== undefined ? n(s.size) : '—') },
          { id: 'perm', header: 'Permissions', className: 'small', cell: (s) => <span title={permWords(s.perm)}>{permWords(s.perm)}</span> },
          {
            id: 'ent',
            header: 'Entropy',
            className: 'num col-opt',
            cell: (s) =>
              s.entropy === undefined ? (
                <span className="muted" title="The packer report has no value for this section">—</span>
              ) : (
                <span title="Shannon entropy of the section's bytes, 0 to 8; above 7.2 usually means compressed or encrypted data" data-testid={`sec-entropy-${s.name}`}>
                  {s.entropy.toFixed(2)}
                  {s.high_entropy && <span className="chip warn xs ml-high"> high</span>}
                </span>
              ),
          },
        ]}
        load={load}
        sourceKey={`${moduleId}|${info.sections.length}`}
        rowKey={(s) => `${s.name}@${s.vaddr ?? ''}`}
        rowLabel={(s) => `section ${s.name}`}
        pageSizes={TABLE_PAGE_SIZES}
        emptyAll={<Empty title="No sections">The file has no section table (it may be raw code or a damaged header).</Empty>}
        keysHelp={`Use the arrow keys to move through the sections. ${hasEntropy ? 'Entropy comes from the packer check of the original file.' : 'Entropy is shown once the packer check has run (see Overview).'}`}
      />
    </div>
  );
}

/** The non-function lists of the Functions area for one module. */
export default function ModuleLists({ kind, ...props }: ViewProps & { kind: Exclude<ModuleList, 'functions' | 'search'> }) {
  const View = { strings: StringsView, imports: ImportsView, exports: ExportsView, sections: SectionsView }[kind];
  return <View key={`${kind}|${props.moduleId}`} {...props} />;
}
