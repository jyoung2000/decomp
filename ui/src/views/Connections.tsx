import { useEffect, useState, type FormEvent } from 'react';
import { ConfirmDialog } from '../components/Dialog';
import { Empty, Loading } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toasts';
import { dateTime, humanize, usd } from '../lib/format';
import { useApi, useResource, useStoreSelector } from '../lib/store';
import type { Connection, HermesStatus, TaskRoute } from '../lib/types';

export const PROVIDERS: { id: string; label: string; endpoint?: string; needsEndpoint?: boolean }[] = [
  { id: 'openai', label: 'OpenAI', endpoint: 'https://api.openai.com/v1' },
  { id: 'anthropic', label: 'Anthropic', endpoint: 'https://api.anthropic.com' },
  { id: 'gemini', label: 'Google Gemini', endpoint: 'https://generativelanguage.googleapis.com' },
  { id: 'openrouter', label: 'OpenRouter', endpoint: 'https://openrouter.ai/api/v1' },
  { id: 'local_openai', label: 'Local (OpenAI-compatible)', endpoint: 'http://127.0.0.1:11434/v1', needsEndpoint: true },
  { id: 'custom', label: 'Custom', needsEndpoint: true },
];
export const TASKS: { id: string; label: string; sub: string }[] = [
  { id: 'interpretation', label: 'Interpretation', sub: 'Explain decompiled code and data' },
  { id: 'repair', label: 'Repair', sub: 'Propose fixes for failing builds/tests' },
  { id: 'visual_review', label: 'Visual review', sub: 'Describe screenshot differences' },
  { id: 'verification_assist', label: 'Verification assist', sub: 'Suggest scenarios (never verdicts)' },
  { id: 'knowledge', label: 'Knowledge', sub: 'Propose reusable adapters/rules' },
];

const providerLabel = (id: string) => PROVIDERS.find((p) => p.id === id)?.label ?? id;
const authLabel = (m: string) => (m === 'subscription_handoff' ? 'External client handoff' : m === 'api_key' ? 'API key' : m === 'local' ? 'Local, no key' : humanize(m));

export function ConnectionsView() {
  const api = useApi();
  const toast = useToast();
  const version = useStoreSelector((s) => s.state.versions.connections ?? 0);
  const conns = useResource(() => api.connections(), [api], version);
  const [del, setDel] = useState<Connection | null>(null);
  const [probing, setProbing] = useState<string | null>(null);

  const probe = async (c: Connection) => {
    setProbing(c.connection_id);
    try {
      const r = await api.probeConnection(c.connection_id);
      toast.push({ kind: r.state === 'ok' || r.state === 'usable' ? 'success' : 'info', title: `Probe: ${humanize(r.state ?? 'done')}`, message: c.label });
      conns.reload();
    } catch (e) {
      toast.error(`Probe failed for ${c.label}`, e);
    } finally {
      setProbing(null);
    }
  };

  return (
    <div className="page" data-testid="connections">
      <div className="page-head">
        <div>
          <h1>Connections</h1>
          <p className="lead">AI providers are optional. They only propose changes; the verifier decides what passes. Keys are stored by the desktop app’s credential store, never shown again.</p>
        </div>
      </div>

      <section className="card" aria-labelledby="conn-list-h">
        <h3 id="conn-list-h">Providers</h3>
        {conns.error ? (
          <ErrorCallout error={conns.error} title="Connections could not be loaded" onRetry={conns.reload} />
        ) : conns.loading && !conns.data ? (
          <Loading what="connections" />
        ) : !conns.data?.length ? (
          <Empty title="No AI connections">Projects using “No AI” work without any connection. Add one below to enable AI assistance.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table" aria-label="AI connections">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Provider</th>
                  <th scope="col">Access</th>
                  <th scope="col">Models</th>
                  <th scope="col">State</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {conns.data.map((c) => (
                  <tr key={c.connection_id} data-testid={`conn-${c.connection_id}`}>
                    <td>
                      <div>{c.label}</div>
                      <div className="xs muted mono wrap-any">{c.endpoint}</div>
                    </td>
                    <td>{providerLabel(c.provider)}</td>
                    <td>
                      {c.auth_mode === 'subscription_handoff' ? (
                        <div className="stack" style={{ gap: 2 }}>
                          <span className="chip info">External client handoff</span>
                          <span className="xs muted">{c.limits?.text ?? 'Limits are set by the external client’s subscription.'}</span>
                        </div>
                      ) : (
                        authLabel(c.auth_mode)
                      )}
                    </td>
                    <td className="small wrap-any">{c.models?.join(', ') || '—'}</td>
                    <td>
                      <StatusChip status={c.state} />
                      <div className="xs muted">{c.last_probe ? `probed ${dateTime(c.last_probe)}` : 'never probed'}</div>
                    </td>
                    <td>
                      <div className="btn-group">
                        <button type="button" className="btn sm" disabled={probing === c.connection_id} onClick={() => probe(c)} aria-label={`Probe ${c.label}`}>
                          {probing === c.connection_id ? 'Probing…' : 'Probe'}
                        </button>
                        <button type="button" className="btn sm danger" onClick={() => setDel(c)} aria-label={`Delete ${c.label}`}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <AddConnection onAdded={conns.reload} />
      <Routes connections={conns.data ?? []} />
      <Costs />
      <Hermes />

      <ConfirmDialog
        open={!!del}
        title={`Delete “${del?.label ?? ''}”?`}
        body={<p>Task routes that use this connection fall back to their next option. The stored key is removed.</p>}
        confirmLabel="Delete connection"
        danger
        onClose={() => setDel(null)}
        onConfirm={async () => {
          if (!del) return;
          try {
            await api.deleteConnection(del.connection_id);
            toast.success('Connection deleted');
            conns.reload();
          } catch (e) {
            toast.error('Delete failed', e);
          }
        }}
      />
    </div>
  );
}

function AddConnection({ onAdded }: { onAdded: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [provider, setProvider] = useState('openai');
  const [label, setLabel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [auth, setAuth] = useState('api_key');
  const [key, setKey] = useState('');
  const [models, setModels] = useState('');
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);
  const p = PROVIDERS.find((x) => x.id === provider)!;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const er: Record<string, string> = {};
    if (!label.trim()) er.label = 'Give the connection a name so routes can refer to it.';
    if (p.needsEndpoint && !endpoint.trim()) er.endpoint = 'This provider needs an endpoint URL (for example http://127.0.0.1:11434/v1).';
    if (auth === 'api_key' && !key.trim()) er.key = 'Enter the API key, or pick another access mode.';
    setErrs(er);
    setError(null);
    if (Object.keys(er).length) return;
    try {
      await api.addConnection({
        provider,
        label: label.trim(),
        endpoint: endpoint.trim() || p.endpoint || '',
        auth_mode: auth,
        ...(auth === 'api_key' ? { api_key: key } : {}),
        models: models.split(',').map((m) => m.trim()).filter(Boolean),
      });
      toast.success('Connection added', 'Probe it to check access and available models.');
      setLabel('');
      setKey('');
      setModels('');
      setEndpoint('');
      onAdded();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section className="card" aria-labelledby="add-conn-h">
      <h3 id="add-conn-h">Add connection</h3>
      <form className="form" onSubmit={submit} noValidate aria-label="Add connection">
        {error ? <ErrorCallout error={error} title="Connection was not added" /> : null}
        <div className="field-row">
          <div className="field">
            <label htmlFor="ac-provider">Provider</label>
            <select id="ac-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
              {PROVIDERS.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="ac-label">Name</label>
            <input id="ac-label" type="text" value={label} onChange={(e) => setLabel(e.target.value)} aria-invalid={!!errs.label} />
            {errs.label && <span className="field-error">{errs.label}</span>}
          </div>
          <div className="field">
            <label htmlFor="ac-endpoint">Endpoint{p.needsEndpoint ? ' *' : ''}</label>
            <input id="ac-endpoint" type="url" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder={p.endpoint ?? 'https://…'} aria-invalid={!!errs.endpoint} />
            {errs.endpoint && <span className="field-error">{errs.endpoint}</span>}
          </div>
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="ac-auth">Access</label>
            <select id="ac-auth" value={auth} onChange={(e) => setAuth(e.target.value)}>
              <option value="api_key">API key</option>
              <option value="subscription_handoff">External client handoff (subscription)</option>
              <option value="local">Local, no key</option>
              <option value="none">None</option>
            </select>
            {auth === 'subscription_handoff' && <span className="hint">Work is handed to an external client (e.g. a desktop assistant) under its own subscription limits; Rebuild Studio does not hold the credentials.</span>}
          </div>
          {auth === 'api_key' && (
            <div className="field">
              <label htmlFor="ac-key">API key</label>
              <input id="ac-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} aria-invalid={!!errs.key} />
              {errs.key && <span className="field-error">{errs.key}</span>}
            </div>
          )}
          <div className="field">
            <label htmlFor="ac-models">Models (comma separated)</label>
            <input id="ac-models" type="text" value={models} onChange={(e) => setModels(e.target.value)} placeholder="leave empty to discover on probe" />
          </div>
        </div>
        <div>
          <button type="submit" className="btn primary">
            Add connection
          </button>
        </div>
      </form>
    </section>
  );
}

function Routes({ connections }: { connections: Connection[] }) {
  const api = useApi();
  const routes = useResource(() => api.routes(), [api]);
  return (
    <section className="card" aria-labelledby="routes-h">
      <h3 id="routes-h">Models per task</h3>
      <p className="small muted" style={{ marginBottom: 12 }}>
        The primary is tried first; fallbacks are used in order when it is unavailable or over its limit.
      </p>
      {routes.error ? (
        <ErrorCallout error={routes.error} onRetry={routes.reload} />
      ) : (
        <div className="stack-lg">
          {TASKS.map((t) => (
            <RouteEditor key={t.id} task={t} route={routes.data?.find((r) => r.task === t.id)} connections={connections} onSaved={routes.reload} />
          ))}
        </div>
      )}
    </section>
  );
}

function RouteEditor({ task, route, connections, onSaved }: { task: { id: string; label: string; sub: string }; route?: TaskRoute; connections: Connection[]; onSaved: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [primary, setPrimary] = useState({ connection: route?.primary_connection ?? '', model: route?.primary_model ?? '' });
  const [fallbacks, setFallbacks] = useState(route?.fallbacks ?? []);
  useEffect(() => {
    setPrimary({ connection: route?.primary_connection ?? '', model: route?.primary_model ?? '' });
    setFallbacks(route?.fallbacks ?? []);
  }, [route]);
  const modelsOf = (cid: string) => connections.find((c) => c.connection_id === cid)?.models ?? [];
  const save = async () => {
    try {
      await api.putRoute(task.id, { primary_connection: primary.connection || null, primary_model: primary.model || null, fallbacks: fallbacks.filter((f) => f.connection) });
      toast.success(`${task.label} route saved`);
      onSaved();
    } catch (e) {
      toast.error('Route was not saved', e);
    }
  };
  const move = (i: number, d: number) => {
    const n = [...fallbacks];
    const j = i + d;
    if (j < 0 || j >= n.length) return;
    [n[i], n[j]] = [n[j], n[i]];
    setFallbacks(n);
  };
  const Picker = ({ value, onChange, idp }: { value: { connection: string; model: string }; onChange: (v: { connection: string; model: string }) => void; idp: string }) => (
    <div className="field-row" style={{ flex: 1 }}>
      <div className="field">
        <label htmlFor={`${idp}-c`} className="small">
          Connection
        </label>
        <select id={`${idp}-c`} value={value.connection} onChange={(e) => onChange({ connection: e.target.value, model: '' })}>
          <option value="">None</option>
          {connections.map((c) => (
            <option key={c.connection_id} value={c.connection_id}>
              {c.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={`${idp}-m`} className="small">
          Model
        </label>
        <input id={`${idp}-m`} type="text" list={`${idp}-ml`} value={value.model} onChange={(e) => onChange({ ...value, model: e.target.value })} />
        <datalist id={`${idp}-ml`}>
          {modelsOf(value.connection).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </div>
    </div>
  );
  return (
    <fieldset data-testid={`route-${task.id}`}>
      <legend>
        {task.label} <span className="small muted">— {task.sub}</span>
      </legend>
      <div className="stack">
        <span className="small" style={{ fontWeight: 600 }}>
          Primary
        </span>
        {Picker({ value: primary, onChange: setPrimary, idp: `rt-${task.id}-p` })}
        {fallbacks.map((f, i) => (
          <div key={i} className="row" style={{ alignItems: 'flex-end' }}>
            <span className="small" style={{ fontWeight: 600, width: 84 }}>
              Fallback {i + 1}
            </span>
            {Picker({ value: f, onChange: (v) => setFallbacks(fallbacks.map((x, j) => (j === i ? v : x))), idp: `rt-${task.id}-f${i}` })}
            <div className="btn-group">
              <button type="button" className="btn sm icon" aria-label={`Move fallback ${i + 1} up`} disabled={i === 0} onClick={() => move(i, -1)}>
                ↑
              </button>
              <button type="button" className="btn sm icon" aria-label={`Move fallback ${i + 1} down`} disabled={i === fallbacks.length - 1} onClick={() => move(i, 1)}>
                ↓
              </button>
              <button type="button" className="btn sm" aria-label={`Remove fallback ${i + 1}`} onClick={() => setFallbacks(fallbacks.filter((_, j) => j !== i))}>
                Remove
              </button>
            </div>
          </div>
        ))}
        <div className="row">
          <button type="button" className="btn sm" onClick={() => setFallbacks([...fallbacks, { connection: '', model: '' }])}>
            Add fallback
          </button>
          <button type="button" className="btn sm primary" onClick={save}>
            Save route
          </button>
        </div>
      </div>
    </fieldset>
  );
}

function Costs() {
  const api = useApi();
  const v = useStoreSelector((s) => s.state.versions.budgets ?? 0);
  const budgets = useResource(() => api.budgets(), [api], v);
  const calls = useResource(() => api.aiCalls(), [api], v);
  const rows = calls.data ?? [];
  const known = rows.filter((r) => r.cost_known && r.cost_usd != null).reduce((a, r) => a + (r.cost_usd ?? 0), 0);
  return (
    <section className="card" aria-labelledby="cost-h">
      <h3 id="cost-h">Limits &amp; cost</h3>
      {budgets.error ? (
        <ErrorCallout error={budgets.error} onRetry={budgets.reload} />
      ) : !budgets.data?.length ? (
        <p className="small muted">No budgets defined yet. Per-job budgets are created from each project’s AI policy.</p>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Budgets">
            <thead>
              <tr>
                <th scope="col">Scope</th>
                <th scope="col">Limit</th>
                <th scope="col">Estimated (reserved)</th>
                <th scope="col">Actual (spent)</th>
              </tr>
            </thead>
            <tbody>
              {budgets.data.map((b) => (
                <tr key={b.budget_id}>
                  <td className="mono small">{b.scope}</td>
                  <td className="num">{usd(b.limit_usd)}</td>
                  <td className="num">{usd(b.reserved_usd)}</td>
                  <td className="num">{usd(b.spent_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small" style={{ marginTop: 8 }}>
        {rows.length} AI call{rows.length === 1 ? '' : 's'} recorded · actual cost {usd(known)}
        {rows.some((r) => !r.cost_known) && ` · ${rows.filter((r) => !r.cost_known).length} with unknown cost (provider did not report usage)`}
      </p>
    </section>
  );
}

function Hermes() {
  const api = useApi();
  const toast = useToast();
  const st = useResource<HermesStatus>(() => api.hermesStatus(), [api]);
  const [profile, setProfile] = useState('');
  const [dry, setDry] = useState<Record<string, unknown> | null>(null);
  const diags = Array.isArray(st.data?.diagnostics) ? st.data!.diagnostics! : [];
  const scalar = Object.entries(st.data ?? {}).filter(([k, v]) => k !== 'diagnostics' && (typeof v !== 'object' || v === null));
  return (
    <section className="card" aria-labelledby="hermes-h" data-testid="hermes">
      <div className="card-head">
        <h3 id="hermes-h">Hermes pairing</h3>
        {st.data && <StatusChip status={st.data.paired ? 'ok' : 'unprobed'} label={st.data.paired ? 'Paired' : 'Not paired'} />}
      </div>
      {st.error ? (
        <ErrorCallout error={st.error} title="Hermes status unavailable" onRetry={st.reload} tone="warn" />
      ) : !st.data ? (
        <Loading what="Hermes status" />
      ) : (
        <div className="stack-lg">
          <dl className="kv small">
            {scalar.map(([k, v]) => (
              <FragmentKV key={k} k={humanize(k)} v={String(v)} />
            ))}
          </dl>
          <div>
            <h4>Diagnostics</h4>
            {diags.length === 0 ? (
              <p className="small muted">No diagnostics reported.</p>
            ) : (
              <ul className="list">
                {diags.map((d, i) =>
                  typeof d === 'string' ? (
                    <li key={i} className="small">
                      {d}
                    </li>
                  ) : (
                    <li key={i} className="row small">
                      <StatusChip status={d.ok ? 'pass' : 'fail'} label={d.ok ? 'OK' : 'Problem'} /> <strong>{d.check}</strong> <span className="muted">{d.detail}</span>
                    </li>
                  ),
                )}
              </ul>
            )}
          </div>
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <div className="field" style={{ flex: '1 1 260px' }}>
              <label htmlFor="hermes-profile">Hermes profile path (optional)</label>
              <input id="hermes-profile" type="text" value={profile} onChange={(e) => setProfile(e.target.value)} placeholder="auto-detect" />
            </div>
            <button
              type="button"
              className="btn"
              onClick={async () => {
                try {
                  await api.hermesPair(profile.trim() || undefined);
                  toast.success('Pairing requested');
                  st.reload();
                } catch (e) {
                  toast.error('Pairing failed', e);
                }
              }}
            >
              Pair
            </button>
            <button
              type="button"
              className="btn"
              onClick={async () => {
                try {
                  setDry(await api.hermesRegister(true));
                } catch (e) {
                  toast.error('MCP registration check failed', e);
                }
              }}
            >
              Register MCP (dry run)
            </button>
          </div>
          {dry && (
            <div className="stack">
              <pre className="pre">{JSON.stringify(dry, null, 2)}</pre>
              <div>
                <button
                  type="button"
                  className="btn primary sm"
                  onClick={async () => {
                    try {
                      await api.hermesRegister(false);
                      toast.success('MCP server registered with Hermes');
                      setDry(null);
                      st.reload();
                    } catch (e) {
                      toast.error('Registration failed', e);
                    }
                  }}
                >
                  Apply registration
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function FragmentKV({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>{k}</dt>
      <dd className="wrap-any">{v}</dd>
    </>
  );
}
