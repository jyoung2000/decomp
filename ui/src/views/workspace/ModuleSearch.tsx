import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { Empty, Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { ApiError } from '../../lib/api';
import {
  BYTE_PATTERN_EXAMPLE,
  SEARCH_PAGE_SIZES,
  TEXT_PATTERN_EXAMPLE,
  checkBytePattern,
  checkTextPattern,
  reSearchApi,
  textPatternFor,
  type SearchKind,
  type SearchMatch,
  type SearchPage,
} from '../../lib/re';
import { useApi } from '../../lib/store';
import { FnLink, type FnNav } from './FunctionRefs';
import { StringText, encodingLabel } from './ModuleLists';

// R6 piece 6: search the module for a byte pattern (hex, ?? = any byte) or a text pattern (plain text or a regular expression over
// the extracted strings). GET .../pattern-search runs the same workbench call as the MCP search_bytes tool. A hit's containing
// function opens in Functions through the piece-3 links (URL + pane history). The submitted search is in the URL (?sq=&sk=&sre=)
// so it survives following a hit and coming back. Program bytes are shown as hex and strings as plain text, never interpreted.

const n = (v: number) => v.toLocaleString('en-US');

export interface SearchQuery {
  pattern: string;
  kind: SearchKind;
  /** text search: the pattern is a regular expression (otherwise it is matched as plain text) */
  regex: boolean;
}

export default function ModuleSearch({ caseId, moduleId, moduleName, nav, query, onQuery }: { caseId: string; moduleId: string; moduleName: string; nav: FnNav; query: SearchQuery; onQuery: (q: SearchQuery) => void }) {
  const api = useApi();
  const re = useMemo(() => reSearchApi(api), [api]);
  const [draft, setDraft] = useState(query.pattern);
  const [kind, setKind] = useState<SearchKind>(query.kind);
  const [regex, setRegex] = useState(query.regex);
  const [tried, setTried] = useState(false);
  const [limit, setLimit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [analyze, setAnalyze] = useState(false);
  const [page, setPage] = useState<SearchPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const [active, setActive] = useState(0);
  const [announce, setAnnounce] = useState('');
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([]);
  const focusRow = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // the URL changed from outside (Back, a link): show that search in the form
  useEffect(() => {
    setDraft(query.pattern);
    setKind(query.kind);
    setRegex(query.regex);
  }, [query.pattern, query.kind, query.regex]);

  const problem = kind === 'hex' ? checkBytePattern(draft) : checkTextPattern(draft, !regex);
  const showProblem = !!problem && (tried || draft.length > 0);
  const sent = query.pattern ? (query.kind === 'hex' ? query.pattern : textPatternFor(query.pattern, !query.regex)) : '';

  const source = `${moduleId}|${query.kind}|${sent}`;
  const lastSource = useRef(source);

  useEffect(() => {
    if (lastSource.current !== source) {   // a new search (or module) starts on page 1
      lastSource.current = source;
      if (offset !== 0) {
        setOffset(0);
        return;
      }
    }
    if (!sent) {
      setPage(null);
      setError(null);
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    re.search(caseId, moduleId, { kind: query.kind, pattern: sent, offset, limit, analyze }).then(
      (p) => {
        if (!alive) return;
        setPage(p);
        setError(null);
        setLoading(false);
        setActive(0);
        if (p.offset === 0) setAnnounce(p.listed === 0 ? 'No matches.' : `${n(p.found)}${p.capped ? '+' : ''} match${p.found === 1 ? '' : 'es'} found.`);
      },
      (e) => {
        if (!alive) return;
        setError(e);
        setPage(null);
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [re, caseId, source, offset, limit, analyze, tick]);

  const rows = page?.matches ?? [];
  useEffect(() => {
    if (focusRow.current) rowRefs.current[active]?.focus();
    focusRow.current = false;
  }, [active, rows]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTried(true);
    if (problem) {
      inputRef.current?.focus();
      return;
    }
    const q = { pattern: kind === 'hex' ? draft.trim() : draft, kind, regex: kind === 'text' && regex };
    if (q.pattern === query.pattern && q.kind === query.kind && q.regex === query.regex) setTick((t) => t + 1);   // search again
    else onQuery(q);
  };

  const openRow = (m: SearchMatch) => {
    if (m.fcn_addr) {
      nav.open(m.fcn_addr);
      return;
    }
    setAnnounce(
      page?.functions_known
        ? `The hit at ${m.addr ?? m.paddr} is not inside an analysed function.`
        : 'Containing functions are known once the module is analysed; choose Analyse now.',
    );
  };

  const onKey = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    if (!rows.length || !(e.target instanceof HTMLTableRowElement)) return;   // keys on a link inside a row are the link's
    let next = active;
    switch (e.key) {
      case 'ArrowDown': next = Math.min(rows.length - 1, active + 1); break;
      case 'ArrowUp': next = Math.max(0, active - 1); break;
      case 'Home': next = 0; break;
      case 'End': next = rows.length - 1; break;
      case 'PageDown': next = Math.min(rows.length - 1, active + 10); break;
      case 'PageUp': next = Math.max(0, active - 10); break;
      case 'Enter':
        e.preventDefault();
        openRow(rows[active]);
        return;
      default:
        return;
    }
    e.preventDefault();
    focusRow.current = true;
    setActive(next);
  };

  const code = error instanceof ApiError ? error.code : null;
  const serverPattern = code === 'invalid_pattern' ? (error as ApiError) : null;
  const listed = page?.listed ?? 0;
  const first = listed ? offset + 1 : 0;
  const last = offset + rows.length;
  const pages = Math.max(1, Math.ceil(listed / limit));
  const pageNo = Math.floor(offset / limit) + 1;
  const atStart = offset === 0;
  const atEnd = !page?.more;
  const hintId = 'ms-hint';
  const errId = 'ms-error';

  return (
    <div className="stack" data-testid="ms-view">
      <form className="filters ms-form" role="search" aria-label={`Search ${moduleName}`} onSubmit={submit} noValidate>
        <fieldset className="ms-kind" data-testid="ms-kind">
          <legend className="small">Search for</legend>
          <label className="small">
            <input type="radio" name="ms-kind" value="hex" checked={kind === 'hex'} onChange={() => setKind('hex')} data-testid="ms-kind-hex" /> Bytes (hex)
          </label>
          <label className="small">
            <input type="radio" name="ms-kind" value="text" checked={kind === 'text'} onChange={() => setKind('text')} data-testid="ms-kind-text" /> Text in strings
          </label>
        </fieldset>
        <label className="field ms-field">
          <span className="small">{kind === 'hex' ? 'Byte pattern' : regex ? 'Regular expression' : 'Text'}</span>
          <input
            ref={inputRef}
            type="search"
            className="mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={kind === 'hex' ? `e.g. ${BYTE_PATTERN_EXAMPLE}` : regex ? `e.g. ${TEXT_PATTERN_EXAMPLE}` : 'e.g. usage, error:, .dll'}
            maxLength={800}
            spellCheck={false}
            autoComplete="off"
            aria-invalid={showProblem || !!serverPattern}
            aria-describedby={showProblem || serverPattern ? `${errId} ${hintId}` : hintId}
            data-testid="ms-pattern"
          />
        </label>
        {kind === 'text' && (
          <label className="field field-check">
            <input type="checkbox" checked={regex} onChange={(e) => setRegex(e.target.checked)} data-testid="ms-regex" />
            <span className="small">Regular expression</span>
          </label>
        )}
        <div className="field ms-go">
          <span className="small" aria-hidden="true">
            &nbsp;
          </span>
          <button type="submit" className="btn primary sm" data-testid="ms-submit">
            Search
          </button>
        </div>
        <label className="field">
          <span className="small">Rows per page</span>
          <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }} data-testid="ms-limit">
            {SEARCH_PAGE_SIZES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </form>
      {(showProblem || serverPattern) && (
        <p id={errId} className="small bad-text" role="alert" data-testid="ms-error">
          {showProblem ? problem : serverPattern!.message}
          {!showProblem && serverPattern!.nextAction ? ` ${serverPattern!.nextAction}` : ''}
        </p>
      )}
      <p id={hintId} className="xs muted" data-testid="ms-hint">
        {kind === 'hex'
          ? `Pairs of hex digits, spaces optional; ?? matches any one byte. Example: ${BYTE_PATTERN_EXAMPLE}. The whole file is searched, headers included.`
          : regex
            ? `A regular expression over the strings found in the program (at most 200 characters; no nested repeats or backreferences). Example: ${TEXT_PATTERN_EXAMPLE}.`
            : 'Finds the text, as typed, inside the strings found in the program (case matters). Turn on Regular expression for patterns.'}
      </p>

      {!sent ? (
        <Empty title="Search this module">
          Type a byte pattern such as <span className="mono">{BYTE_PATTERN_EXAMPLE}</span>, or switch to Text in strings, then press Enter. Each hit links to the
          function that contains it.
        </Empty>
      ) : serverPattern ? null : error ? (
        <>
          <ErrorCallout error={error} title="The search could not run" onRetry={() => setTick((t) => t + 1)} />
          {code === 'engine_missing' && (
            <p className="small">
              <Link to="/tools" data-testid="ms-open-tools">Open Tools to install rizin</Link>
            </p>
          )}
        </>
      ) : !page ? (
        <Loading what={`matches in ${moduleName}${analyze ? ' (analysing the module first; this can take a minute)' : ''}`} />
      ) : (
        <>
          <div className="row small muted fn-summary" aria-live="polite" data-testid="ms-summary">
            {listed === 0
              ? `No match for ${query.kind === 'hex' ? 'the bytes' : 'the text'} “${page.pattern}”.`
              : `Showing ${n(first)}–${n(last)} of ${n(listed)} match${listed === 1 ? '' : 'es'} for “${page.pattern}”.`}
            {page.capped && (
              <span data-testid="ms-capped">
                {' '}
                {query.kind === 'text' && page.found_exact
                  ? `${n(page.found)} strings match; only the first ${n(page.max_hits)} are listed.`
                  : `There are more than ${n(page.max_hits)}; only the first ${n(page.max_hits)} are listed. Make the pattern longer to narrow it.`}
              </span>
            )}
            {loading && <span> Updating…</span>}
          </div>
          {!page.functions_known && listed > 0 && (
            <div className="callout info small" data-testid="ms-not-analyzed">
              <span>Containing functions are shown once the module has been analysed.</span>{' '}
              <button type="button" className="btn sm" onClick={() => setAnalyze(true)} disabled={analyze && loading} data-tooltip={analyze && loading ? 'Analysis is running' : undefined} data-testid="ms-analyze">
                {analyze && loading ? 'Analysing…' : 'Analyse now'}
              </button>
            </div>
          )}
          {listed === 0 ? (
            <Empty title="No matches" action={<button type="button" className="btn sm" onClick={() => inputRef.current?.focus()} data-testid="ms-edit">Change the pattern</button>}>
              {query.kind === 'hex'
                ? 'No place in the file has these bytes. Check the byte order (x86 stores numbers little-endian: 0x1234 is 34 12), or replace uncertain bytes with ??.'
                : query.regex
                  ? 'No extracted string matches. Strings shorter than four characters or inside packed data are not extracted; try a shorter pattern.'
                  : 'No extracted string contains this text (case matters). Try part of it, or a regular expression such as [Uu]sage.'}
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table fn-table ml-table ms-table" role="grid" aria-label={`Matches for ${page.pattern} in ${moduleName}`} aria-rowcount={listed + 1} aria-describedby="ms-keys" data-testid="ms-table">
                <thead>
                  <tr>
                    <th scope="col">Address</th>
                    <th scope="col" className="col-opt">
                      Section
                    </th>
                    <th scope="col">In function</th>
                    <th scope="col">{query.kind === 'hex' ? 'Bytes around it' : 'String'}</th>
                  </tr>
                </thead>
                <tbody onKeyDown={onKey}>
                  {rows.map((m, i) => (
                    <tr
                      key={`${m.paddr}|${i}`}
                      ref={(el) => {
                        rowRefs.current[i] = el;
                      }}
                      aria-rowindex={offset + i + 2}
                      tabIndex={i === active ? 0 : -1}
                      onFocus={(e) => {
                        if (e.target === e.currentTarget) setActive(i);
                      }}
                      onClick={() => setActive(i)}
                      data-testid={`ms-row-${i}`}
                    >
                      <td className="mono">
                        {m.addr ?? <span className="muted" title="These bytes are not mapped into memory (for example the file header).">not mapped</span>}
                        <div className="xs muted">file offset {m.paddr}</div>
                        <div className="xs muted ml-inline">{m.section || 'no section'}</div>
                      </td>
                      <td className="mono small col-opt">{m.section || <span className="muted">none</span>}</td>
                      <td>
                        {m.fcn_addr ? (
                          <FnLink addr={m.fcn_addr} name={m.fcn_name} nav={nav} testId={`ms-fn-${i}`} tabIndex={i === active ? 0 : -1} />
                        ) : (
                          <span className="muted small" data-testid={`ms-nofn-${i}`}>
                            {page.functions_known ? 'not inside an analysed function' : 'analyse to see'}
                          </span>
                        )}
                      </td>
                      <td className="wrap-any">
                        {query.kind === 'text' && m.string !== undefined && (
                          <>
                            <StringText text={m.string} testId={`ms-str-${i}`} />
                            <div className="xs muted">
                              {encodingLabel(m.encoding)}
                              {m.length !== undefined ? ` · ${n(m.length)} chars` : ''}
                            </div>
                          </>
                        )}
                        {m.bytes !== undefined && (
                          <div className={`mono ms-bytes${query.kind === 'text' ? ' xs' : ' small'}`} data-testid={`ms-bytes-${i}`} title="Raw bytes as hex: up to 8 before the hit, the hit (highlighted), up to 8 after.">
                            {m.before && <span className="muted">{m.before} </span>}
                            <mark className="ms-hit">{m.bytes}</mark>
                            {m.bytes_clipped ? <span className="muted"> … ({n(m.bytes_clipped)} bytes)</span> : m.after ? <span className="muted"> {m.after}</span> : null}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p id="ms-keys" className="xs muted">
            Use the arrow keys (Home, End, Page Up, Page Down) to move through the hits and press Enter to open the function that contains one; the
            function opens in Functions, and the Search tab keeps this search. Bytes are shown as hex and strings as plain text.
          </p>
          {listed > 0 && (
            <nav className="row fn-pager" aria-label="Pages of matches">
              <button type="button" className="btn sm" onClick={() => setOffset(Math.max(0, offset - limit))} disabled={atStart || loading} data-tooltip={atStart ? 'Already on the first page' : loading ? 'Loading' : undefined} data-testid="ms-prev">
                ‹ Previous
              </button>
              <span className="small" data-testid="ms-page">
                Page {n(pageNo)} of {n(pages)}
              </span>
              <button type="button" className="btn sm" onClick={() => setOffset(offset + limit)} disabled={atEnd || loading} data-tooltip={atEnd ? 'Already on the last page' : loading ? 'Loading' : undefined} data-testid="ms-next">
                Next ›
              </button>
            </nav>
          )}
        </>
      )}
      <div className="sr-only" role="status" aria-live="polite" data-testid="ms-announce">
        {announce}
      </div>
    </div>
  );
}
