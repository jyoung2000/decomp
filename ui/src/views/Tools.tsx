import { useEffect, useId, useState } from 'react';
import { ConfirmDialog } from '../components/Dialog';
import { Loading } from '../components/Empty';
import { ErrorCallout } from '../components/ErrorCallout';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toasts';
import { bytes } from '../lib/format';
import { useApi, useResource } from '../lib/store';
import type { ToolSetupEntry, ToolSetupSnapshot } from '../lib/types';

const CHIP: Record<string, [string, string, string]> = {
  not_installed: ['missing', 'Not installed', 'Not on this computer yet.'],
  installed: ['verified', 'Installed', 'Installed and checked against its pinned checksum.'],
  corrupt: ['failed', 'Damaged', 'Files are missing or changed since installation. Reinstall to repair.'],
  blocked_unverified: ['blocked', 'Blocked', 'No verified checksum is pinned, so installing is refused.'],
  update_available: ['stale', 'Update available', 'A newer pinned version is available.'],
  installing: ['running', 'Installing', 'Installing now.'],
};

const PHASE: Record<string, string> = {
  queued: 'Waiting to start',
  downloading: 'Downloading',
  verifying: 'Verifying checksum',
  extracting: 'Unpacking',
  checking: 'Checking that the tool starts',
  activating: 'Finishing',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export function ToolsView() {
  const api = useApi();
  const res = useResource(() => api.toolsSetup(), [api]);
  const installing = !!res.data?.tools.some((t) => t.status === 'installing');
  const reload = res.reload;
  useEffect(() => {
    if (!installing) return;
    const id = window.setInterval(reload, 700);
    return () => window.clearInterval(id);
  }, [installing, reload]);
  return (
    <div className="page" data-testid="tools">
      <div className="page-head">
        <div>
          <h1>Tools</h1>
          <p className="lead">
            Rebuild Studio uses a few free open-source programs to read the original app. Install them here with one click: each download is checked against a pinned checksum, and nothing is installed system-wide.
          </p>
        </div>
        <button type="button" className="btn sm" onClick={res.reload}>
          Refresh
        </button>
      </div>
      {res.error ? (
        <ErrorCallout error={res.error} title="Tools could not be listed" onRetry={res.reload} />
      ) : res.loading && !res.data ? (
        <Loading what="tools" />
      ) : res.data ? (
        <>
          <p className="small muted" style={{ marginBottom: 12 }}>
            Installed under <span className="mono wrap-any">{res.data.tools_dir}</span>
          </p>
          <div className="grid grid-2">
            {res.data.tools.map((t) => (
              <ToolCard key={t.name} tool={t} snapshot={res.data!} onChanged={res.reload} />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

function ToolCard({ tool, snapshot, onChanged }: { tool: ToolSetupEntry; snapshot: ToolSetupSnapshot; onChanged: () => void }) {
  const api = useApi();
  const toast = useToast();
  const uid = useId();
  const [busy, setBusy] = useState(false);
  const [fromFile, setFromFile] = useState(false);
  const [path, setPath] = useState('');
  const [confirmRemove, setConfirmRemove] = useState(false);
  const job = tool.job;
  const status = tool.status;
  const [tone, label, meaning] = CHIP[status] ?? ['unprobed', status, ''];
  const otherBusy = snapshot.busy && status !== 'installing';
  const blocked = status === 'blocked_unverified';
  const error = status !== 'installing' && job?.error ? job.error : null;
  const cancelled = status !== 'installing' && job?.phase === 'cancelled';
  const installLabel = status === 'corrupt' ? 'Repair' : status === 'update_available' ? 'Update' : error ? 'Retry' : 'Install';
  const canInstall = status === 'not_installed' || status === 'corrupt' || status === 'update_available' || blocked;
  const disabledWhy = blocked
    ? tool.blocked_reason || 'No verified checksum is pinned for this download, so Rebuild Studio refuses to install it.'
    : otherBusy
      ? 'Another tool is installing. Wait for it to finish or cancel it.'
      : null;
  const whyId = `${uid}-why`;

  const act = async (fn: () => Promise<unknown>, failTitle: string) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toast.error(failTitle, e);
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const pct = job && status === 'installing' ? job.percent : null;
  const progressText =
    job && status === 'installing'
      ? job.bytes_total > 0
        ? `${bytes(job.bytes_done)} of ${bytes(job.bytes_total)}${pct != null ? ` · ${Math.floor(pct)}%` : ''}`
        : `${bytes(job.bytes_done)} done`
      : '';

  return (
    <section className="card" aria-labelledby={`${uid}-h`} data-testid={`tool-${tool.name}`} data-status={status}>
      <div className="card-head">
        <h3 id={`${uid}-h`}>{tool.title}</h3>
        <StatusChip status={tone} label={label} title={meaning} />
      </div>
      <p>{tool.purpose}</p>
      <p className="small muted">
        Version {tool.installed_version && tool.installed_version !== tool.version ? `${tool.installed_version} (installed), ${tool.version} (available)` : tool.version}
        {' · '}Download {bytes(tool.size_bytes)}
        {tool.license ? ` · License ${tool.license}` : ''}
        {tool.optional ? ' · Optional' : ''}
      </p>
      {tool.requires.length > 0 && <p className="xs muted">Also installs: {tool.requires.join(', ')} if missing.</p>}

      {status === 'installing' && job && (
        <div className="progress" style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 500 }}>{PHASE[job.phase] ?? job.phase}{job.message && job.message !== PHASE[job.phase] ? ` – ${job.message}` : ''}</div>
          {pct != null ? (
            <div className="bar" role="progressbar" aria-label={`${tool.title} install progress`} aria-valuemin={0} aria-valuemax={job.bytes_total} aria-valuenow={job.bytes_done} aria-valuetext={progressText}>
              <span style={{ width: `${Math.min(100, pct)}%` }} />
            </div>
          ) : (
            <div className="bar unknown" role="progressbar" aria-label={`${tool.title} install progress`} aria-valuetext={progressText || 'working'} />
          )}
          <div className="nums" data-testid={`tool-${tool.name}-nums`}>{progressText}</div>
        </div>
      )}

      {error && (
        <div className="callout bad" role="alert" style={{ marginTop: 8 }}>
          <div><strong>What happened: </strong>{error.message}</div>
          <div><strong>Affected: </strong>{error.affected ?? tool.title}. Nothing was installed.</div>
          <div><strong>Next: </strong>{error.next_action ?? 'Retry.'}</div>
        </div>
      )}
      {cancelled && (
        <div className="callout info" role="status" style={{ marginTop: 8 }}>
          Install cancelled. Nothing was installed and the partial download was deleted.
        </div>
      )}
      {status === 'corrupt' && (
        <div className="callout warn" role="alert" style={{ marginTop: 8 }}>
          Files for this tool are missing or were changed after installation. Press Repair to reinstall a verified copy.
        </div>
      )}
      {disabledWhy && (
        <p className="small muted" id={whyId} style={{ marginTop: 8 }}>
          {disabledWhy}
        </p>
      )}

      <div className="btn-group" style={{ marginTop: 12 }}>
        {status === 'installing' && (
          <button type="button" className="btn" disabled={busy} onClick={() => act(() => api.cancelToolInstall(tool.name), 'Cancel failed')}>
            Cancel
          </button>
        )}
        {canInstall && (
          <button
            type="button"
            className="btn primary"
            disabled={busy || !!disabledWhy}
            aria-describedby={disabledWhy ? whyId : undefined}
            data-tooltip={disabledWhy ?? undefined}
            onClick={() => act(() => api.installTool(tool.name), `${tool.title} could not be installed`)}
          >
            {installLabel}
          </button>
        )}
        {canInstall && !blocked && (
          <button
            type="button"
            className="btn"
            disabled={busy || otherBusy}
            aria-describedby={otherBusy ? whyId : undefined}
            aria-expanded={fromFile}
            onClick={() => setFromFile((v) => !v)}
          >
            Install from file…
          </button>
        )}
        {(status === 'installed' || status === 'update_available' || status === 'corrupt') && (
          <button type="button" className="btn" disabled={busy || otherBusy} aria-describedby={otherBusy ? whyId : undefined} onClick={() => setConfirmRemove(true)}>
            Remove
          </button>
        )}
      </div>

      {fromFile && status !== 'installing' && (
        <div className="field" style={{ marginTop: 12 }}>
          <label htmlFor={`${uid}-path`}>Downloaded file</label>
          <div className="input-with-btn">
            <input
              id={`${uid}-path`}
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder={`C:\\Users\\you\\Downloads\\${tool.file_name ?? 'file.zip'}`}
              spellCheck={false}
              autoComplete="off"
              aria-describedby={`${uid}-file-hint`}
            />
            <button
              type="button"
              className="btn primary"
              disabled={busy || !path.trim()}
              aria-describedby={!path.trim() ? `${uid}-file-hint` : undefined}
              onClick={() =>
                act(async () => {
                  await api.installToolFromFile(tool.name, path.trim());
                  setFromFile(false);
                }, 'Install from file failed')
              }
            >
              Install this file
            </button>
          </div>
          <span className="hint" id={`${uid}-file-hint`}>
            {path.trim() ? 'The file is checked against the pinned checksum before anything is unpacked.' : 'Type the full path of the file first. '}
            {' '}Expected: <span className="mono">{tool.file_name}</span>
            {tool.url && (
              <>
                {' '}from <span className="mono wrap-any">{tool.url}</span>
              </>
            )}
            .
          </span>
        </div>
      )}
      {error?.code === 'offline' && !fromFile && (
        <p className="small" style={{ marginTop: 8 }}>
          Offline? Download <span className="mono">{tool.file_name}</span> on another computer, copy it here, then choose <strong>Install from file…</strong>.
        </p>
      )}

      <ConfirmDialog
        open={confirmRemove}
        title={`Remove ${tool.title}?`}
        body={<>This deletes the installed copy under {tool.install_path}. You can install it again at any time.</>}
        confirmLabel="Remove"
        danger
        onClose={() => setConfirmRemove(false)}
        onConfirm={() => {
          setConfirmRemove(false);
          void act(() => api.removeTool(tool.name), `${tool.title} could not be removed`);
        }}
      />
    </section>
  );
}
