import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';
import { Dialog } from '../../components/Dialog';
import { Loading } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { describeError } from '../../lib/api';
import { proposalTitle, type Proposal } from '../../lib/aiQuality';
import { CHECK_DEBOUNCE_MS, describeChange, reEditApi, whoLabel, type AnnotationsHistory, type CheckResult, type CheckWhat, type FunctionVariable } from '../../lib/re';
import { useApi } from '../../lib/store';
import { useLoad } from './useLoad';

// R6 piece 5: editing in the function pane. Every save goes to the controller's edit routes, which use the same workbench path and
// value grammar as the MCP rename / set_type / add_comment / apply_struct tools and record the change as made by a person. History,
// review of model proposals and roll back reuse the R5 routes. All program text is rendered as plain text.

type Check = { state: 'idle' | 'checking' | 'ok' | 'bad'; result?: CheckResult };

/** Validate as you type: the controller's own grammar (POST .../annotations/check), debounced; nothing is stored. */
export function useValueCheck(caseId: string, moduleId: string, what: CheckWhat, value: string, enabled = true): Check {
  const api = useApi();
  const re = useMemo(() => reEditApi(api), [api]);
  const [check, setCheck] = useState<Check>({ state: 'idle' });
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setCheck((c) => ({ state: 'checking', result: c.result }));
    const t = setTimeout(() => {
      re.check(caseId, moduleId, what, value).then(
        (result) => alive && setCheck({ state: result.ok ? 'ok' : 'bad', result }),
        () => alive && setCheck({ state: 'idle' }),       // the check is a convenience: saving still validates
      );
    }, CHECK_DEBOUNCE_MS);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [re, caseId, moduleId, what, value, enabled]);
  return check;
}

/** One inline editor: a field validated as you type, Enter (Ctrl+Enter in a multi-line field) saves, Escape cancels. */
export function ValueEditor({
  caseId,
  moduleId,
  what,
  label,
  initial,
  multiline,
  onSave,
  onDone,
  extra,
  testId,
  hint,
}: {
  caseId: string;
  moduleId: string;
  what: CheckWhat;
  label: string;
  initial: string;
  multiline?: boolean;
  /** stores the value; resolves to the sentence announced after saving */
  onSave: (value: string) => Promise<string>;
  /** closes the editor: a message after a save, null when cancelled */
  onDone: (message: string | null) => void;
  extra?: ReactNode;
  testId: string;
  hint?: string;
}) {
  const [value, setValue] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [local, setLocal] = useState<string | null>(null);
  const fieldRef = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const id = useId();
  const statusId = `${id}-status`;
  const check = useValueCheck(caseId, moduleId, what, multiline ? value : value.trim());
  useEffect(() => {
    fieldRef.current?.focus();
    fieldRef.current?.select();
  }, []);

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (saving) return;
    const v = multiline ? value : value.trim();
    if (v === initial) {
      onDone(null);
      return;
    }
    if (check.state === 'bad' && check.result && check.result.message) {
      setLocal(`Not saved: ${check.result.message}`);
      fieldRef.current?.focus();
      return;
    }
    setSaving(true);
    setError(null);
    setLocal(null);
    try {
      onDone(await onSave(v));
    } catch (err) {
      setError(err);
      setSaving(false);
      fieldRef.current?.focus();
    }
  };
  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();             // only the editor closes, not the pane
      onDone(null);
    } else if (multiline && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void submit();
    }
  };
  const r = check.result;
  const bad = check.state === 'bad';
  const Field = multiline ? 'textarea' : 'input';
  return (
    <form className="fn-edit stack" onSubmit={submit} onKeyDown={onKey} data-testid={testId} aria-label={label}>
      <label className="field">
        <span className="small">{label}</span>
        <Field
          ref={fieldRef}
          className={`mono${multiline ? ' fn-edit-area' : ''}`}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setLocal(null);
          }}
          aria-invalid={bad || undefined}
          aria-describedby={statusId}
          spellCheck={false}
          autoComplete="off"
          rows={multiline ? 8 : undefined}
          data-testid={`${testId}-input`}
          data-autofocus
        />
      </label>
      <div id={statusId} className={`small ${bad ? 'bad-text' : 'muted'}`} role="status" aria-live="polite" data-testid={`${testId}-check`}>
        {saving
          ? 'Saving…'
          : bad && r
            ? `${r.message} Example: ${r.example}`
            : check.state === 'ok' && r
              ? r.normalized && r.normalized !== (multiline ? value : value.trim()) && what !== 'declaration'
                ? `Valid; it will be saved as: ${r.normalized}`
                : what === 'declaration' && r.normalized
                  ? `Valid; declares ${r.normalized}.`
                  : 'Valid.'
              : check.state === 'checking'
                ? 'Checking…'
                : hint ?? ''}
      </div>
      {local && (
        <div className="callout bad small" role="alert" data-testid={`${testId}-refused`}>
          {local}
        </div>
      )}
      {error ? <SaveError error={error} testId={`${testId}-error`} /> : null}
      <div className="row fn-edit-actions">
        <button type="submit" className="btn primary sm" disabled={saving} aria-describedby={saving ? statusId : undefined} data-testid={`${testId}-save`}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="btn sm" onClick={() => onDone(null)} data-testid={`${testId}-cancel`}>
          Cancel
        </button>
        {extra}
        <span className="xs muted">{multiline ? 'Ctrl+Enter saves, Escape cancels.' : 'Enter saves, Escape cancels.'}</span>
      </div>
    </form>
  );
}

function SaveError({ error, testId }: { error: unknown; testId: string }) {
  const d = describeError(error);
  return (
    <div className="callout bad small" role="alert" data-testid={testId}>
      <div>
        <strong>Not saved: </strong>
        {d.what}
      </div>
      {d.next && (
        <div>
          <strong>Next: </strong>
          {d.next}
        </div>
      )}
    </div>
  );
}

/** Comment at one address: add, change or remove (empty text removes it too). */
export function CommentEditor({ caseId, moduleId, addr, initial, onDone, testId = 'fn-comment-edit' }: { caseId: string; moduleId: string; addr: string; initial: string; onDone: (m: string | null) => void; testId?: string }) {
  const api = useApi();
  const re = useMemo(() => reEditApi(api), [api]);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const remove = async () => {
    setRemoving(true);
    setError(null);
    try {
      const r = await re.comment(caseId, moduleId, addr, '');
      onDone(`Removed the comment at ${addr} (revision ${r.revision}).`);
    } catch (e) {
      setError(e);
      setRemoving(false);
    }
  };
  return (
    <div className="stack">
      <ValueEditor
        caseId={caseId}
        moduleId={moduleId}
        what="comment"
        label={`Comment at ${addr}`}
        initial={initial}
        hint="One line, at most 1,000 characters. Leave it empty to remove the comment."
        onSave={async (v) => {
          const r = await re.comment(caseId, moduleId, addr, v);
          return v ? `Saved the comment at ${addr} (revision ${r.revision}).` : `Removed the comment at ${addr} (revision ${r.revision}).`;
        }}
        onDone={onDone}
        testId={testId}
        extra={
          initial ? (
            <button type="button" className="btn sm" onClick={remove} disabled={removing} aria-describedby={removing ? `${testId}-removing` : undefined} data-testid={`${testId}-remove`}>
              {removing ? 'Removing…' : 'Remove comment'}
            </button>
          ) : null
        }
      />
      {removing && (
        <span id={`${testId}-removing`} className="sr-only">
          Removing the comment
        </span>
      )}
      {error ? <SaveError error={error} testId={`${testId}-remove-error`} /> : null}
    </div>
  );
}

/** The function's arguments and locals, each with Rename and Change type. */
export function VariablesPanel({ caseId, moduleId, fnAddr, variables, onEdited }: { caseId: string; moduleId: string; fnAddr: string; variables: FunctionVariable[]; onEdited: (m: string) => void }) {
  const api = useApi();
  const re = useMemo(() => reEditApi(api), [api]);
  const [editing, setEditing] = useState<{ name: string; mode: 'rename' | 'retype' } | null>(null);
  const backTo = useRef<string | null>(null);
  const rootRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (editing || !backTo.current) return;
    rootRef.current?.querySelector<HTMLElement>(`[data-focus-key="${backTo.current}"]`)?.focus();
    backTo.current = null;
  });
  const done = (key: string) => (m: string | null) => {
    backTo.current = key;
    setEditing(null);
    if (m) onEdited(m);
  };
  return (
    <details ref={rootRef} className="fn-vars" data-testid="fn-vars">
      <summary className="small">
        Variables ({variables.length}) <span className="muted">· rename or change a type</span>
      </summary>
      {variables.length === 0 ? (
        <p className="small muted">The analysis found no arguments or local variables in this function.</p>
      ) : (
        <ul className="list fn-var-list" aria-label="Variables">
          {variables.map((v) => {
            const isEdit = editing?.name === v.name;
            return (
              <li key={`${v.storage ?? ''}:${v.name}`} className="fn-var" data-testid={`fn-var-${v.name}`}>
                <div className="row fn-var-row">
                  <span className="mono wrap-any fn-var-name">{v.name}</span>
                  <span className="mono small wrap-any">{v.type ?? 'unknown type'}</span>
                  <span className="xs muted">{v.is_arg ? 'argument' : 'local'}{v.storage ? ` · ${v.storage}` : ''}</span>
                  <span className="row fn-var-actions">
                    <button type="button" className="btn sm" data-focus-key={`r:${v.name}`} onClick={() => setEditing({ name: v.name, mode: 'rename' })} aria-label={`Rename variable ${v.name}`} data-testid={`fn-var-rename-${v.name}`}>
                      Rename
                    </button>
                    <button type="button" className="btn sm" data-focus-key={`t:${v.name}`} onClick={() => setEditing({ name: v.name, mode: 'retype' })} aria-label={`Change the type of ${v.name}`} data-testid={`fn-var-retype-${v.name}`}>
                      Change type
                    </button>
                  </span>
                </div>
                {isEdit && editing!.mode === 'rename' && (
                  <ValueEditor
                    caseId={caseId}
                    moduleId={moduleId}
                    what="name"
                    label={`New name for ${v.name}`}
                    initial={v.name}
                    onSave={async (n) => {
                      const r = await re.rename(caseId, moduleId, { kind: 'local', address: fnAddr, variable: v.name, new_name: n });
                      return `Renamed ${v.name} to ${n} (revision ${r.revision}).`;
                    }}
                    onDone={done(`r:${v.name}`)}
                    testId="fn-var-edit"
                  />
                )}
                {isEdit && editing!.mode === 'retype' && (
                  <ValueEditor
                    caseId={caseId}
                    moduleId={moduleId}
                    what="type"
                    label={`C type of ${v.name}`}
                    initial={v.type ?? ''}
                    hint="For example int, uint32_t, char * or struct point * (declare a struct first under Types)."
                    onSave={async (t) => {
                      const r = await re.setType(caseId, moduleId, { kind: 'local', address: fnAddr, variable: v.name, type: t });
                      return `Set the type of ${v.name} to ${t} (revision ${r.revision}).`;
                    }}
                    onDone={done(`t:${v.name}`)}
                    testId="fn-var-edit"
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </details>
  );
}

/** Paste a C declaration (struct / union / enum / typedef) and apply it to the module. */
export function TypesDialog({ caseId, moduleId, open, onClose, onEdited }: { caseId: string; moduleId: string; open: boolean; onClose: () => void; onEdited: (m: string) => void }) {
  const api = useApi();
  const re = useMemo(() => reEditApi(api), [api]);
  const [tick, setTick] = useState(0);
  const declared = useLoad<AnnotationsHistory | null>(open ? `${moduleId}|types|${tick}` : 'closed', () => (open ? re.history(caseId, moduleId) : Promise.resolve(null)));
  const types = declared.data?.annotations?.types ?? [];
  return (
    <Dialog open={open} title="Types" onClose={onClose} describedBy="fn-types-help">
      <p id="fn-types-help" className="small">
        Paste a C declaration: struct, union, enum or typedef, without # lines, comments or quotes. Once applied, its names can be used as variable types and in
        prototypes in this module. It is kept with the module and applied again after every analysis.
      </p>
      <ValueEditor
        caseId={caseId}
        moduleId={moduleId}
        what="declaration"
        label="C declaration"
        initial=""
        multiline
        hint="For example: struct point { int x; int y; };"
        onSave={async (d) => {
          const r = await re.declare(caseId, moduleId, d);
          const names = (r.change.names ?? []).map((n) => `${n.kind} ${n.name}`).join(', ');
          setTick((t) => t + 1);
          return `Declared ${names || 'the types'} (revision ${r.revision}).`;
        }}
        onDone={(m) => {
          if (m) onEdited(m);
          onClose();
        }}
        testId="fn-types-edit"
      />
      <h3 className="fn-sub-h small">Declared in this module</h3>
      {declared.error ? (
        <ErrorCallout error={declared.error} title="The declared types could not be loaded" onRetry={declared.reload} />
      ) : !declared.data ? (
        <Loading what="declared types" />
      ) : types.length === 0 ? (
        <p className="small muted" data-testid="fn-types-none">
          None yet.
        </p>
      ) : (
        <ul className="list small" aria-label="Declared types" data-testid="fn-types-list">
          {types.map((t) => (
            <li key={t.sha256}>
              <span className="mono">{t.names.map((n) => `${n.kind} ${n.name}`).join(', ')}</span>
            </li>
          ))}
        </ul>
      )}
    </Dialog>
  );
}

/** History of the module's annotations: who made each revision, Undo last change, Roll back to revision N, and Accept / Reject for
 * open model proposals (the R5 review and rollback routes). Nothing is ever deleted: undo and roll back store a new revision. */
export function HistoryView({ caseId, moduleId, fnAddr, tick, onEdited }: { caseId: string; moduleId: string; fnAddr: string; tick: number; onEdited: (m: string) => void }) {
  const api = useApi();
  const re = useMemo(() => reEditApi(api), [api]);
  const res = useLoad<AnnotationsHistory>(`${moduleId}|history|${tick}`, () => re.history(caseId, moduleId));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [note, setNote] = useState('');
  const act = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(key);
    setError(null);
    setNote('');
    try {
      await fn();
      onEdited(done);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  if (res.error) return <ErrorCallout error={res.error} title="The history could not be loaded" onRetry={res.reload} />;
  const d = res.data;
  if (!d) return <Loading what="annotation history" />;
  const hist = d.history ?? [];
  const newest = hist.at(-1);
  const rows = hist.slice().reverse();
  const open = d.proposals.filter((p) => p.status === 'proposed');
  const mine = (p: Proposal) => p.key === fnAddr;
  const sorted = [...open.filter(mine), ...open.filter((p) => !mine(p))];
  const undo = () => {
    if (!newest) {
      setNote('Nothing to undo yet: this module has no annotation changes.');
      return;
    }
    const target = hist.length >= 2 ? hist[hist.length - 2].revision : 0;
    void act('undo', () => re.rollback(caseId, moduleId, target), `Undid “${describeChange(newest.change)}” (rolled back to revision ${target}).`);
  };
  const busyNote = busy ? 'Working on the previous request…' : undefined;
  return (
    <div
      className="stack"
      data-testid="fn-history"
      onKeyDown={(e) => {
        const t = e.target as HTMLElement;
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) {
          e.preventDefault();
          if (!busy) undo();
        }
      }}
    >
      <div className="row fn-code-bar">
        <span className="small" data-testid="fn-history-summary">
          {hist.length === 0 ? 'No changes yet in this module.' : `Revision ${d.revision} is current. ${n(hist.length)} change${hist.length === 1 ? '' : 's'} shown, newest first.`}
        </span>
        <button type="button" className="btn sm" onClick={undo} disabled={busy != null} data-tooltip={busyNote} aria-keyshortcuts="Control+Z" data-testid="fn-undo">
          {busy === 'undo' ? 'Undoing…' : 'Undo last change'}
        </button>
      </div>
      <p className="xs muted">Undo and roll back never delete anything: they store a new revision with the earlier state, so they can be undone too. Ctrl+Z in this view undoes the last change.</p>
      {note && (
        <p className="small" role="status" data-testid="fn-history-note">
          {note}
        </p>
      )}
      {error ? <SaveError error={error} testId="fn-history-error" /> : null}
      <section aria-labelledby="fn-proposals-h" className="stack">
        <h4 id="fn-proposals-h" className="fn-sub-h small">
          Waiting for review ({open.length})
        </h4>
        {open.length === 0 ? (
          <p className="small muted" data-testid="fn-proposals-none">
            No open model proposals in this module.
          </p>
        ) : (
          <ul className="list small" aria-label="Open model proposals" data-testid="fn-proposals">
            {sorted.map((p) => {
              const k = `${p.kind}:${p.key}:${p.local ?? ''}`;
              const title = proposalTitle(p);
              const was = p.replaced?.name ?? p.replaced?.text ?? p.analysis_name;
              return (
                <li key={k} className="row fn-proposal" data-testid={`fn-proposal-${p.kind}-${p.key}${p.local ? `-${p.local}` : ''}`}>
                  <span className="wrap-any" style={{ flex: 1 }}>
                    <strong>{title}:</strong> <span className="mono">{p.value}</span>
                    {was ? <span className="muted"> (was {was})</span> : null} <span className="chip warn">model-proposed</span>
                    {p.model ? <span className="muted"> · {p.model}</span> : null}
                    {mine(p) ? <span className="muted"> · this function</span> : null}
                  </span>
                  <button type="button" className="btn sm" disabled={busy != null} data-tooltip={busyNote} onClick={() => act(`a:${k}`, () => re.review(caseId, moduleId, p, 'accept'), `Accepted ${title}: ${p.value}.`)} aria-label={`Accept ${title} ${p.value}`}>
                    Accept
                  </button>
                  <button type="button" className="btn sm" disabled={busy != null} data-tooltip={busyNote} onClick={() => act(`r:${k}`, () => re.review(caseId, moduleId, p, 'reject'), `Rejected ${title}: ${p.value}.`)} aria-label={`Reject ${title} ${p.value}`}>
                    Reject
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
      <section aria-labelledby="fn-revisions-h" className="stack">
        <h4 id="fn-revisions-h" className="fn-sub-h small">
          Revisions
        </h4>
        {rows.length === 0 ? (
          <p className="small muted">Renames, types, comments and declarations you or a model make appear here.</p>
        ) : (
          <ol className="list small fn-revisions" aria-label="Annotation revisions, newest first" data-testid="fn-revisions">
            {rows.map((h) => {
              const who = whoLabel(h.who ?? (h.change?.author === 'model' ? 'model-proposed' : h.change?.author === 'test' ? 'confirmed by test' : 'person'));
              const current = h.revision === newest?.revision;
              return (
                <li key={h.evidence_id} className="row fn-revision" data-testid={`fn-rev-${h.revision}`}>
                  <span className="wrap-any" style={{ flex: 1 }}>
                    <strong>Revision {h.revision}</strong>
                    {current && <span className="muted"> (current)</span>} · <span className={`chip ${who.tone}`}>{who.text}</span> <span>{describeChange(h.change)}</span>
                    {h.change?.at ? <span className="xs muted"> · {h.change.at.replace('T', ' ').slice(0, 19)}</span> : null}
                  </span>
                  {current ? (
                    <span className="xs muted">current state</span>
                  ) : (
                    <button type="button" className="btn sm" disabled={busy != null} data-tooltip={busyNote} onClick={() => act(`rb:${h.revision}`, () => re.rollback(caseId, moduleId, h.revision), `Rolled back to revision ${h.revision}.`)} data-testid={`fn-rollback-${h.revision}`}>
                      Roll back to revision {h.revision}
                    </button>
                  )}
                </li>
              );
            })}
            <li className="row fn-revision" data-testid="fn-rev-0">
              <span style={{ flex: 1 }}>
                <strong>Revision 0</strong> · <span className="muted">before any change (the analysis names and types)</span>
              </span>
              <button type="button" className="btn sm" disabled={busy != null} data-tooltip={busyNote} onClick={() => act('rb:0', () => re.rollback(caseId, moduleId, 0), 'Rolled back to before any change.')} data-testid="fn-rollback-0">
                Roll back to revision 0
              </button>
            </li>
          </ol>
        )}
      </section>
    </div>
  );
}

const n = (v: number) => v.toLocaleString('en-US');

