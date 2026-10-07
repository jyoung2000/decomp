import { useEffect, useMemo, useRef, useState } from 'react';
import { AI_TASKS, PRESETS, presetLadders, rungKey, taskLabel } from '../../lib/ai';
import { useApi, useResource } from '../../lib/store';
import type { Connection, LadderEntry, LadderPresetId, PresetResult } from '../../lib/types';
import { Empty, Loading } from '../Empty';
import { ErrorCallout } from '../ErrorCallout';
import { useToast } from '../Toasts';
import { LocalityBadge } from './Badges';
import { LadderList } from './LadderList';
import { ModelPicker } from './ModelPicker';

/** The app-wide model ladder: "Models per task" with presets, picker, reorder and a version number. */
export function LadderSection({ connections, version = 0 }: { connections: Connection[]; version?: number }) {
  const api = useApi();
  const ladder = useResource(() => api.ladder(), [api], version);
  const rev = ladder.data?.config_revision;
  // a probe changes each rung's availability / capabilities: refetch the ladder whenever a connection's probe stamp changes
  const probeSig = connections.map((c) => `${c.connection_id}:${c.state}:${c.last_probe ?? ''}`).join('|');
  const { reload } = ladder;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reload();
  }, [probeSig]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <section className="card" aria-labelledby="routes-h" data-testid="ladder-section">
      <div className="card-head">
        <h3 id="routes-h">Models per task</h3>
        {rev != null && (
          <span className="chip outline" data-testid="ladder-version" title="Increases every time any ladder changes. Each AI attempt records the version it used.">
            Ladder version {rev}
          </span>
        )}
      </div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        For each task, position 1 is tried first. If it is unavailable, over its limit, cannot take the input, or would break your budget, the next position is tried. “No AI” on a project skips every ladder.
      </p>
      {ladder.error ? (
        <ErrorCallout error={ladder.error} title="The model ladder could not be loaded" onRetry={ladder.reload} />
      ) : !ladder.data ? (
        <Loading what="the model ladder" />
      ) : (
        <div className="stack-lg">
          <PresetBar onApplied={ladder.reload} />
          {connections.length === 0 && <Empty title="No connections yet">Add a provider above (a local model server or a cloud key) before building a ladder.</Empty>}
          {AI_TASKS.map((t) => (
            <TaskLadder key={t.id} task={t} entries={ladder.data!.tasks?.[t.id]?.entries ?? []} rationale={ladder.data!.tasks?.[t.id]?.rationale} revision={ladder.data!.config_revision} connections={connections} onSaved={ladder.reload} />
          ))}
        </div>
      )}
    </section>
  );
}

function rationaleText(r: string | undefined): string {
  if (!r) return '';
  if (r === 'user') return 'set by you';
  if (r === 'auto') return 'chosen automatically';
  if (r.startsWith('preset:')) return `from the “${PRESETS.find((p) => p.id === r.slice(7))?.label ?? r.slice(7)}” preset`;
  return r;
}

function PresetBar({ onApplied }: { onApplied: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [pending, setPending] = useState<{ id: LadderPresetId; result: PresetResult } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const preview = async (id: LadderPresetId) => {
    setBusy(id);
    setError(null);
    try {
      setPending({ id, result: await api.ladderPreset(id, false) });
    } catch (e) {
      setPending(null);
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const apply = async () => {
    if (!pending) return;
    setBusy('apply');
    try {
      await api.ladderPreset(pending.id, true);
      toast.success(`Preset applied: ${PRESETS.find((p) => p.id === pending.id)?.label}`);
      setPending(null);
      onApplied();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const ladders = pending ? presetLadders(pending.result) : {};
  const label = pending ? PRESETS.find((p) => p.id === pending.id)?.label : '';
  return (
    <div className="stack" data-testid="preset-bar">
      <div role="group" aria-label="Ladder presets" className="row">
        <span className="small" style={{ fontWeight: 600 }}>
          Presets
        </span>
        {PRESETS.map((p) => (
          <button key={p.id} type="button" className="btn sm" title={p.sub} aria-pressed={pending?.id === p.id} disabled={busy != null} aria-describedby={busy != null ? 'preset-busy' : undefined} onClick={() => preview(p.id)}>
            {busy === p.id ? 'Previewing…' : p.label}
          </button>
        ))}
        {busy != null && (
          <span className="sr-only" id="preset-busy">
            Wait for the current request to finish.
          </span>
        )}
      </div>
      <p className="small muted">A preset replaces every task’s ladder. You see exactly what would change first; nothing is saved until you apply it.</p>
      {error ? <ErrorCallout error={error} title="The preset could not be previewed or applied" /> : null}
      {pending && (
        <div className="callout info" role="region" aria-label={`Preview of the ${label} preset`} aria-live="polite" data-testid="preset-preview">
          <div className="ttl">Preview: {label} (not applied yet)</div>
          {pending.id === 'no_ai' && <div>Every ladder would be cleared. Projects that use the app ladder would not contact any AI service; detection, recovery, evidence and deterministic ports still run.</div>}
          <ul className="list">
            {AI_TASKS.map((t) => {
              const es = ladders[t.id] ?? [];
              return (
                <li key={t.id}>
                  <strong>{t.label}: </strong>
                  {es.length ? (
                    <span className="row" style={{ display: 'inline-flex' }}>
                      {es.map((e, i) => (
                        <span key={rungKey(e)} className="row" style={{ display: 'inline-flex', gap: 4 }}>
                          {i + 1}. <span className="mono">{e.model}</span> <LocalityBadge locality={e.locality} />
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="muted">no model; work that needs this task is skipped</span>
                  )}
                </li>
              );
            })}
          </ul>
          {(pending.result.warnings ?? []).length > 0 && (
            <div className="callout warn" role="alert">
              <div className="ttl">Warnings</div>
              <ul className="bullets">
                {pending.result.warnings!.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="row">
            <button type="button" className="btn primary" onClick={apply} disabled={busy != null} aria-describedby={busy != null ? 'preset-busy' : undefined}>
              {busy === 'apply' ? 'Applying…' : `Apply “${label}”`}
            </button>
            <button type="button" className="btn" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function TaskLadder({ task, entries, rationale, revision, connections, onSaved }: { task: { id: string; label: string; sub: string }; entries: LadderEntry[]; rationale?: string; revision: number; connections: Connection[]; onSaved: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [draft, setDraft] = useState<LadderEntry[]>(entries);
  const [picking, setPicking] = useState(false);
  const [saving, setSaving] = useState(false);
  const sig = (es: LadderEntry[]) => es.map(rungKey).join('|');
  const serverSig = sig(entries);
  useEffect(() => setDraft(entries), [serverSig, revision]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = sig(draft) !== serverSig;
  const existing = useMemo(() => new Set(draft.map(rungKey)), [draft]);
  const id = `lad-${task.id}`;
  const save = async () => {
    setSaving(true);
    try {
      await api.putLadder(task.id, draft.map((e) => ({ connection_id: e.connection_id, model: e.model })));
      toast.success(`${task.label} ladder saved`, 'The ladder version increased.');
      onSaved();
    } catch (e) {
      toast.error('The ladder was not saved', e);
    } finally {
      setSaving(false);
    }
  };
  return (
    <fieldset data-testid={`route-${task.id}`}>
      <legend>
        {task.label} <span className="small muted">— {task.sub}</span>
      </legend>
      <div className="stack">
        {rationale && <p className="small muted">Currently {rationaleText(rationale)}.</p>}
        <LadderList id={id} label={`${task.label} model ladder`} entries={draft} onChange={setDraft} />
        <div className="row">
          <button type="button" className="btn sm" onClick={() => setPicking(true)}>
            Add model<span className="sr-only"> to {task.label}</span>
          </button>
          <button type="button" className="btn sm primary" onClick={save} disabled={!dirty || saving} aria-describedby={!dirty ? `${id}-nosave` : undefined}>
            {saving ? 'Saving…' : 'Save ladder'}
            <span className="sr-only"> for {task.label}</span>
          </button>
          <button type="button" className="btn sm" onClick={() => setDraft(entries)} disabled={!dirty} aria-describedby={!dirty ? `${id}-nosave` : undefined}>
            Revert
          </button>
          {dirty ? <span className="small" role="status">Unsaved changes</span> : <span className="small muted" id={`${id}-nosave`}>No changes to save.</span>}
        </div>
      </div>
      <ModelPicker open={picking} task={task.id} taskLabel={taskLabel(task.id)} connections={connections} existing={existing} onPick={(e) => setDraft((d) => [...d, e])} onClose={() => setPicking(false)} />
    </fieldset>
  );
}
