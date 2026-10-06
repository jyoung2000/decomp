import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { ErrorCallout } from '../components/ErrorCallout';
import { PathField } from '../components/PathField';
import { useToast } from '../components/Toasts';
import { setSelectedCase } from '../lib/selection';
import { useApi, useResource } from '../lib/store';
import type { AiMode, NewCaseBody, OutputType, TargetLanguage } from '../lib/types';
import { comboState, emptyForm, splitArgs, validateNewProject, type FieldError, type NewProjectForm } from '../lib/validate';

const TARGETS: { id: TargetLanguage; label: string; sub: string }[] = [
  { id: 'rust', label: 'Rust', sub: 'Native code, CLI or desktop' },
  { id: 'rust_bevy', label: 'Rust + Bevy', sub: 'Games and real-time apps' },
  { id: 'web', label: 'HTML / CSS / JS', sub: 'Runs in a browser' },
  { id: 'auto', label: 'Auto', sub: 'Decide after discovery' },
];
const OUTPUTS: { id: OutputType; label: string; sub: string }[] = [
  { id: 'exe', label: 'Executable', sub: 'Single .exe' },
  { id: 'installer', label: 'Installer', sub: 'Windows setup (NSIS)' },
  { id: 'portable', label: 'Portable', sub: 'Folder, no install' },
  { id: 'web', label: 'Web', sub: 'Static site' },
  { id: 'pwa', label: 'PWA', sub: 'Installable web app' },
];
const AI: { id: AiMode; label: string; sub: string }[] = [
  { id: 'no_ai', label: 'No AI', sub: 'Deterministic tools only' },
  { id: 'assist_on_failure', label: 'Assist on failure', sub: 'Only when a step fails' },
  { id: 'assisted', label: 'AI-assisted', sub: 'Interpretation and repair' },
];

export function NewProjectView() {
  const api = useApi();
  const toast = useToast();
  const nav = useNavigate();
  const caps = useResource(() => api.capabilities(), [api]);
  const [f, setF] = useState<NewProjectForm>(emptyForm);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const summaryRef = useRef<HTMLDivElement>(null);

  const set = <K extends keyof NewProjectForm>(k: K, v: NewProjectForm[K]) => {
    const next = { ...f, [k]: v };
    setF(next);
    if (submitted) setErrors(validateNewProject(next, caps.data));
  };
  const err = (field: string) => errors.find((e) => e.field === field);
  const errText = (field: string) => {
    const e = err(field);
    return e ? `${e.what} ${e.next}` : null;
  };

  const capsNote = useMemo(() => {
    if (caps.error) return 'The controller did not report which target/output combinations are supported; it will validate the combination when you create the project.';
    return null;
  }, [caps.error]);

  const onSubmit = async (ev: FormEvent) => {
    ev.preventDefault();
    setSubmitted(true);
    setServerError(null);
    const errs = validateNewProject(f, caps.data);
    setErrors(errs);
    if (errs.length) {
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    const limits: Record<string, number> = {};
    if (f.max_workers) limits.max_workers = Number(f.max_workers);
    if (f.max_memory_mb) limits.max_memory_mb = Number(f.max_memory_mb);
    if (f.max_disk_gb) limits.max_disk_gb = Number(f.max_disk_gb);
    if (f.stage_timeout_minutes) limits.max_stage_seconds = Number(f.stage_timeout_minutes) * 60;
    const body: NewCaseBody = {
      name: f.name.trim(),
      source_root: f.source_root.trim(),
      output_root: f.output_root.trim(),
      target_language: f.target_language as TargetLanguage,
      output_type: f.output_type as OutputType,
      ai_policy: f.ai_mode === 'no_ai' ? { mode: 'no_ai' } : { mode: f.ai_mode, budget_usd: Number(f.budget_usd) },
      launch_profile: f.execute_original
        ? {
            execute_original: true,
            command: [f.program.trim(), ...splitArgs(f.args)],
            scenarios: f.scenarios.map((s) => ({ name: s.name.trim(), args: splitArgs(s.args), ...(s.stdin ? { stdin: s.stdin } : {}) })),
          }
        : { execute_original: false },
      ...(Object.keys(limits).length ? { settings: { limits } } : {}),
    };
    setBusy(true);
    try {
      const c = await api.createCase(body);
      setSelectedCase(c.case_id);
      toast.success('Project created', `${c.name} is ready. Press Start to begin discovery.`);
      nav(`/projects/${encodeURIComponent(c.case_id)}/overview`);
    } catch (e) {
      setServerError(e);
      requestAnimationFrame(() => summaryRef.current?.focus());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>New project</h1>
          <p className="lead">Point Rebuild Studio at an original program and choose what to rebuild it into. Originals are never modified; everything is written to the output folder.</p>
        </div>
      </div>
      <form className="form" onSubmit={onSubmit} noValidate aria-label="New project">
        <div ref={summaryRef} tabIndex={-1} aria-live="assertive">
          {serverError ? <ErrorCallout error={serverError} title="The project was not created" /> : null}
          {errors.length > 0 && (
            <div className="callout bad" role="alert" data-testid="validation-summary">
              <div className="ttl">
                {errors.length === 1 ? 'One thing needs attention' : `${errors.length} things need attention`} before the project can be created
              </div>
              <ul className="bullets">
                {errors.map((e) => (
                  <li key={e.field}>
                    <strong>{e.what}</strong> Affected: {e.affected} Next: {e.next}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <fieldset>
          <legend>Project</legend>
          <div className="stack-lg">
            <div className="field">
              <label htmlFor="np-name">Name *</label>
              <input id="np-name" type="text" value={f.name} onChange={(e) => set('name', e.target.value)} aria-invalid={!!err('name')} aria-describedby={err('name') ? 'np-name-err' : undefined} />
              {err('name') && <span className="field-error" id="np-name-err">{errText('name')}</span>}
            </div>
            <div className="field-row">
              <PathField name="source_root" label="Source folder (original program)" value={f.source_root} onChange={(v) => set('source_root', v)} error={errText('source_root')} required hint="Read-only: files here are never changed." />
              <PathField name="output_root" label="Output folder" value={f.output_root} onChange={(v) => set('output_root', v)} error={errText('output_root')} required hint="Receives source/, dist/, evidence/ and reports/." />
            </div>
          </div>
        </fieldset>

        <fieldset aria-describedby="np-target-hint">
          <legend>Target</legend>
          <div className="stack-lg">
            <div className="stack" role="radiogroup" aria-label="Target language">
              <span className="field-label">Target language</span>
              <div className="choice-grid">
                {TARGETS.map((t) => (
                  <label className="choice" key={t.id}>
                    <input type="radio" name="target_language" value={t.id} checked={f.target_language === t.id} onChange={() => set('target_language', t.id)} />
                    <span>
                      <span className="choice-title">{t.label}</span>
                      <span className="choice-sub">{t.sub}</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>
            <div className="stack" role="radiogroup" aria-label="Output type">
              <span className="field-label">Output type</span>
              <div className="choice-grid">
                {OUTPUTS.map((o) => {
                  const st = comboState(caps.data, f.target_language, o.id);
                  const disabled = st?.state === 'unsupported';
                  const note = st && st.state !== 'supported' ? `${st.state === 'handoff' ? 'Needs Windows handoff' : st.state === 'experimental' ? 'Experimental' : 'Not supported'}${st.reason ? `: ${st.reason}` : ''}` : null;
                  return (
                    <label className="choice" key={o.id} data-tooltip={note ?? undefined} data-testid={`output-${o.id}`}>
                      <input type="radio" name="output_type" value={o.id} checked={f.output_type === o.id} disabled={disabled} onChange={() => set('output_type', o.id)} aria-describedby={note ? `np-out-${o.id}` : undefined} />
                      <span>
                        <span className="choice-title">{o.label}</span>
                        <span className="choice-sub" id={`np-out-${o.id}`}>
                          {note ?? o.sub}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
              {err('output_type') && <span className="field-error">{errText('output_type')}</span>}
              <span className="hint muted small" id="np-target-hint">
                {capsNote ?? 'Combinations the controller marks unsupported are disabled; hover a choice to see why.'}
              </span>
            </div>
          </div>
        </fieldset>

        <fieldset>
          <legend>Original launch configuration</legend>
          <div className="stack-lg">
            <label className="check">
              <input type="checkbox" checked={f.execute_original} onChange={(e) => set('execute_original', e.target.checked)} />
              Execute the original program to capture its behaviour
            </label>
            <p className="small muted">When enabled, the original runs in a bounded sandbox for each scenario; its exit code, output, files and screens become the reference for comparisons. When disabled, only static analysis is used and behaviour comparisons are skipped.</p>
            {f.execute_original && (
              <>
                <div className="field-row">
                  <div className="field">
                    <label htmlFor="np-program">Program to run *</label>
                    <input id="np-program" type="text" value={f.program} onChange={(e) => set('program', e.target.value)} placeholder="bin\\app.exe" aria-invalid={!!err('program')} spellCheck={false} />
                    <span className="hint">Relative to the source folder.</span>
                    {err('program') && <span className="field-error">{errText('program')}</span>}
                  </div>
                  <div className="field">
                    <label htmlFor="np-args">Arguments</label>
                    <input id="np-args" type="text" value={f.args} onChange={(e) => set('args', e.target.value)} placeholder='--mode demo "file with spaces.txt"' spellCheck={false} />
                  </div>
                </div>
                <div className="stack">
                  <div className="row between">
                    <span className="field-label">Scenarios</span>
                    <button type="button" className="btn sm" onClick={() => set('scenarios', [...f.scenarios, { name: '', args: '', stdin: '' }])}>
                      Add scenario
                    </button>
                  </div>
                  {f.scenarios.length === 0 && <p className="small muted">No scenarios: the original is run once with the arguments above.</p>}
                  {f.scenarios.map((s, i) => (
                    <div className="field-row" key={i} style={{ alignItems: 'end' }}>
                      <div className="field">
                        <label htmlFor={`np-sc-name-${i}`}>Scenario {i + 1} name</label>
                        <input id={`np-sc-name-${i}`} type="text" value={s.name} aria-invalid={!!err(`scenario_${i}`)} onChange={(e) => set('scenarios', f.scenarios.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                      </div>
                      <div className="field">
                        <label htmlFor={`np-sc-args-${i}`}>Extra arguments</label>
                        <input id={`np-sc-args-${i}`} type="text" value={s.args} onChange={(e) => set('scenarios', f.scenarios.map((x, j) => (j === i ? { ...x, args: e.target.value } : x)))} />
                      </div>
                      <div className="field">
                        <label htmlFor={`np-sc-stdin-${i}`}>Standard input</label>
                        <input id={`np-sc-stdin-${i}`} type="text" value={s.stdin} onChange={(e) => set('scenarios', f.scenarios.map((x, j) => (j === i ? { ...x, stdin: e.target.value } : x)))} />
                      </div>
                      <div>
                        <button type="button" className="btn sm danger" aria-label={`Remove scenario ${i + 1}`} onClick={() => set('scenarios', f.scenarios.filter((_, j) => j !== i))}>
                          Remove
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        </fieldset>

        <fieldset>
          <legend>AI policy</legend>
          <div className="stack-lg">
            <div className="choice-grid" role="radiogroup" aria-label="AI policy">
              {AI.map((a) => (
                <label className="choice" key={a.id}>
                  <input type="radio" name="ai_mode" value={a.id} checked={f.ai_mode === a.id} onChange={() => set('ai_mode', a.id)} />
                  <span>
                    <span className="choice-title">{a.label}</span>
                    <span className="choice-sub">{a.sub}</span>
                  </span>
                </label>
              ))}
            </div>
            <div className="field" style={{ maxWidth: 260 }}>
              <label htmlFor="np-budget">Per-job budget (USD){f.ai_mode !== 'no_ai' && ' *'}</label>
              <input id="np-budget" type="number" min="0" step="0.01" inputMode="decimal" value={f.budget_usd} disabled={f.ai_mode === 'no_ai'} onChange={(e) => set('budget_usd', e.target.value)} aria-invalid={!!err('budget_usd')} />
              <span className="hint">AI never writes verification verdicts; it only proposes changes that the verifier checks.</span>
              {err('budget_usd') && <span className="field-error">{errText('budget_usd')}</span>}
            </div>
          </div>
        </fieldset>

        <fieldset>
          <legend>Resource limits</legend>
          <p className="small muted" style={{ marginBottom: 12 }}>Leave empty to use the controller defaults.</p>
          <div className="field-row">
            {(
              [
                ['max_workers', 'Parallel workers', '1–64'],
                ['max_memory_mb', 'Memory per worker (MB)', '≥ 256'],
                ['max_disk_gb', 'Disk budget (GB)', '≥ 1'],
                ['stage_timeout_minutes', 'Stage timeout (minutes)', '1–1440'],
              ] as const
            ).map(([k, label, hint]) => (
              <div className="field" key={k}>
                <label htmlFor={`np-${k}`}>{label}</label>
                <input id={`np-${k}`} type="number" inputMode="numeric" value={f[k]} onChange={(e) => set(k, e.target.value)} aria-invalid={!!err(k)} placeholder={hint} />
                {err(k) && <span className="field-error">{errText(k)}</span>}
              </div>
            ))}
          </div>
        </fieldset>

        <div className="row">
          <button type="submit" className="btn primary" disabled={busy} data-testid="create-project">
            {busy ? 'Creating…' : 'Create project'}
          </button>
          <button type="button" className="btn" onClick={() => nav(-1)}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}
