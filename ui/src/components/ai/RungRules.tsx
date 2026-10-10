import { useId, useState } from 'react';
import { ACTION_LABEL, cleanRules, DEFAULT_FAILURE_KINDS, ruleSummary, type FailureKind, type RuleAction, type RungRules } from '../../lib/aiControl';

/**
 * What happens when ONE rung fails, per failure kind: use the next rung (default), wait and retry, or stop and ask me.
 * Collapsed it shows a one-line summary; the editor is a plain form (select + numbers) that works with the keyboard.
 */
export function RungRulesEditor({ model, rules, kinds = DEFAULT_FAILURE_KINDS, onChange, testId }: { model: string; rules: RungRules | undefined; kinds?: FailureKind[]; onChange: (r: RungRules) => void; testId: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const cur = cleanRules(rules);
  const set = (kind: string, patch: Partial<{ action: RuleAction; wait_minutes: number; max_tries: number }>) => {
    const prev = cur[kind] ?? { action: 'next' as RuleAction };
    const next = { ...prev, ...patch };
    if (next.action === 'wait') {
      next.wait_minutes = next.wait_minutes ?? 5;
      next.max_tries = next.max_tries ?? 3;
    }
    onChange(cleanRules({ ...cur, [kind]: next }));
  };
  return (
    <div className="stack" data-testid={testId}>
      <div className="row small">
        <span className="muted" data-testid={`${testId}-summary`}>
          {ruleSummary(cur, kinds)}
        </span>
        <button type="button" className="btn sm" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)}>
          {open ? 'Close rules' : 'Failure rules'}
          <span className="sr-only"> for {model}</span>
        </button>
      </div>
      {open && (
        <fieldset id={id} className="stack" aria-label={`What to do when ${model} fails`}>
          <legend className="small">When {model}…</legend>
          {kinds.map((k) => {
            const r = cur[k.kind] ?? { action: 'next' as RuleAction };
            const sel = `${id}-${k.kind}`;
            return (
              <div key={k.kind} className="row small" data-testid={`${testId}-${k.kind}`}>
                <label htmlFor={sel} style={{ minWidth: 220 }}>
                  {k.label}
                </label>
                <select id={sel} value={r.action} onChange={(e) => set(k.kind, { action: e.target.value as RuleAction })}>
                  <option value="next">{ACTION_LABEL.next}</option>
                  {k.wait_allowed && <option value="wait">{ACTION_LABEL.wait}</option>}
                  <option value="stop">{ACTION_LABEL.stop}</option>
                </select>
                {r.action === 'wait' && (
                  <>
                    <label htmlFor={`${sel}-min`}>minutes</label>
                    <input id={`${sel}-min`} type="number" min={0.5} max={240} step={0.5} style={{ width: 72 }} value={r.wait_minutes ?? 5} onChange={(e) => set(k.kind, { wait_minutes: Number(e.target.value) })} />
                    <label htmlFor={`${sel}-tries`}>tries</label>
                    <input id={`${sel}-tries`} type="number" min={1} max={20} step={1} style={{ width: 60 }} value={r.max_tries ?? 3} onChange={(e) => set(k.kind, { max_tries: Number(e.target.value) })} />
                  </>
                )}
              </div>
            );
          })}
          <p className="xs muted">Waiting never re-sends a request that may already have been billed. “Stop and ask me” blocks the project with a plain message until you act.</p>
        </fieldset>
      )}
    </div>
  );
}
