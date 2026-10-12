import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { LocalTabs } from '../../components/Tabs';
import { ApiError } from '../../lib/api';
import { DISASM_PAGE, reEditApi, reFunctionApi, type Decompiled, type Decompiler, type DecompilerInfo, type DisasmOp, type DisasmPage, type FunctionDetail } from '../../lib/re';
import { useApi } from '../../lib/store';
import { CallGraphView, DEFAULT_GRAPH, ReferencesView, type FnNav, type GraphSettings } from './FunctionRefs';
import { CommentEditor, HistoryView, TypesDialog, ValueEditor, VariablesPanel } from './FunctionEdit';
import { useLoad } from './useLoad';

export type CodeView = 'decompiled' | 'disassembly' | 'references' | 'callgraph' | 'history';
export const CODE_VIEWS: CodeView[] = ['decompiled', 'disassembly', 'references', 'callgraph', 'history'];
type Editing = null | 'name' | 'prototype' | 'comment';

const n = (v: number) => v.toLocaleString('en-US');
const bytes = (v: number) => `${n(v)} byte${v === 1 ? '' : 's'}`;

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

/** Back/Forward inside the pane: the functions opened in it, newest last. */
export interface PaneHistory {
  canBack: boolean;
  canForward: boolean;
  back: () => void;
  forward: () => void;
}

/** R6 side pane of the function selected in the Functions table — header, decompiled code (rizin or Ghidra), disassembly
 * (piece 2), references and a bounded call graph (piece 3; links select another function, with Back/Forward). All text is
 * binary-derived and untrusted: it is rendered as plain text, never as HTML. */
export function FunctionPane({
  caseId,
  moduleId,
  addr,
  view,
  decompiler,
  onView,
  onDecompiler,
  onClose,
  nav,
  history,
  editTick = 0,
  onEdited = () => {},
}: {
  caseId: string;
  moduleId: string;
  addr: string;
  view: CodeView;
  decompiler: Decompiler;
  onView: (v: CodeView) => void;
  onDecompiler: (d: Decompiler) => void;
  onClose: () => void;
  nav: FnNav;
  history: PaneHistory;
  /** bumped after every saved edit, undo, roll back or review: the open views and the list reload */
  editTick?: number;
  onEdited?: (message: string) => void;
}) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const detail = useLoad<FunctionDetail>(`${moduleId}|${addr}|${editTick}`, () => re.detail(caseId, moduleId, addr));
  const editApi = useMemo(() => reEditApi(api), [api]);
  // keep showing the last header of this function while it reloads after an edit (the edit controls stay in place)
  const lastDetail = useRef<FunctionDetail | null>(null);
  if (detail.data) lastDetail.current = detail.data;
  const shown = detail.data ?? (lastDetail.current?.header.addr === addr ? lastDetail.current : undefined);
  const [editing, setEditing] = useState<Editing>(null);
  const [typesOpen, setTypesOpen] = useState(false);
  const [status, setStatus] = useState('');
  const editBtn = useRef<Record<string, HTMLButtonElement | null>>({});
  const returnFocus = useRef<string | null>(null);
  useEffect(() => {
    setEditing(null);
    setStatus('');
  }, [addr]);
  useEffect(() => {
    if (editing || !returnFocus.current) return;
    const key = returnFocus.current;
    const btn = editBtn.current[key];
    if (btn) {
      btn.focus();
      returnFocus.current = null;
    }
  });
  const edited = (m: string) => {
    setStatus(m);
    onEdited(m);
  };
  const closeEditor = (which: string) => (m: string | null) => {
    returnFocus.current = which;
    setEditing(null);
    if (m) edited(m);
    else setStatus('Not changed.');
  };
  const paneRef = useRef<HTMLElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);
  const [graph, setGraph] = useState<GraphSettings>(DEFAULT_GRAPH);
  const [announce, setAnnounce] = useState('');
  const firstAddr = useRef(addr);

  // moving to another function from inside the pane (a link, Enter in the call graph, Back/Forward) puts focus on its name
  useEffect(() => {
    if (addr === firstAddr.current) return;
    firstAddr.current = addr;
    const active = document.activeElement as HTMLElement | null;
    if (active?.classList.contains('fn-hist') && !(active as HTMLButtonElement).disabled) return;   // repeated Back/Forward
    if (!active || active === document.body || paneRef.current?.contains(active)) headRef.current?.focus();
  }, [addr]);
  const h = shown?.header;
  useEffect(() => {
    if (h) setAnnounce(`Function ${h.name} at ${h.addr}.`);
  }, [h]);

  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    const inField = ['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName);
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    } else if (e.key === 'F2' && !inField && h) {
      e.preventDefault();
      setEditing('name');
    } else if (e.altKey && e.key === 'ArrowLeft') {
      e.preventDefault();
      if (history.canBack) history.back();
      else setAnnounce('No earlier function in this pane.');
    } else if (e.altKey && e.key === 'ArrowRight') {
      e.preventDefault();
      if (history.canForward) history.forward();
      else setAnnounce('No later function in this pane.');
    }
  };
  return (
    <section ref={paneRef} className="fn-pane card" aria-labelledby="fn-pane-h" onKeyDown={onKey} data-testid="fn-pane">
      <div className="row fn-pane-head">
        {editing === 'name' && h ? (
          <ValueEditor
            caseId={caseId}
            moduleId={moduleId}
            what="name"
            label={`New name for the function at ${h.addr}`}
            initial={h.name}
            onSave={async (name) => {
              const r = await editApi.rename(caseId, moduleId, { kind: 'function', address: h.addr, new_name: name });
              return `Renamed ${h.name} to ${name} (revision ${r.revision}).`;
            }}
            onDone={closeEditor('name')}
            testId="fn-name-edit"
          />
        ) : (
          <h3 id="fn-pane-h" ref={headRef} tabIndex={-1} className="mono wrap-any" data-testid="fn-pane-name">
            {h?.name ?? addr}
          </h3>
        )}
        <div className="row fn-pane-actions">
          <button type="button" className="btn sm fn-hist" onClick={history.back} disabled={!history.canBack} data-tooltip={history.canBack ? 'Alt+Left' : 'No earlier function opened in this pane'} aria-label="Back to the previous function (Alt+Left)" aria-keyshortcuts="Alt+ArrowLeft" data-testid="fn-back">
            ‹ Back
          </button>
          <button type="button" className="btn sm fn-hist" onClick={history.forward} disabled={!history.canForward} data-tooltip={history.canForward ? 'Alt+Right' : 'No later function: go Back first'} aria-label="Forward to the next function (Alt+Right)" aria-keyshortcuts="Alt+ArrowRight" data-testid="fn-forward">
            Forward ›
          </button>
          <button type="button" className="btn sm" onClick={onClose} aria-label="Close the function pane (Escape)" data-testid="fn-pane-close">
            ✕ Close
          </button>
        </div>
      </div>
      <div className="sr-only" role="status" aria-live="polite" data-testid="fn-pane-announce">
        {announce}
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
          {shown?.annotations?.function?.prov?.origin && (
            <>
              <dt>Name from</dt>
              <dd className="small" data-testid="fn-pane-name-from">
                <NameFrom prov={shown.annotations.function.prov} />
              </dd>
            </>
          )}
        </dl>
      )}
      {h && (
        <div className="stack fn-edit-zone">
          <div className="row fn-edit-bar" role="group" aria-label="Edit this function" data-testid="fn-edit-bar">
            <button type="button" className="btn sm" ref={(el) => { editBtn.current.name = el; }} onClick={() => setEditing('name')} aria-keyshortcuts="F2" data-tooltip="F2" data-testid="fn-edit-name">
              Edit name
            </button>
            <button type="button" className="btn sm" ref={(el) => { editBtn.current.prototype = el; }} onClick={() => setEditing('prototype')} data-testid="fn-edit-proto">
              Edit prototype
            </button>
            <button type="button" className="btn sm" ref={(el) => { editBtn.current.comment = el; }} onClick={() => setEditing('comment')} data-testid="fn-edit-comment">
              {headComment(shown, h.addr) ? 'Edit comment' : 'Add comment'}
            </button>
            <button type="button" className="btn sm" ref={(el) => { editBtn.current.types = el; }} onClick={() => setTypesOpen(true)} aria-haspopup="dialog" data-testid="fn-edit-types">
              Types…
            </button>
          </div>
          {editing === 'prototype' && (
            <ValueEditor
              caseId={caseId}
              moduleId={moduleId}
              what="prototype"
              label={`C prototype of ${h.name}`}
              initial={(h.signature ?? `int ${h.name.replace(/[^A-Za-z0-9_]/g, '_')}(void)`).replace(/;\s*$/, '')}
              hint="The name in the prototype becomes the function's name. Declare structs first under Types."
              onSave={async (proto) => {
                const r = await editApi.setType(caseId, moduleId, { kind: 'function', address: h.addr, prototype: proto });
                return `Set the prototype to ${proto} (revision ${r.revision}).`;
              }}
              onDone={closeEditor('prototype')}
              testId="fn-proto-edit"
            />
          )}
          {editing === 'comment' && (
            <CommentEditor caseId={caseId} moduleId={moduleId} addr={h.addr} initial={headComment(shown, h.addr)} onDone={closeEditor('comment')} testId="fn-head-comment" />
          )}
          <VariablesPanel caseId={caseId} moduleId={moduleId} fnAddr={h.addr} variables={shown?.variables ?? []} onEdited={edited} />
          <div className="small fn-status" role="status" aria-live="polite" data-testid="fn-edit-status">
            {status}
          </div>
          <TypesDialog caseId={caseId} moduleId={moduleId} open={typesOpen} onClose={() => { setTypesOpen(false); returnFocus.current = 'types'; }} onEdited={edited} />
        </div>
      )}
      <LocalTabs<CodeView>
        label="Code views"
        value={view}
        onChange={onView}
        tabs={[
          { id: 'decompiled', label: 'Decompiled' },
          { id: 'disassembly', label: 'Disassembly' },
          { id: 'references', label: 'References' },
          { id: 'callgraph', label: 'Call graph' },
          { id: 'history', label: 'History' },
        ]}
      />
      <div role="tabpanel" id={`panel-${view}`} aria-labelledby={`tab-${view}`} className="fn-panel">
        {view === 'decompiled' ? (
          <DecompiledView key={`${addr}|${decompiler}`} caseId={caseId} moduleId={moduleId} addr={addr} decompiler={decompiler} decompilers={shown?.decompilers} onDecompiler={onDecompiler} tick={editTick} />
        ) : view === 'disassembly' ? (
          <DisassemblyView key={addr} caseId={caseId} moduleId={moduleId} addr={addr} tick={editTick} onEdited={edited} />
        ) : view === 'references' ? (
          <ReferencesView key={`${addr}|${editTick}`} caseId={caseId} moduleId={moduleId} addr={addr} nav={nav} />
        ) : view === 'callgraph' ? (
          <CallGraphView key={`${addr}|${editTick}`} caseId={caseId} moduleId={moduleId} addr={addr} nav={nav} settings={graph} onSettings={setGraph} />
        ) : (
          <HistoryView caseId={caseId} moduleId={moduleId} fnAddr={h?.addr ?? addr} tick={editTick} onEdited={edited} />
        )}
      </div>
      <p className="xs muted">
        Names, code, strings and comments here come from the analysed program and are shown as plain text. Escape closes this pane (or the open editor); Alt+Left and Alt+Right go back and
        forward between the functions opened in it; F2 renames the function. Your edits are recorded as made by a person; History lists every change with Undo and Roll back.
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

function DecompiledView({ caseId, moduleId, addr, decompiler, decompilers, onDecompiler, tick = 0 }: { caseId: string; moduleId: string; addr: string; decompiler: Decompiler; decompilers?: DecompilerInfo[]; onDecompiler: (d: Decompiler) => void; tick?: number }) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const res = useLoad<Decompiled>(`${moduleId}|${addr}|${decompiler}|${tick}`, () => re.decompiled(caseId, moduleId, addr, decompiler));
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

function DisassemblyView({ caseId, moduleId, addr, tick = 0, onEdited }: { caseId: string; moduleId: string; addr: string; tick?: number; onEdited: (m: string) => void }) {
  const api = useApi();
  const re = useMemo(() => reFunctionApi(api), [api]);
  const [offset, setOffset] = useState(0);
  const res = useLoad<DisasmPage>(`${moduleId}|${addr}|${offset}|${tick}`, () => re.disassembly(caseId, moduleId, addr, offset, DISASM_PAGE));
  const [commenting, setCommenting] = useState<string | null>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const back = useRef<string | null>(null);
  useEffect(() => {
    if (commenting || !back.current) return;
    const el = tableRef.current?.querySelector<HTMLElement>(`[data-note="${back.current}"]`);
    if (el) {
      el.focus();
      back.current = null;
    }
  });
  const onTableKey = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    const a = (e.target as HTMLElement).dataset.note;
    if (e.key === ';' && a) {
      e.preventDefault();
      setCommenting(a);
    }
  };
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
      <p className="xs muted">Comment on an instruction with its ✎ button (or press ; on that button). Comments are kept with the module and shown here and in the decompiled code.</p>
      <div className="table-wrap fn-disasm-wrap" tabIndex={0} aria-label="Disassembly">
        <table ref={tableRef} className="table fn-disasm mono small" data-testid="fn-disasm-table">
          <thead>
            <tr>
              <th scope="col">Address</th>
              <th scope="col" className="col-bytes">
                Bytes
              </th>
              <th scope="col">Instruction</th>
              <th scope="col" className="col-note">
                <span className="sr-only">Your comment</span>
              </th>
            </tr>
          </thead>
          <tbody onKeyDown={onTableKey}>
            {p.ops.map((o) => (
              <DisasmRow
                key={o.addr}
                o={o}
                editing={commenting === o.addr}
                onEdit={() => setCommenting(o.addr)}
                editor={
                  <CommentEditor
                    caseId={caseId}
                    moduleId={moduleId}
                    addr={o.addr}
                    initial={o.user_comment ?? ''}
                    testId="fn-op-comment"
                    onDone={(m) => {
                      back.current = o.addr;
                      setCommenting(null);
                      if (m) onEdited(m);
                    }}
                  />
                }
              />
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

function DisasmRow({ o, editing, onEdit, editor }: { o: DisasmOp; editing: boolean; onEdit: () => void; editor: ReactNode }) {
  const has = !!o.user_comment;
  return (
    <>
      <tr data-testid={`fn-op-${o.addr}`}>
        <td>{o.addr}</td>
        <td className="col-bytes muted wrap-any">{o.bytes}</td>
        <td className="wrap-any">
          {o.disasm}
          {(o.user_comment || o.comment) && <span className="muted"> ; {o.user_comment ?? o.comment}</span>}
          {o.user_comment && o.comment_status && <span className="chip warn">{o.comment_status}</span>}
        </td>
        <td className="col-note">
          <button type="button" className="btn sm fn-note-btn" data-note={o.addr} onClick={onEdit} aria-expanded={editing} aria-label={has ? `Edit the comment at ${o.addr}` : `Add a comment at ${o.addr}`} aria-keyshortcuts=";" data-testid={`fn-note-${o.addr}`}>
            ✎<span className="fn-note-label"> {has ? 'Edit' : 'Note'}</span>
          </button>
        </td>
      </tr>
      {editing && (
        <tr className="fn-note-row">
          <td colSpan={4}>{editor}</td>
        </tr>
      )}
    </>
  );
}

function NameFrom({ prov }: { prov: { origin?: string; status?: string } }) {
  if (prov.origin !== 'model') return <span className="chip outline">a person</span>;
  if (prov.status === 'proposed') return <span className="chip warn">model-proposed (accept or reject it under History)</span>;
  if (prov.status === 'confirmed') return <span className="chip ok">a model, confirmed by a test</span>;
  return <span className="chip ok">a model, accepted by a person</span>;
}

/** The comment stored at the function's own address (not the notes overlaid for model-proposed names). */
function headComment(d: FunctionDetail | undefined, addr: string): string {
  const c = d?.annotations?.comments?.find((x) => x.addr === addr && !/^\[model-proposed\] (function name|local) /.test(x.text));
  return c ? c.text.replace(/^\[model-proposed\] /, '') : '';
}
