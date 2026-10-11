import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { LocalTabs } from '../../components/Tabs';
import { ApiError } from '../../lib/api';
import { DISASM_PAGE, reFunctionApi, type Decompiled, type Decompiler, type DecompilerInfo, type DisasmPage, type FunctionDetail } from '../../lib/re';
import { useApi } from '../../lib/store';

export type CodeView = 'decompiled' | 'disassembly';

const n = (v: number) => v.toLocaleString('en-US');
const bytes = (v: number) => `${n(v)} byte${v === 1 ? '' : 's'}`;

/** Loads one request keyed by `key`; stale answers (an older selection) are dropped. */
function useLoad<T>(key: string, load: () => Promise<T>) {
  const [state, setState] = useState<{ key: string; data?: T; error?: unknown } | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setState(null);
    load().then(
      (data) => alive && setState({ key, data }),
      (error) => alive && setState({ key, error }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);
  const current = state && state.key === key ? state : null;
  return { data: current?.data, error: current?.error, loading: !current, reload: () => setTick((t) => t + 1) };
}

function CopyButton({ text, what, testId }: { text: string; what: string; testId: string }) {
  const [msg, setMsg] = useState('');
  return (
    <>
      <button
        type="button"
        className="btn sm"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setMsg(`Copied the ${what}.`);
          } catch {
            setMsg('Copying is not allowed here; select the text and press Ctrl+C.');
          }
        }}
        data-testid={testId}
      >
        Copy
      </button>
      <span className="xs muted" role="status" aria-live="polite" data-testid={`${testId}-status`}>
        {msg}
      </span>
    </>
  );
}

/** R6 piece 2: the side pane of the function selected in the Functions table — header, decompiled code (rizin or Ghidra)
 * and disassembly. All text is binary-derived and untrusted: it is rendered as plain text, never as HTML. */
export function FunctionPane({
  caseId,
  moduleId,
  addr,
  view,
  decompiler,
  onView,
  onDecompiler,
  onClose,
}: {
  caseId: string;
  moduleId: string;
  addr: string;
  view: CodeView;
  decompiler: Decompiler;
  onView: (v: CodeView) => void;
  onDecompiler: (d: Decompiler) => void;
  onClose: () => void;
}) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const detail = useLoad<FunctionDetail>(`${moduleId}|${addr}`, () => re.detail(caseId, moduleId, addr));
  const paneRef = useRef<HTMLElement>(null);

  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
  };
  const h = detail.data?.header;
  return (
    <section ref={paneRef} className="fn-pane card" aria-labelledby="fn-pane-h" onKeyDown={onKey} data-testid="fn-pane">
      <div className="row fn-pane-head">
        <h3 id="fn-pane-h" className="mono wrap-any" data-testid="fn-pane-name">
          {h?.name ?? addr}
        </h3>
        <button type="button" className="btn sm" onClick={onClose} aria-label="Close the function pane (Escape)" data-testid="fn-pane-close">
          ✕ Close
        </button>
      </div>
      {detail.error ? (
        <ErrorCallout error={detail.error} title="This function could not be opened" onRetry={detail.reload} />
      ) : !h ? (
        <Loading what={`function ${addr}`} />
      ) : (
        <dl className="fn-facts small" data-testid="fn-pane-header">
          <dt>Address</dt>
          <dd className="mono">
            {h.addr}
            {h.range && h.range.end !== h.addr && (
              <span className="muted">
                {' '}
                (code from {h.range.start} to {h.range.end})
              </span>
            )}
          </dd>
          <dt>Code size</dt>
          <dd data-testid="fn-pane-size">
            {bytes(h.size)}
            {h.span !== undefined && <span className="muted"> spread over an address span of {bytes(h.span)}</span>}
            {h.nbbs !== undefined && <span className="muted"> · {n(h.nbbs)} block{h.nbbs === 1 ? '' : 's'}</span>}
          </dd>
          <dt>Signature</dt>
          <dd className="mono wrap-any" data-testid="fn-pane-sig">
            {h.signature ?? <span className="muted">not recovered</span>}
            {h.calltype && <span className="muted"> · calling convention {h.calltype}</span>}
          </dd>
        </dl>
      )}
      <LocalTabs<CodeView>
        label="Code views"
        value={view}
        onChange={onView}
        tabs={[
          { id: 'decompiled', label: 'Decompiled' },
          { id: 'disassembly', label: 'Disassembly' },
        ]}
      />
      <div role="tabpanel" id={`panel-${view}`} aria-labelledby={`tab-${view}`} className="fn-panel">
        {view === 'decompiled' ? (
          <DecompiledView key={`${addr}|${decompiler}`} caseId={caseId} moduleId={moduleId} addr={addr} decompiler={decompiler} decompilers={detail.data?.decompilers} onDecompiler={onDecompiler} />
        ) : (
          <DisassemblyView key={addr} caseId={caseId} moduleId={moduleId} addr={addr} />
        )}
      </div>
      <p className="xs muted">
        Names, code and comments here come from the analysed program and are shown as plain text. Escape closes this pane.
      </p>
    </section>
  );
}

function DecompilerSwitch({ value, onChange, decompilers }: { value: Decompiler; onChange: (d: Decompiler) => void; decompilers?: DecompilerInfo[] }) {
  const info = (id: Decompiler) => decompilers?.find((d) => d.id === id);
  const gh = info('ghidra');
  const rz = info('rizin');
  return (
    <fieldset className="fn-dec-switch" data-testid="fn-dec-switch">
      <legend className="small">Decompiler</legend>
      <label className="small">
        <input type="radio" name="fn-decompiler" value="rizin" checked={value === 'rizin'} onChange={() => onChange('rizin')} /> rizin
        {rz && !rz.real_decompiler && <span className="muted"> (pseudo-code only: rz-ghidra is not loaded)</span>}
      </label>
      <label className="small">
        <input type="radio" name="fn-decompiler" value="ghidra" checked={value === 'ghidra'} onChange={() => onChange('ghidra')} /> Ghidra
        {gh && !gh.available && <span className="muted"> (not installed)</span>}
        {gh?.available && <span className="muted"> (slower: a full Ghidra analysis on first use)</span>}
      </label>
    </fieldset>
  );
}

function DecompiledView({ caseId, moduleId, addr, decompiler, decompilers, onDecompiler }: { caseId: string; moduleId: string; addr: string; decompiler: Decompiler; decompilers?: DecompilerInfo[]; onDecompiler: (d: Decompiler) => void }) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const res = useLoad<Decompiled>(`${moduleId}|${addr}|${decompiler}`, () => re.decompiled(caseId, moduleId, addr, decompiler));
  const code = res.error instanceof ApiError ? res.error.code : null;
  const d = res.data;
  return (
    <div className="stack" data-testid="fn-decompiled">
      <div className="row fn-code-bar">
        <DecompilerSwitch value={decompiler} onChange={onDecompiler} decompilers={decompilers} />
        {d && <CopyButton text={d.decompiled.text} what="decompiled code" testId="fn-copy-decompiled" />}
      </div>
      {code === 'decompiler_unavailable' ? (
        <div className="callout warn" role="alert" data-testid="fn-dec-unavailable">
          <div className="ttl">Ghidra is not available</div>
          <div>{(res.error as ApiError).message}</div>
          <div>
            <strong>Next: </strong>
            {(res.error as ApiError).nextAction}
          </div>
          <div className="row">
            <button type="button" className="btn primary sm" onClick={() => onDecompiler('rizin')} data-testid="fn-use-rizin">
              Use rizin
            </button>
            <Link to="/tools" className="btn sm">
              Open Tools
            </Link>
          </div>
        </div>
      ) : res.error ? (
        <ErrorCallout error={res.error} title="The function could not be decompiled" onRetry={res.reload} />
      ) : !d ? (
        <Loading what={`decompiled code (${decompiler === 'ghidra' ? 'Ghidra can take a few minutes the first time' : 'rizin'})`} />
      ) : (
        <>
          <p className="small" data-testid="fn-dec-engine">
            Produced by <span className="mono">{d.decompiler}</span>{' '}
            {d.is_real_decompiler ? (
              <span className="chip ok">real decompiler</span>
            ) : (
              <>
                <span className="chip warn">fallback</span>
                <span className="muted"> rizin pseudo-code, not a real decompiler: read it next to the disassembly.</span>
              </>
            )}
            {d.engine !== d.requested && <span className="muted"> (asked for {d.requested})</span>}
          </p>
          {d.annotations_note && <p className="xs muted">{d.annotations_note}</p>}
          {(d.truncated || d.decompiled.truncated) && (
            <p className="small callout warn" data-testid="fn-dec-truncated">
              The code was cut at the size limit{d.decompiled.total_bytes ? ` (${bytes(d.decompiled.total_bytes)} in full)` : ''}. The Disassembly view shows every instruction.
            </p>
          )}
          <pre className="pre fn-code" tabIndex={0} aria-label={`Decompiled code of ${d.function}`} data-testid="fn-dec-code">
            {d.decompiled.text}
          </pre>
        </>
      )}
    </div>
  );
}

function DisassemblyView({ caseId, moduleId, addr }: { caseId: string; moduleId: string; addr: string }) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const [offset, setOffset] = useState(0);
  const res = useLoad<DisasmPage>(`${moduleId}|${addr}|${offset}`, () => re.disassembly(caseId, moduleId, addr, offset, DISASM_PAGE));
  const [last, setLast] = useState<DisasmPage | null>(null);
  useEffect(() => {
    if (res.data) setLast(res.data);
  }, [res.data]);
  const p = res.data ?? last;
  const text = useMemo(() => (p ? p.ops.map((o) => `${o.addr}  ${(o.bytes ?? '').padEnd(20)}  ${o.disasm ?? ''}${o.user_comment ? `  ; ${o.user_comment}` : ''}`).join('\n') : ''), [p]);
  if (res.error) return <ErrorCallout error={res.error} title="The function could not be disassembled" onRetry={res.reload} />;
  if (!p) return <Loading what="disassembly" />;
  const atEnd = !p.more;
  return (
    <div className="stack" data-testid="fn-disasm">
      <div className="row fn-code-bar">
        <span className="small muted" data-testid="fn-disasm-summary">
          {p.total === 0 ? 'No instructions.' : `Instructions ${n(p.offset + 1)}–${n(p.offset + p.ops.length)} of ${n(p.total)}.`}
          {res.loading && ' Loading…'}
        </span>
        <CopyButton text={text} what="instructions on this page" testId="fn-copy-disasm" />
      </div>
      <div className="table-wrap fn-disasm-wrap" tabIndex={0} aria-label="Disassembly">
        <table className="table fn-disasm mono small" data-testid="fn-disasm-table">
          <thead>
            <tr>
              <th scope="col">Address</th>
              <th scope="col" className="col-bytes">
                Bytes
              </th>
              <th scope="col">Instruction</th>
            </tr>
          </thead>
          <tbody>
            {p.ops.map((o) => (
              <tr key={o.addr} data-testid={`fn-op-${o.addr}`}>
                <td>{o.addr}</td>
                <td className="col-bytes muted wrap-any">{o.bytes}</td>
                <td className="wrap-any">
                  {o.disasm}
                  {(o.user_comment || o.comment) && <span className="muted"> ; {o.user_comment ?? o.comment}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {p.total > DISASM_PAGE && (
        <nav className="row fn-pager" aria-label="Disassembly pages">
          <button type="button" className="btn sm" onClick={() => setOffset(Math.max(0, offset - DISASM_PAGE))} disabled={offset === 0 || res.loading} data-tooltip={offset === 0 ? 'Already on the first page' : res.loading ? 'Loading' : undefined} data-testid="fn-disasm-prev">
            ‹ Previous
          </button>
          <button type="button" className="btn sm" onClick={() => setOffset(offset + DISASM_PAGE)} disabled={atEnd || res.loading} data-tooltip={atEnd ? 'Already on the last page' : res.loading ? 'Loading' : undefined} data-testid="fn-disasm-next">
            Next ›
          </button>
        </nav>
      )}
    </div>
  );
}
