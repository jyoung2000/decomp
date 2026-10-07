import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { AI_TASKS, presetLadders, rungKey } from '../../lib/ai';
import { bytes, shortHash } from '../../lib/format';
import { useApi, useResource } from '../../lib/store';
import { hasTauri, openPath } from '../../lib/tauri';
import type { DownloadedModel, HfFile, HfRepoFiles, HfSearchResult, LocalAiSnapshot, LocalJob, LocalModel, LocalServer, PresetResult } from '../../lib/types';
import { ConfirmDialog } from '../Dialog';
import { Loading } from '../Empty';
import { ErrorCallout } from '../ErrorCallout';
import { PathField } from '../PathField';
import { useToast } from '../Toasts';

const RECHECK_MS = 60_000;
const CTX_CHOICES = [8192, 16384, 32768, 65536, 131072];
const SHORT_TASK: Record<string, string> = { interpretation: 'Interpret', repair: 'Repair', visual_review: 'Screenshots', verification_assist: 'Scenarios', knowledge: 'Knowledge' };

export const kTokens = (n: number | null | undefined) => (n ? (n >= 1024 ? `${Math.round(n / 1024)}k` : String(n)) : '—');
const fmtEta = (s: number | null | undefined) => (s == null ? '' : s < 60 ? `${Math.ceil(s)} s left` : `${Math.ceil(s / 60)} min left`);

/** Count of suitable models across running servers. */
export const suitableCount = (s: LocalAiSnapshot | undefined) => (s?.servers ?? []).filter((x) => x.found).reduce((a, x) => a + x.models.filter((m) => m.suitable).length, 0);

/**
 * "Local AI on this PC": detected servers and their models with plain-language suitability, "Detect again",
 * "Use detected local models" (preview, then apply) and the "Find and download models" panel.
 */
export function LocalAiSection({ onLadderChanged }: { onLadderChanged?: () => void }) {
  const api = useApi();
  const toast = useToast();
  // opening the page re-detects; afterwards a cheap re-check at most every 60 s while the page is visible
  const [mode, setMode] = useState<'detect' | 'cached'>('detect');
  const snap = useResource<LocalAiSnapshot>(() => (mode === 'detect' ? api.detectLocalAi() : api.localAi(55)), [api, mode]);
  const { reload } = snap;
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        setMode('cached');
        reload();
      }
    }, RECHECK_MS);
    return () => window.clearInterval(id);
  }, [reload]);
  const detectAgain = async () => {
    setBusy(true);
    setMode('detect');
    try {
      reload();
    } finally {
      setBusy(false);
    }
  };
  const data = snap.data;
  const found = (data?.servers ?? []).filter((s) => s.found);
  return (
    <section className="card" aria-labelledby="local-ai-h" data-testid="local-ai">
      <div className="card-head">
        <h3 id="local-ai-h">Local AI on this PC</h3>
        <button type="button" className="btn sm" onClick={detectAgain} disabled={snap.loading} aria-describedby={snap.loading ? 'local-ai-checking' : undefined}>
          {snap.loading ? 'Checking…' : 'Detect again'}
        </button>
        {snap.loading && (
          <span className="sr-only" id="local-ai-checking">
            Detection is running.
          </span>
        )}
      </div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        Rebuild Studio looks for Ollama, LM Studio and llama.cpp running on this computer (only on this PC’s own address; nothing is sent anywhere). Local models are free and private, but slower and less capable than large cloud models.
      </p>
      {snap.error ? (
        <ErrorCallout error={snap.error} title="Local AI detection failed" onRetry={detectAgain} tone="warn" />
      ) : !data ? (
        <Loading what="local AI servers" />
      ) : (
        <div className="stack-lg">
          <ServerList servers={data.servers} />
          {found.length > 0 && <UseDetected snap={data} onApplied={() => { onLadderChanged?.(); setMode('cached'); reload(); }} />}
          {found.map((s) => (
            <ModelTable key={s.kind} server={s} />
          ))}
          {found.some((s) => s.kind === 'ollama') && <ContextCap value={data.num_ctx_cap ?? 32768} onSaved={() => { setMode('detect'); reload(); toast.success('Max context saved'); }} />}
        </div>
      )}
      <FindModels snap={data} onRegistered={() => { setMode('detect'); reload(); onLadderChanged?.(); }} busy={busy} />
    </section>
  );
}

function ServerList({ servers }: { servers: LocalServer[] }) {
  return (
    <ul className="list" aria-label="Local AI servers" data-testid="local-servers">
      {servers.map((s) => (
        <li key={s.kind} className="row small" data-testid={`local-server-${s.kind}`}>
          {s.found ? <span className="chip ok">Running</span> : <span className="chip muted">Not running</span>}
          <strong>{s.name}</strong>
          {s.found ? (
            <span className="muted">
              {s.version ? `version ${s.version} · ` : ''}
              {s.models.length} model{s.models.length === 1 ? '' : 's'} · <span className="mono">{s.endpoint}</span>
              {s.kind === 'ollama' && s.models_folder ? (
                <>
                  {' '}
                  · models stored in <span className="mono wrap-any">{s.models_folder}</span>
                </>
              ) : null}
            </span>
          ) : (
            <span className="muted">
              not found at <span className="mono">{s.endpoint}</span>
              {s.install_page ? (
                <>
                  {' '}
                  · get it from{' '}
                  <a href={s.install_page} target="_blank" rel="noreferrer">
                    {new URL(s.install_page).hostname}
                  </a>{' '}
                  (Rebuild Studio does not install it for you)
                </>
              ) : null}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function capsText(m: LocalModel): string {
  const c = m.capabilities ?? {};
  const out: string[] = [];
  if (c.tools) out.push('tools');
  if (c.vision) out.push('images');
  if (c.thinking) out.push('thinking');
  if (c.completion === false) out.push('embeddings only');
  return out.length ? out.join(', ') : c.source === 'unknown' || !c.source ? 'not reported' : 'text';
}

function ModelTable({ server }: { server: LocalServer }) {
  if (!server.models.length)
    return (
      <p className="small muted" data-testid={`local-models-${server.kind}`}>
        {server.name} is running but has no models yet. {server.kind === 'ollama' ? 'Pull one by name or download one below.' : 'Load or download a model in it, then press Detect again.'}
      </p>
    );
  return (
    <div className="table-wrap">
      <table className="table" aria-label={`Models in ${server.name}`} data-testid={`local-models-${server.kind}`}>
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Size</th>
            <th scope="col">Context</th>
            <th scope="col">Can take</th>
            <th scope="col">Good for</th>
            <th scope="col">Use</th>
          </tr>
        </thead>
        <tbody>
          {server.models.map((m) => (
            <tr key={m.id} data-testid={`local-model-${m.id}`}>
              <td>
                <div className="mono">{m.id}</div>
                <div className="xs muted">
                  {[m.parameter_size ?? (m.parameter_b ? `${m.parameter_b}B` : null), m.quantization].filter(Boolean).join(' · ') || ' '}
                </div>
              </td>
              <td className="num small">{m.size_bytes ? bytes(m.size_bytes) : '—'}</td>
              <td className="small">
                {kTokens(m.context_window)}
                {m.effective_context && m.context_window && m.effective_context < m.context_window ? <div className="xs muted">uses up to {kTokens(m.effective_context)}</div> : null}
              </td>
              <td className="small">{capsText(m)}</td>
              <td>
                <span className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
                  {AI_TASKS.map((t) => {
                    const fit = m.tasks?.[t.id];
                    if (!fit) return null;
                    return (
                      <span key={t.id} className={`chip ${fit.ok ? 'ok' : 'muted'}`} title={`${t.label}: ${fit.ok ? 'yes' : 'no'}${fit.note ? ` — ${fit.note}` : ''}`}>
                        {fit.ok ? '' : 'no '}
                        {SHORT_TASK[t.id] ?? t.label}
                        <span className="sr-only">{fit.note ? ` (${fit.note})` : ''}</span>
                      </span>
                    );
                  })}
                  {m.quick_only ? <span className="chip warn">Quick tasks only</span> : null}
                </span>
              </td>
              <td className="small">
                {m.suitable ? <span className="chip ok">Use</span> : <span className="chip bad">Not suitable</span>}
                <div className="xs muted">{m.summary}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UseDetected({ snap, onApplied }: { snap: LocalAiSnapshot; onApplied: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [preview, setPreview] = useState<(PresetResult & { replaces_user_ladder?: boolean }) | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const preset = snap.recommended_preset ?? 'all_local';
  const n = snap.suitable_models ?? suitableCount(snap);
  const show = async () => {
    setBusy(true);
    setError(null);
    try {
      setPreview(await api.useLocalModels(false, preset));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    setBusy(true);
    try {
      await api.useLocalModels(true, preset);
      toast.success('Local models are now used for AI tasks', 'The model ladder below was updated.');
      setPreview(null);
      onApplied();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const ladders = preview ? presetLadders(preview) : {};
  return (
    <div className="stack" data-testid="use-detected">
      <div className="row">
        <button type="button" className={`btn ${snap.recommend_use ? 'primary' : ''}`} onClick={show} disabled={busy || n === 0} aria-describedby="use-detected-why">
          Use detected local models
        </button>
        <span className="small muted" id="use-detected-why">
          {n === 0
            ? 'None of the detected models is suitable for Rebuild Studio tasks (see the table).'
            : `${n} suitable model${n === 1 ? '' : 's'} · runs on this PC, free. ${preset === 'local_first' ? 'Local models first, your cloud models as fallback.' : 'Local models only.'} You see the change before it is saved.`}
        </span>
      </div>
      {snap.advice ? <p className="small" data-testid="local-advice">{snap.advice}</p> : null}
      {error ? <ErrorCallout error={error} title="Could not prepare the local model ladder" /> : null}
      {preview && (
        <div className="callout info" role="region" aria-label="Preview of the local model ladder" aria-live="polite" data-testid="use-detected-preview">
          <div className="ttl">Preview (not applied yet)</div>
          {preview.replaces_user_ladder ? <div className="small"><strong>This replaces the ladder you set up yourself.</strong></div> : null}
          <ul className="list small">
            {AI_TASKS.map((t) => {
              const es = ladders[t.id] ?? [];
              return (
                <li key={t.id}>
                  <strong>{t.label}: </strong>
                  {es.length ? es.map((e, i) => <span key={rungKey(e)} className="mono">{`${i ? ', ' : ''}${i + 1}. ${e.model}`}</span>) : <span className="muted">no suitable local model; this task is skipped</span>}
                </li>
              );
            })}
          </ul>
          {(preview.warnings ?? []).map((w) => (
            <div key={w} className="small muted">
              {w}
            </div>
          ))}
          <div className="btn-group">
            <button type="button" className="btn primary sm" onClick={apply} disabled={busy}>
              Apply
            </button>
            <button type="button" className="btn sm" onClick={() => setPreview(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function ContextCap({ value, onSaved }: { value: number; onSaved: () => void }) {
  const api = useApi();
  const toast = useToast();
  const id = useId();
  return (
    <div className="field" style={{ maxWidth: 520 }}>
      <label htmlFor={id}>Max context for Ollama models</label>
      <select
        id={id}
        value={value}
        onChange={async (e) => {
          try {
            await api.putLocalAiConfig({ num_ctx_cap: Number(e.target.value) });
            onSaved();
          } catch (err) {
            toast.error('Could not save the max context', err);
          }
        }}
      >
        {CTX_CHOICES.map((c) => (
          <option key={c} value={c}>
            {kTokens(c)} tokens{c === 32768 ? ' (default)' : ''}
          </option>
        ))}
      </select>
      <span className="hint">
        Rebuild Studio asks Ollama for exactly the context each request needs, up to this limit and the model’s own maximum. A bigger limit lets larger code fit but needs more GPU/RAM. A request that does not fit is refused, never silently cut.
      </span>
    </div>
  );
}

// ================================================================================== find and download
function FindModels({ snap, onRegistered }: { snap: LocalAiSnapshot | undefined; onRegistered: () => void; busy: boolean }) {
  const api = useApi();
  const toast = useToast();
  const cfg = useResource(() => api.localAiConfig(), [api]);
  const jobs = useResource(() => api.localJobs(), [api]);
  const downloaded = useResource(() => api.downloadedModels(), [api]);
  const running = !!jobs.data?.some((j) => !j.finished);
  const reloadJobs = jobs.reload;
  const reloadDownloaded = downloaded.reload;
  const wasRunning = useRef(false);
  useEffect(() => {
    if (!running) {
      if (wasRunning.current) {
        wasRunning.current = false;
        reloadDownloaded();
        onRegistered();
      }
      return;
    }
    wasRunning.current = true;
    const id = window.setInterval(reloadJobs, 700);
    return () => window.clearInterval(id);
  }, [running, reloadJobs, reloadDownloaded, onRegistered]);
  const ollamaUp = !!snap?.servers.some((s) => s.kind === 'ollama' && s.found);
  return (
    <div className="stack-lg" style={{ marginTop: 16 }} data-testid="find-models">
      <h4>Find and download models</h4>
      <p className="small muted">
        Search GGUF models on Hugging Face (huggingface.co). Each file is checked against the SHA-256 Hugging Face publishes before it is used, then added to Ollama so it shows up in the model picker.
      </p>
      <PullByName ollamaUp={ollamaUp} folder={cfg.data?.ollama_models_folder} onStarted={reloadJobs} />
      <Search
        defaultDir={cfg.data?.models_dir ?? ''}
        hasToken={!!cfg.data?.has_hf_token}
        onStarted={reloadJobs}
        onTokenSaved={cfg.reload}
        onDefaultSaved={() => {
          cfg.reload();
          toast.success('Default model folder saved');
        }}
      />
      <Jobs jobs={jobs.data ?? []} onChanged={reloadJobs} />
      <Downloaded rows={downloaded.data ?? []} error={downloaded.error} ollamaUp={ollamaUp} onChanged={() => { reloadDownloaded(); onRegistered(); }} />
    </div>
  );
}

function PullByName({ ollamaUp, folder, onStarted }: { ollamaUp: boolean; folder?: string; onStarted: () => void }) {
  const api = useApi();
  const id = useId();
  const [name, setName] = useState('');
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api.pullOllamaModel(name.trim());
      setName('');
      onStarted();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <form className="stack" onSubmit={submit} aria-label="Pull from the Ollama library by name">
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div className="field" style={{ flex: '1 1 260px' }}>
          <label htmlFor={id}>Pull from the Ollama library by name</label>
          <input id={id} type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="for example qwen2.5-coder:7b" spellCheck={false} autoComplete="off" aria-describedby={`${id}-hint`} />
        </div>
        <button type="submit" className="btn" disabled={!ollamaUp || !name.trim()} aria-describedby={`${id}-hint`}>
          Pull
        </button>
      </div>
      <span className="hint" id={`${id}-hint`}>
        {ollamaUp
          ? `Ollama downloads it into its own model folder${folder ? ` (${folder})` : ''}, not the folder below.`
          : 'Needs Ollama running on this PC. Start it, then press Detect again.'}
        {ollamaUp && !name.trim() ? ' Type a model name to enable Pull.' : ''}
      </span>
      {error ? <ErrorCallout error={error} title="The pull did not start" /> : null}
    </form>
  );
}

function Search({ defaultDir, hasToken, onStarted, onTokenSaved, onDefaultSaved }: { defaultDir: string; hasToken: boolean; onStarted: () => void; onTokenSaved: () => void; onDefaultSaved: () => void }) {
  const api = useApi();
  const qid = useId();
  const [q, setQ] = useState('');
  const [results, setResults] = useState<HfSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [repo, setRepo] = useState<HfRepoFiles | null>(null);
  const [loadingRepo, setLoadingRepo] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    setSearching(true);
    setError(null);
    setRepo(null);
    try {
      setResults((await api.searchModels(q.trim())).results);
    } catch (err) {
      setError(err);
    } finally {
      setSearching(false);
    }
  };
  const choose = async (r: HfSearchResult) => {
    setLoadingRepo(r.repo);
    setError(null);
    try {
      setRepo(await api.modelFiles(r.repo));
    } catch (err) {
      setError(err);
    } finally {
      setLoadingRepo(null);
    }
  };
  return (
    <div className="stack">
      <form className="row" style={{ alignItems: 'flex-end' }} onSubmit={submit} role="search" aria-label="Search Hugging Face models">
        <div className="field" style={{ flex: '1 1 260px' }}>
          <label htmlFor={qid}>Search models on Hugging Face</label>
          <input id={qid} type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="for example qwen2.5 coder 7b" />
        </div>
        <button type="submit" className="btn" disabled={searching || !q.trim()} aria-describedby={`${qid}-hint`}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </form>
      <span className="hint" id={`${qid}-hint`}>
        {q.trim() ? 'Sorted by downloads. Only GGUF models (the format local servers run) are listed.' : 'Type part of a model name to enable Search.'}
      </span>
      {error ? <ErrorCallout error={error} title="Hugging Face search failed" /> : null}
      {results && (
        <ul className="list" aria-label="Search results" data-testid="hf-results">
          {results.length === 0 && <li className="small muted">No GGUF models match. Try a shorter name.</li>}
          {results.map((r) => (
            <li key={r.repo} className="row small" data-testid={`hf-result-${r.repo}`}>
              <strong className="mono wrap-any">{r.repo}</strong>
              <span className="muted">
                {(r.downloads ?? 0).toLocaleString()} downloads · {r.likes ?? 0} likes
              </span>
              <span className={`chip ${r.license_permissive ? 'ok' : 'warn'}`}>{r.license ? `license: ${r.license}` : 'license not stated'}</span>
              {r.gated ? <span className="chip warn">gated (needs a Hugging Face account)</span> : null}
              <button type="button" className="btn sm" onClick={() => choose(r)} disabled={loadingRepo != null} aria-label={`Choose a file from ${r.repo}`}>
                {loadingRepo === r.repo ? 'Loading files…' : 'Choose file'}
              </button>
            </li>
          ))}
        </ul>
      )}
      {repo && <RepoDownload repo={repo} defaultDir={defaultDir} hasToken={hasToken} onStarted={onStarted} onTokenSaved={async () => { onTokenSaved(); setRepo(await api.modelFiles(repo.repo)); }} onDefaultSaved={onDefaultSaved} onClose={() => setRepo(null)} />}
    </div>
  );
}

function RepoDownload({ repo, defaultDir, hasToken, onStarted, onTokenSaved, onDefaultSaved, onClose }: { repo: HfRepoFiles; defaultDir: string; hasToken: boolean; onStarted: () => void; onTokenSaved: () => void; onDefaultSaved: () => void; onClose: () => void }) {
  const api = useApi();
  const toast = useToast();
  const uid = useId();
  const usable = repo.files.filter((f) => f.downloadable);
  const preferred = usable.find((f) => f.quant === 'Q4_K_M') ?? usable[0];
  const [pick, setPick] = useState<string>(preferred?.path ?? '');
  const [dir, setDir] = useState(defaultDir);
  const [ack, setAck] = useState(false);
  const [token, setToken] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [starting, setStarting] = useState(false);
  useEffect(() => setDir((d) => d || defaultDir), [defaultDir]);
  const chosen = repo.files.find((f) => f.path === pick);
  const blockers: string[] = [];
  if (!chosen) blockers.push('Pick a file above.');
  if (repo.gated && repo.needs_token) blockers.push('This model is gated: add a Hugging Face token first.');
  if (repo.license_ack_required && !ack) blockers.push('Tick the license box to confirm you accept it.');
  if (!dir.trim()) blockers.push('Choose a destination folder.');
  const start = async () => {
    if (!chosen) return;
    setStarting(true);
    setError(null);
    try {
      await api.startModelDownload({ repo: repo.repo, path: chosen.path, dest_dir: dir.trim() || undefined, accept_license: ack });
      toast.success('Download started', chosen.path);
      onStarted();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setStarting(false);
    }
  };
  return (
    <div className="callout info" role="region" aria-label={`Download from ${repo.repo}`} data-testid="hf-repo">
      <div className="ttl">
        {repo.repo}{' '}
        <a href={repo.page} target="_blank" rel="noreferrer" className="small">
          model page
        </a>
      </div>
      <fieldset>
        <legend className="small">Quantization (smaller = faster and lighter, larger = better answers)</legend>
        <div className="stack" role="radiogroup" aria-label="Files">
          {repo.files.map((f: HfFile) => (
            <label key={f.path} className="row small" style={{ gap: 8 }}>
              <input type="radio" name={`${uid}-file`} value={f.path} checked={pick === f.path} disabled={!f.downloadable} onChange={() => setPick(f.path)} aria-describedby={`${uid}-${f.path}`} />
              <span className="mono">{f.quant ?? f.path}</span>
              <span>{bytes(f.size_bytes)}</span>
              {f.ram_hint_gb ? <span className="muted">needs about {f.ram_hint_gb} GB of RAM or GPU memory</span> : null}
              <span className="muted xs" id={`${uid}-${f.path}`}>
                {f.downloadable ? f.path : f.note}
              </span>
            </label>
          ))}
          {repo.files.length === 0 && <span className="small muted">This repository has no GGUF files.</span>}
        </div>
      </fieldset>
      <PathField label="Destination folder" name="models_dir" value={dir} onChange={setDir} hint="Paths with spaces are fine. Free space is checked before downloading." />
      {dir.trim() && dir.trim() !== defaultDir && (
        <div>
          <button
            type="button"
            className="btn sm"
            onClick={async () => {
              try {
                await api.putLocalAiConfig({ models_dir: dir.trim() });
                onDefaultSaved();
              } catch (e) {
                setError(e);
              }
            }}
          >
            Use this folder by default
          </button>
        </div>
      )}
      {repo.gated && (
        <div className="small" data-testid="hf-gated">
          <strong>Gated model.</strong> Hugging Face only gives it to signed-in users who accepted its terms on the model page. {hasToken ? 'Your Hugging Face token will be used.' : 'Accept the terms there, then paste a Hugging Face read token (optional; stored in the credential store, never shown again).'}
          {!hasToken && (
            <div className="row" style={{ alignItems: 'flex-end' }}>
              <div className="field" style={{ flex: '1 1 240px' }}>
                <label htmlFor={`${uid}-tok`}>Hugging Face token</label>
                <input id={`${uid}-tok`} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
              </div>
              <button
                type="button"
                className="btn sm"
                disabled={!token.trim()}
                aria-describedby={`${uid}-tokhint`}
                onClick={async () => {
                  try {
                    await api.putHfToken(token.trim());
                    setToken('');
                    onTokenSaved();
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                Save token
              </button>
              <span className="sr-only" id={`${uid}-tokhint`}>
                Paste a token to enable Save token.
              </span>
            </div>
          )}
        </div>
      )}
      {repo.license_ack_required ? (
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>
            I have read and accept the license ({repo.license ?? 'not stated'}). {repo.license_note}
          </span>
        </label>
      ) : (
        <div className="small muted">License: {repo.license} (permissive).</div>
      )}
      {error ? <ErrorCallout error={error} title="The download did not start" /> : null}
      <div className="btn-group">
        <button type="button" className="btn primary sm" onClick={start} disabled={starting || blockers.length > 0} aria-describedby={`${uid}-block`}>
          {starting ? 'Starting…' : chosen ? `Download ${bytes(chosen.size_bytes)}` : 'Download'}
        </button>
        <button type="button" className="btn sm" onClick={onClose}>
          Close
        </button>
      </div>
      <span className="small muted" id={`${uid}-block`}>
        {blockers.join(' ')}
      </span>
    </div>
  );
}

const PHASE: Record<string, string> = {
  queued: 'Waiting to start',
  downloading: 'Downloading',
  verifying: 'Verifying SHA-256',
  registering: 'Adding to Ollama',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

function Jobs({ jobs, onChanged }: { jobs: LocalJob[]; onChanged: () => void }) {
  const api = useApi();
  const toast = useToast();
  const visible = jobs.slice(0, 6);
  if (!visible.length) return null;
  return (
    <ul className="list" aria-label="Downloads in progress and recent" data-testid="local-jobs">
      {visible.map((j) => {
        const pct = j.percent != null ? Math.min(100, j.percent) : null;
        const nums = j.bytes_total ? `${bytes(j.bytes_done)} of ${bytes(j.bytes_total)}${pct != null ? ` · ${Math.floor(pct)}%` : ''}` : j.bytes_done ? `${bytes(j.bytes_done)}` : '';
        const speed = !j.finished && j.speed_bps ? ` · ${bytes(j.speed_bps)}/s · ${fmtEta(j.eta_s)}` : '';
        return (
          <li key={j.job_id} className="stack small" data-testid={`local-job-${j.job_id}`}>
            <div className="row">
              <strong className="mono wrap-any">{j.title}</strong>
              <span className={`chip ${j.phase === 'done' ? 'ok' : j.phase === 'failed' ? 'bad' : j.phase === 'cancelled' ? 'muted' : 'run'}`}>{PHASE[j.phase] ?? j.phase}</span>
              {!j.finished && (
                <button
                  type="button"
                  className="btn sm"
                  onClick={async () => {
                    try {
                      await api.cancelLocalJob(j.job_id);
                      onChanged();
                    } catch (e) {
                      toast.error('Cancel failed', e);
                    }
                  }}
                  aria-label={`Cancel ${j.title}`}
                >
                  Cancel
                </button>
              )}
            </div>
            {!j.finished && (
              <div className={`bar${pct == null ? ' unknown' : ''}`} role="progressbar" aria-label={`${j.title} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? undefined} aria-valuetext={nums || 'starting'}>
                {pct != null && <span style={{ width: `${pct}%` }} />}
              </div>
            )}
            <div className="muted">
              {nums}
              {speed}
              {j.message ? ` · ${j.message}` : ''}
            </div>
            {j.error && (
              <ErrorCallout
                error={{ code: j.error.code, message: j.error.message, next_action: j.error.next_action }}
                title={j.error.retryable ? 'The download stopped (you can retry)' : 'The download failed'}
                tone={j.error.retryable ? 'warn' : 'bad'}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Downloaded({ rows, error, ollamaUp, onChanged }: { rows: DownloadedModel[]; error: unknown; ollamaUp: boolean; onChanged: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [del, setDel] = useState<DownloadedModel | null>(null);
  const [alsoOllama, setAlsoOllama] = useState(true);
  const native = hasTauri();
  const open = useCallback(
    async (p: string) => {
      try {
        if (!(await openPath(p))) toast.push({ kind: 'info', title: 'Folder', message: p });
      } catch (e) {
        toast.error('Could not open the folder', e);
      }
    },
    [toast],
  );
  if (error) return <ErrorCallout error={error} title="Downloaded models could not be listed" tone="warn" />;
  if (!rows.length) return <p className="small muted">No models downloaded yet.</p>;
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Downloaded models" data-testid="downloaded-models">
        <thead>
          <tr>
            <th scope="col">File</th>
            <th scope="col">Size</th>
            <th scope="col">Status</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} data-testid={`downloaded-${r.id}`}>
              <td>
                <div className="mono wrap-any">{r.file}</div>
                <div className="xs muted wrap-any">{r.path}</div>
                <div className="xs muted mono" title={r.sha256}>
                  sha256 {shortHash(r.sha256, 16)}
                </div>
              </td>
              <td className="num small">{bytes(r.size_bytes)}</td>
              <td className="small">
                {r.registered_as ? <span className="chip ok">In Ollama as {r.registered_as}</span> : <span className="chip warn">Not in a local server yet</span>}
                {r.exists === false && <span className="chip bad">File missing</span>}
                <div className="xs muted">{r.status_text}</div>
                {!r.registered_as && r.install_pages && !ollamaUp ? (
                  <div className="xs">
                    Install{' '}
                    <a href={r.install_pages.ollama} target="_blank" rel="noreferrer">
                      Ollama
                    </a>{' '}
                    or{' '}
                    <a href={r.install_pages.lmstudio} target="_blank" rel="noreferrer">
                      LM Studio
                    </a>
                    , then press Detect again.
                  </div>
                ) : null}
              </td>
              <td>
                <div className="btn-group">
                  <button type="button" className="btn sm" onClick={() => open(r.folder ?? r.path)} aria-label={`Open folder of ${r.file}`} title={native ? 'Open in Explorer' : 'Shows the folder path (the desktop app opens it)'}>
                    Open folder
                  </button>
                  {!r.registered_as && ollamaUp && (
                    <button
                      type="button"
                      className="btn sm"
                      onClick={async () => {
                        try {
                          await api.registerModel(r.id);
                          toast.success('Added to Ollama');
                          onChanged();
                        } catch (e) {
                          toast.error('Could not add it to Ollama', e);
                        }
                      }}
                    >
                      Add to Ollama
                    </button>
                  )}
                  <button type="button" className="btn sm danger" onClick={() => setDel(r)} aria-label={`Remove ${r.file}`}>
                    Remove
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ConfirmDialog
        open={!!del}
        title={`Remove ${del?.file ?? ''}?`}
        body={
          <div className="stack">
            <p>The file is deleted from {del?.folder ?? 'its folder'}.</p>
            {del?.registered_as && (
              <label className="row small" style={{ gap: 6 }}>
                <input type="checkbox" checked={alsoOllama} onChange={(e) => setAlsoOllama(e.target.checked)} />
                <span>Also remove {del.registered_as} from Ollama</span>
              </label>
            )}
          </div>
        }
        confirmLabel="Remove"
        danger
        onClose={() => setDel(null)}
        onConfirm={async () => {
          if (!del) return;
          try {
            await api.removeModel(del.id, !!del.registered_as && alsoOllama);
            toast.success('Model removed');
            onChanged();
          } catch (e) {
            toast.error('Remove failed', e);
          }
        }}
      />
    </div>
  );
}

/** One-line first-run hint: shown when a local server with suitable models is detected and no AI ladder exists yet. */
export function LocalAiHint({ onUse, testId = 'local-ai-hint' }: { onUse?: () => void; testId?: string }) {
  const api = useApi();
  const toast = useToast();
  const snap = useResource<LocalAiSnapshot>(() => api.localAi(), [api]);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const n = suitableCount(snap.data);
  if (!snap.data || n === 0 || snap.data.ladder?.state !== 'empty' || done) return null;
  return (
    <span className="small" data-testid={testId}>
      Local AI detected: {n} model{n === 1 ? '' : 's'} (runs on this PC, free) —{' '}
      <button
        type="button"
        className="btn sm"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.useLocalModels(true, snap.data?.recommended_preset);
            toast.success('Local models will be used for AI tasks', 'Review or change them under Connections.');
            setDone(true);
            onUse?.();
          } catch (e) {
            toast.error('Could not set up local models', e);
          } finally {
            setBusy(false);
          }
        }}
      >
        Use them
      </button>
    </span>
  );
}
