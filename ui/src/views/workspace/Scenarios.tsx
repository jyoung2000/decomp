import { useEffect, useId, useState, type FormEvent } from 'react';
import { ConsentCard, OriginalRunExplainer, useIsolation } from '../../components/ConsentCard';
import { ConfirmDialog } from '../../components/Dialog';
import { Empty } from '../../components/Empty';
import { ErrorCallout } from '../../components/ErrorCallout';
import { useOutcome } from '../../components/Outcome';
import { useToast } from '../../components/Toasts';
import { values } from '../../lib/derive';
import { emptyScenarioForm, formToBody, scenarioToForm, STATUS_TEXT, summarize, validateScenarioForm, type ScenarioFieldError, type ScenarioForm } from '../../lib/scenarios';
import { useApi, useCaseState, useResource, useStore } from '../../lib/store';
import type { UserScenario } from '../../lib/types';

/** Why a button is unavailable, announced through aria-describedby (no silent disabled controls). */
function Why({ id, children }: { id: string; children: string }) {
  return (
    <span className="xs muted" id={id}>
      {children}
    </span>
  );
}

export function ScenariosTab({ caseId }: { caseId: string }) {
  const api = useApi();
  const store = useStore();
  const toast = useToast();
  const cs = useCaseState(caseId);
  const outcome = useOutcome(caseId);
  const isolation = useIsolation();
  const [tick, setTick] = useState(0);
  const reload = () => setTick((t) => t + 1);
  useEffect(
    () => store.onControllerEvent((ev) => ev.case_id === caseId && ['scenarios.recorded', 'verification.completed', 'verification.invalidated', 'case.consent'].includes(ev.kind) && reload()),
    [store, caseId],
  );
  const list = useResource(() => api.scenarios(caseId), [api, caseId], tick);
  const [editing, setEditing] = useState<'new' | string | null>(null);
  const [form, setForm] = useState<ScenarioForm>(emptyScenarioForm);
  const [errors, setErrors] = useState<ScenarioFieldError[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<unknown>(null);
  const [recordError, setRecordError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<UserScenario | null>(null);
  const [askConsent, setAskConsent] = useState(false);
  const ids = { why: useId(), recWhy: useId(), addWhy: useId() };

  if (!cs?.case) return <Empty title="Loading scenarios…" />;
  const web = cs.case.value.launch_profile?.kind === 'web';
  const features = values(cs.features);
  const data = list.data;
  const rows = data?.scenarios ?? [];
  const counts = data?.counts;
  const allowed = !!data?.consent.allowed;
  const todo = rows.filter((s) => s.status === 'no_baseline' || s.status === 'changed');
  const v = outcome?.verification;

  const setField = <K extends keyof ScenarioForm>(k: K, val: ScenarioForm[K]) => {
    const next = { ...form, [k]: val };
    setForm(next);
    if (submitted) setErrors(validateScenarioForm(next));
  };
  const setStep = (i: number, patch: Partial<ScenarioForm['steps'][number]>) => setField('steps', form.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const err = (f: string) => errors.find((e) => e.field === f);
  const errText = (f: string) => {
    const e = err(f);
    return e ? `${e.what} ${e.next}` : null;
  };

  const startNew = () => {
    setForm(emptyScenarioForm());
    setErrors([]);
    setSubmitted(false);
    setServerError(null);
    setEditing('new');
  };
  const startEdit = (s: UserScenario) => {
    setForm(scenarioToForm(s));
    setErrors([]);
    setSubmitted(false);
    setServerError(null);
    setEditing(s.scenario_id);
  };

  const save = async (ev: FormEvent) => {
    ev.preventDefault();
    setSubmitted(true);
    setServerError(null);
    const errs = validateScenarioForm(form);
    setErrors(errs);
    if (errs.length) return;
    setBusy('save');
    try {
      if (editing === 'new') await api.createScenario(caseId, formToBody(form));
      else if (editing) await api.updateScenario(caseId, editing, formToBody(form));
      toast.success('Scenario saved', editing === 'new' ? 'Record the original’s behaviour so it can be compared.' : 'If it was recorded before, record it again so the new steps are used.');
      setEditing(null);
      reload();
    } catch (e) {
      setServerError(e);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (s: UserScenario) => {
    try {
      await api.deleteScenario(caseId, s.scenario_id);
      toast.success('Scenario deleted', 'Behaviour already recorded stays in the frozen baseline until you record again.');
      reload();
    } catch (e) {
      toast.error('Could not delete the scenario', e);
    }
  };

  const record = async () => {
    setRecordError(null);
    setBusy('record');
    try {
      const r = await api.recordScenarios(caseId, todo.map((s) => s.scenario_id));
      toast.success(`Recorded ${r.recorded.length} scenario${r.recorded.length === 1 ? '' : 's'}`, `Saved as baseline revision ${r.baseline_revision}. The earlier baseline is kept unchanged.`);
      reload();
      void store.refresh(caseId, 'case');
    } catch (e) {
      setRecordError(e);
      reload();
    } finally {
      setBusy(null);
    }
  };
  const onRecordClick = () => (allowed ? void record() : setAskConsent(true));
  const grantAndRecord = async () => {
    try {
      await api.setConsent(caseId, true, 'Allowed from the Scenarios tab');
    } catch (e) {
      toast.error('Could not record the permission', e);
      return;
    }
    reload();
    await record();
  };

  const recordWhy = web
    ? 'Scenarios can only be authored for command-line programs so far.'
    : rows.length === 0
      ? 'Add a scenario first.'
      : todo.length === 0
        ? 'Every scenario already has recorded behaviour. Edit one, or add a new one, to record again.'
        : busy === 'record'
          ? 'Recording is in progress; this can take up to the time limit of each scenario.'
          : null;

  return (
    <div className="stack-lg" data-testid="scenarios">
      <div>
        <h2>Scenarios</h2>
        <section className="card" aria-label="What is a scenario" data-testid="scenario-help">
          <p>
            <strong>A scenario is one thing you want the rebuilt program to keep doing exactly like the original.</strong> You describe how to run the program (what to type after its name, and anything to type into it); Rebuild Studio runs the <em>original</em> once to record what happens, and later runs the rebuilt version the same way and compares.
          </p>
          <p className="small">
            <strong>Example:</strong> a note-keeping program. Step 1: <span className="mono">init {'{work}'}/notes.dat</span>. Step 2: <span className="mono">add {'{work}'}/notes.dat shopping milk</span>. Step 3: <span className="mono">get {'{work}'}/notes.dat shopping</span>. Compare the printed text and the exit code: the rebuilt program must print “milk” and finish with the same result.
          </p>
          <p className="small muted">
            <span className="mono">{'{work}'}</span> stands for a private scratch folder created for each run, so scenarios never touch your own files. Only the behaviour you list here is checked; anything else is not measured.
          </p>
        </section>
      </div>

      <section className="card" aria-label="Scenario counts" data-testid="scenario-counts">
        <div className="card-head">
          <h3>Counts</h3>
        </div>
        <dl className="kv small">
          <dt>Your scenarios</dt>
          <dd data-testid="count-declared">{counts?.declared ?? 0}</dd>
          <dt>Recorded from the original</dt>
          <dd data-testid="count-recorded">{counts?.recorded ?? 0}</dd>
          <dt>Passed on the current build</dt>
          <dd data-testid="count-passed">{counts?.passed ?? 0}</dd>
          <dt>Failed on the current build</dt>
          <dd data-testid="count-failed">{counts?.failed ?? 0}</dd>
          <dt>Not run yet</dt>
          <dd data-testid="count-not-run">{(counts?.not_run ?? 0) + (counts?.no_baseline ?? 0) + (counts?.changed ?? 0)}</dd>
        </dl>
        {v && (
          <p className="small muted" data-testid="outcome-counts">
            Whole project (same numbers as the Outcome panel, which also counts scenarios that came from other sources): {v.declared} declared, {v.passed} passed, {v.failed} failed, {v.untested} not run.
          </p>
        )}
      </section>

      <ConsentCard caseId={caseId} onChanged={reload} />

      {recordError != null && <ErrorCallout error={recordError} title="The original’s behaviour was not recorded" />}
      {list.error && !data ? <ErrorCallout error={list.error} title="Scenarios could not be loaded" onRetry={reload} /> : null}

      {web && (
        <div className="callout info" role="note" data-testid="web-not-supported">
          <div className="ttl">Scenarios for web apps are not available yet</div>
          <div>You can write scenarios for command-line programs here. Web projects use the scenarios chosen when the project was created.</div>
        </div>
      )}

      <section className="stack" aria-label="Your scenarios">
        <div className="row between">
          <h3>Your scenarios</h3>
          <div className="row">
            <button type="button" className="btn sm" onClick={startNew} disabled={web} aria-describedby={web ? ids.addWhy : undefined} data-testid="scenario-add">
              Add scenario
            </button>
            {web && <Why id={ids.addWhy}>Not available for web projects yet.</Why>}
            <button type="button" className="btn primary sm" onClick={onRecordClick} disabled={!!recordWhy} aria-describedby={recordWhy ? ids.recWhy : undefined} data-testid="scenario-record">
              {busy === 'record' ? 'Recording…' : 'Record original behaviour'}
            </button>
          </div>
        </div>
        {recordWhy && <Why id={ids.recWhy}>{recordWhy}</Why>}
        {!allowed && !recordWhy && <p className="xs muted">Recording runs the original program, so Rebuild Studio will ask for your permission first.</p>}

        {rows.length === 0 && !list.loading && <Empty title="No scenarios yet">Use “Add scenario” to describe one thing the rebuilt program must keep doing.</Empty>}
        <ul className="list" data-testid="scenario-list">
          {rows.map((s) => {
            const st = STATUS_TEXT[s.status];
            const hintId = `sc-hint-${s.scenario_id}`;
            return (
              <li key={s.scenario_id} data-testid={`scenario-${s.scenario_id}`}>
                <div className="row between">
                  <div className="stack" style={{ gap: 2, minWidth: 0 }}>
                    <strong>{s.title}</strong>
                    <span className="mono small wrap-any">{summarize(s)}</span>
                    <span className="xs muted" id={hintId}>
                      {st.hint}
                    </span>
                  </div>
                  <div className="row">
                    <span className={`chip ${st.tone}`} data-status={s.status} data-testid="scenario-status" aria-describedby={hintId}>
                      {st.label}
                    </span>
                    <button type="button" className="btn sm" onClick={() => startEdit(s)} aria-label={`Edit scenario ${s.title}`}>
                      Edit
                    </button>
                    <button type="button" className="btn sm danger" onClick={() => setToDelete(s)} aria-label={`Delete scenario ${s.title}`}>
                      Delete
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      {editing && (
        <form className="card form stack-lg" onSubmit={save} noValidate aria-label="Scenario editor" data-testid="scenario-form">
          <h3>{editing === 'new' ? 'New scenario' : 'Edit scenario'}</h3>
          {serverError != null && <ErrorCallout error={serverError} title="The scenario was not saved" />}
          {errors.length > 0 && (
            <div className="callout bad" role="alert" data-testid="scenario-errors">
              <div className="ttl">{errors.length === 1 ? 'One thing needs attention' : `${errors.length} things need attention`}</div>
              <ul className="bullets">
                {errors.map((e, i) => (
                  <li key={i}>
                    {e.what} {e.next}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {editing !== 'new' && rows.find((r) => r.scenario_id === editing)?.status !== 'no_baseline' && (
            <p className="small muted">This scenario was recorded before. Saving does not change the recorded behaviour; record it again afterwards so the new steps are used.</p>
          )}
          <div className="field">
            <label htmlFor="sc-title">Title *</label>
            <input id="sc-title" type="text" value={form.title} onChange={(e) => setField('title', e.target.value)} aria-invalid={!!err('title')} aria-describedby={err('title') ? 'sc-title-err' : undefined} />
            {err('title') && (
              <span className="field-error" id="sc-title-err">
                {errText('title')}
              </span>
            )}
          </div>
          <div className="field">
            <label htmlFor="sc-feature">Feature this checks (optional)</label>
            <select id="sc-feature" value={form.feature_id} onChange={(e) => setField('feature_id', e.target.value)} aria-describedby="sc-feature-hint">
              <option value="">None in particular</option>
              {features.map((f) => (
                <option key={f.feature_id} value={f.feature_id}>
                  {f.title}
                </option>
              ))}
            </select>
            <span className="hint" id="sc-feature-hint">
              Linking a feature lets the feature list show whether it is verified.
            </span>
          </div>

          <fieldset>
            <legend>Steps (run in order, in the same scratch folder)</legend>
            <div className="stack-lg">
              {form.steps.map((s, i) => (
                <div className="stack" key={i} role="group" aria-label={`Step ${i + 1}`}>
                  <div className="field">
                    <label htmlFor={`sc-args-${i}`}>Command-line arguments (step {i + 1})</label>
                    <input id={`sc-args-${i}`} type="text" value={s.args} spellCheck={false} placeholder='add {work}/notes.dat shopping "two words"' onChange={(e) => setStep(i, { args: e.target.value })} aria-invalid={!!err(`args_${i}`)} aria-describedby={`sc-args-hint-${i}${err(`args_${i}`) ? ` sc-args-err-${i}` : ''}`} />
                    <span className="hint" id={`sc-args-hint-${i}`}>
                      What you would type after the program’s name. Put values with spaces in double quotes.
                    </span>
                    {err(`args_${i}`) && (
                      <span className="field-error" id={`sc-args-err-${i}`}>
                        {errText(`args_${i}`)}
                      </span>
                    )}
                  </div>
                  <div className="field">
                    <label htmlFor={`sc-stdin-${i}`}>What to type into the program (step {i + 1})</label>
                    <textarea id={`sc-stdin-${i}`} value={s.stdin} spellCheck={false} onChange={(e) => setStep(i, { stdin: e.target.value })} aria-invalid={!!err(`stdin_${i}`)} aria-describedby={`sc-stdin-hint-${i}`} />
                    <span className="hint" id={`sc-stdin-hint-${i}`}>
                      Optional. Text the program reads while it runs, as if you typed it and pressed Enter. Leave empty if it asks for nothing.
                    </span>
                    {err(`stdin_${i}`) && <span className="field-error">{errText(`stdin_${i}`)}</span>}
                  </div>
                  {form.steps.length > 1 && (
                    <div>
                      <button type="button" className="btn sm danger" onClick={() => setField('steps', form.steps.filter((_, j) => j !== i))} aria-label={`Remove step ${i + 1}`}>
                        Remove step {i + 1}
                      </button>
                    </div>
                  )}
                </div>
              ))}
              <div>
                <button type="button" className="btn sm" onClick={() => setField('steps', [...form.steps, { args: '', stdin: '' }])}>
                  Add another step
                </button>
              </div>
              {err('steps') && <span className="field-error">{errText('steps')}</span>}
            </div>
          </fieldset>

          <fieldset aria-describedby={err('compare') ? 'sc-compare-err' : undefined}>
            <legend>Compare</legend>
            <div className="stack">
              <label className="check">
                <input type="checkbox" checked={form.exit_code} onChange={(e) => setField('exit_code', e.target.checked)} /> Exit code (the number that says whether it succeeded)
              </label>
              <label className="check">
                <input type="checkbox" checked={form.output} onChange={(e) => setField('output', e.target.checked)} /> Printed text (what it shows, including error messages)
              </label>
              <label className="check">
                <input type="checkbox" checked={form.files} onChange={(e) => setField('files', e.target.checked)} /> Files it writes (names and exact contents in the scratch folder)
              </label>
              {err('compare') && (
                <span className="field-error" id="sc-compare-err">
                  {errText('compare')}
                </span>
              )}
            </div>
          </fieldset>

          <fieldset aria-describedby="sc-norm-hint">
            <legend>Ignore these differences when comparing printed text</legend>
            <div className="stack">
              <span className="hint" id="sc-norm-hint">
                These apply to printed text only. File contents are always compared exactly.
              </span>
              <label className="check">
                <input type="checkbox" checked={form.line_endings} onChange={(e) => setField('line_endings', e.target.checked)} /> Line-ending differences (Windows vs other systems)
              </label>
              <label className="check">
                <input type="checkbox" checked={form.trailing_spaces} onChange={(e) => setField('trailing_spaces', e.target.checked)} /> Extra spaces at the end of lines
              </label>
              <label className="check">
                <input type="checkbox" checked={form.trim} onChange={(e) => setField('trim', e.target.checked)} /> Blank space before and after the whole text
              </label>
              <label className="check">
                <input type="checkbox" checked={form.ignore_timestamps} onChange={(e) => setField('ignore_timestamps', e.target.checked)} /> Dates and times (for programs that print the current time)
              </label>
            </div>
          </fieldset>

          <div className="field" style={{ maxWidth: 260 }}>
            <label htmlFor="sc-timeout">Time limit per run (seconds)</label>
            <input id="sc-timeout" type="number" inputMode="numeric" value={form.timeout} onChange={(e) => setField('timeout', e.target.value)} aria-invalid={!!err('timeout')} aria-describedby={err('timeout') ? 'sc-timeout-err' : undefined} />
            {err('timeout') && (
              <span className="field-error" id="sc-timeout-err">
                {errText('timeout')}
              </span>
            )}
          </div>

          <div className="row">
            <button type="submit" className="btn primary" disabled={busy === 'save'} aria-describedby={busy === 'save' ? ids.why : undefined} data-testid="scenario-save">
              {busy === 'save' ? 'Saving…' : 'Save scenario'}
            </button>
            {busy === 'save' && <Why id={ids.why}>Saving, please wait.</Why>}
            <button type="button" className="btn" onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      <ConfirmDialog
        open={!!toDelete}
        title="Delete this scenario?"
        body={<p>“{toDelete?.title}” will be removed from your list. If its behaviour was already recorded, that record stays in the frozen baseline until you record again.</p>}
        confirmLabel="Delete scenario"
        danger
        onConfirm={() => toDelete && void remove(toDelete)}
        onClose={() => setToDelete(null)}
      />
      <ConfirmDialog
        open={askConsent}
        title="Allow Rebuild Studio to run the original program?"
        body={
          <div className="stack">
            <p>Recording runs the original program once for each scenario. It needs your permission, which is remembered for this project and can be revoked at any time.</p>
            <OriginalRunExplainer isolation={isolation} />
          </div>
        }
        confirmLabel="Allow and record"
        onConfirm={() => void grantAndRecord()}
        onClose={() => setAskConsent(false)}
      />
    </div>
  );
}
