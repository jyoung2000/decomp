import { useEffect, useMemo, useState } from 'react';
import { Loading } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toasts';
import { bytes, humanize } from '../lib/format';
import { useApi, useResource } from '../lib/store';
import type { Availability, BackendEntry, ToolProbe } from '../lib/types';

const AVAIL: Availability[] = ['missing', 'detected', 'installed', 'usable', 'verified'];
const AVAIL_HELP: Record<Availability, string> = {
  missing: 'Not found on this machine',
  detected: 'Found, but not confirmed to run',
  installed: 'Installed at a known path',
  usable: 'Ran a smoke check successfully',
  verified: 'Passed a fixture regression on this host',
};

export function SettingsView() {
  return (
    <div className="page" data-testid="settings">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="lead">Tools the rebuild depends on, where data is stored, and how much the controller may run at once.</p>
        </div>
      </div>
      <Doctor />
      <SettingsEditor />
    </div>
  );
}

function Doctor() {
  const api = useApi();
  const [smoke, setSmoke] = useState(false);
  const res = useResource(() => api.doctor(smoke), [api, smoke]);
  const rows = useMemo(() => (res.data?.backends ?? []).flatMap((b) => (b.tools?.length ? b.tools.map((t) => ({ b, t })) : [{ b, t: null as ToolProbe | null }])), [res.data]);
  return (
    <section className="card" aria-labelledby="doctor-h">
      <div className="card-head">
        <h3 id="doctor-h">Dependencies</h3>
        <div className="btn-group">
          <button type="button" className="btn sm" onClick={() => (smoke ? res.reload() : setSmoke(true))} data-tooltip="Run each installed tool once to confirm it works" data-tooltip-pos="left">
            Run smoke checks
          </button>
          <button type="button" className="btn sm" onClick={res.reload}>
            Refresh
          </button>
        </div>
      </div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        States, weakest to strongest: {AVAIL.map((a) => `${humanize(a)} (${AVAIL_HELP[a].toLowerCase()})`).join(' → ')}.
      </p>
      {res.data?.summary && (
        <div className="row" style={{ marginBottom: 12 }} aria-label="Summary">
          {AVAIL.map((a) => (
            <StatusChip key={a} status={a} label={`${res.data!.summary[a] ?? 0} ${a}`} />
          ))}
        </div>
      )}
      {res.error ? (
        <ErrorCallout error={res.error} title="Dependency report unavailable" onRetry={res.reload} />
      ) : res.loading && !res.data ? (
        <Loading what="dependency report" />
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Dependency doctor">
            <thead>
              <tr>
                <th scope="col">Backend / tool</th>
                <th scope="col">State</th>
                <th scope="col">Version</th>
                <th scope="col">License</th>
                <th scope="col">Size</th>
                <th scope="col">Prerequisites</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ b, t }, i) => (
                <DoctorRow key={`${b.backend_id}-${t?.name ?? i}`} b={b} t={t} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DoctorRow({ b, t }: { b: BackendEntry; t: ToolProbe | null }) {
  const state = (t?.availability ?? b.availability) as Availability;
  const size = t?.size_bytes ?? (typeof b.resources?.disk_bytes === 'number' ? (b.resources.disk_bytes as number) : null);
  return (
    <tr>
      <td>
        <div>
          {b.title}
          {b.experimental && <span className="chip warn" style={{ marginLeft: 6 }}>experimental</span>}
        </div>
        <div className="xs muted mono">{t?.name ?? b.backend_id}</div>
      </td>
      <td>
        <StatusChip status={state} title={AVAIL_HELP[state]} />
      </td>
      <td className="mono small">
        {t?.version ?? '—'}
        {t?.pinned && <div className="xs muted">pinned {t.pinned}</div>}
      </td>
      <td className="small">{t?.license || '—'}</td>
      <td className="small">{size != null ? bytes(size) : '—'}</td>
      <td className="small">{t?.prerequisites?.length ? t.prerequisites.join(', ') : '—'}</td>
      <td className="small wrap-any">
        {t?.detail || ''}
        {t?.path && <div className="xs muted mono">{t.path}</div>}
        {b.smoke && <div className="xs muted">smoke: {JSON.stringify(b.smoke).slice(0, 160)}</div>}
      </td>
    </tr>
  );
}

type Settings = Record<string, unknown>;
const SECTION_ORDER = ['storage', 'concurrency', 'limits', 'capture', 'network', 'output'];
/** Reported by the controller at runtime; PUT /settings does not change them. */
const READ_ONLY = new Set(['data_dir', 'tools_dir', 'previews_running', 'version', 'pid']);
const SECTION_HELP: Record<string, string> = {
  data_dir: 'Where this controller keeps projects, evidence and its database. Set when the controller starts.',
  tools_dir: 'Where pinned rebuild tools are installed.',
  previews_running: 'Preview instances currently running.',
  storage: 'Where projects, evidence blobs and caches are kept.',
  concurrency: 'How many jobs and workers may run at once.',
  limits: 'Hard bounds for each stage and subprocess.',
  capture: 'What may be captured while running originals and candidates.',
  network: 'Which network access rebuild jobs may use.',
  output: 'How outputs are published.',
};

function SettingsEditor() {
  const api = useApi();
  const toast = useToast();
  const res = useResource(() => api.settings(), [api]);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (res.data) setDraft(structuredClone(res.data));
  }, [res.data]);
  if (res.error) return <ErrorCallout error={res.error} title="Settings could not be loaded" onRetry={res.reload} />;
  if (!draft) return <Loading what="settings" />;
  const sections = Object.keys(draft).sort((a, b) => (SECTION_ORDER.indexOf(a) + 99) % 99 - ((SECTION_ORDER.indexOf(b) + 99) % 99) || a.localeCompare(b));
  const dirty = JSON.stringify(draft) !== JSON.stringify(res.data);
  const save = async () => {
    setError(null);
    try {
      const saved = await api.putSettings(draft);
      toast.success('Settings saved');
      setDraft(structuredClone(saved ?? draft));
      res.reload();
    } catch (e) {
      setError(e);
    }
  };
  const update = (path: string[], value: unknown) => {
    const next = structuredClone(draft);
    let cur = next as Record<string, unknown>;
    for (const p of path.slice(0, -1)) cur = cur[p] as Record<string, unknown>;
    cur[path[path.length - 1]] = value;
    setDraft(next);
  };
  return (
    <section className="stack-lg" aria-label="Settings">
      {error ? <ErrorCallout error={error} title="Settings were not saved" /> : null}
      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        {sections.map((s) => {
          const val = draft[s];
          return (
            <fieldset key={s} data-testid={`settings-${s}`}>
              <legend>{humanize(s)}</legend>
              {SECTION_HELP[s] && <p className="small muted" style={{ marginBottom: 8 }}>{SECTION_HELP[s]}</p>}
              {READ_ONLY.has(s) ? (
                <ReadOnlyValue id={`set-${s}`} value={val} />
              ) : val && typeof val === 'object' && !Array.isArray(val) ? (
                <div className="stack">
                  {Object.entries(val as Settings).map(([k, v]) => (
                    <SettingField key={k} id={`set-${s}-${k}`} label={humanize(k)} value={v} onChange={(nv) => update([s, k], nv)} />
                  ))}
                </div>
              ) : (
                <SettingField id={`set-${s}`} label={humanize(s)} value={val} onChange={(nv) => update([s], nv)} />
              )}
            </fieldset>
          );
        })}
      </div>
      <div className="row">
        <button type="button" className="btn primary" disabled={!dirty} onClick={save}>
          Save settings
        </button>
        <button type="button" className="btn" disabled={!dirty} onClick={() => setDraft(structuredClone(res.data!))}>
          Discard changes
        </button>
        {dirty && <span className="small muted">Unsaved changes</span>}
      </div>
    </section>
  );
}

function SettingField({ id, label, value, onChange }: { id: string; label: string; value: unknown; onChange: (v: unknown) => void }) {
  if (typeof value === 'boolean') {
    return (
      <label className="check" htmlFor={id}>
        <input id={id} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} /> {label}
      </label>
    );
  }
  if (typeof value === 'number') {
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input id={id} type="number" value={Number.isFinite(value) ? value : ''} onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))} />
      </div>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input id={id} type="text" value={value.join(', ')} onChange={(e) => onChange(e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} />
        <span className="hint">Comma separated</span>
      </div>
    );
  }
  if (value && typeof value === 'object') {
    return (
      <div className="field">
        <span className="field-label">{label}</span>
        <pre className="pre">{JSON.stringify(value, null, 2)}</pre>
      </div>
    );
  }
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input id={id} type="text" value={value == null ? '' : String(value)} onChange={(e) => onChange(e.target.value)} spellCheck={false} />
    </div>
  );
}

function ReadOnlyValue({ id, value }: { id: string; value: unknown }) {
  const text = Array.isArray(value) ? (value.length ? value.map((v) => (typeof v === 'object' ? JSON.stringify(v) : String(v))).join(', ') : 'None') : value == null || value === '' ? 'Not reported' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  return (
    <p className="mono small wrap-any" id={id} data-readonly="true" title="Reported by the controller; not editable here">
      {text}
    </p>
  );
}
