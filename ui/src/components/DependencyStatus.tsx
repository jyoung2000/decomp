import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ConfirmDialog } from './Dialog';
import { useToast } from './Toasts';
import { bytes } from '../lib/format';
import { useApi, useResource } from '../lib/store';
import type { DepFix, DepQueue, DependencyReport } from '../lib/types';

const TONE: Record<string, 'ok' | 'warn' | 'bad'> = { ok: 'ok', warn: 'warn', busy: 'warn', error: 'bad' };
const WORD: Record<string, string> = { ok: 'All set', warn: 'Attention', busy: 'Installing', error: 'Action needed' };

const CHANGED = 'rs-dependencies-changed';
/** Tell every dependency view to refresh now (an install was started, a setting changed, …). */
export function dependenciesChanged() {
  window.dispatchEvent(new Event(CHANGED));
}

/** GET /health/dependencies: refreshed on navigation and after any install action, every second while an install runs,
 * every 5 s while something needs attention, every 30 s when all is well. */
export function useDependencyReport() {
  const api = useApi();
  const { pathname } = useLocation();
  const res = useResource(() => api.dependencies(), [api, pathname]);
  const running = !!res.data?.queue?.running;
  const calm = res.data?.overall === 'ok';
  const reload = res.reload;
  useEffect(() => {
    const id = window.setInterval(reload, running ? 1000 : calm ? 30000 : 5000);
    window.addEventListener(CHANGED, reload);
    return () => {
      window.clearInterval(id);
      window.removeEventListener(CHANGED, reload);
    };
  }, [running, calm, reload]);
  return res;
}

/** Runs a fix the controller proposed (install / repair / start Ollama / retry / …); repairs ask once first. */
export function useDepFix(onDone: () => void) {
  const api = useApi();
  const toast = useToast();
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<DepFix | null>(null);

  const go = async (fix: DepFix) => {
    setBusy(true);
    try {
      if (fix.kind === 'install' || fix.kind === 'repair') {
        await api.installDependencies({ items: fix.items, repair: fix.kind === 'repair' });
        toast.success(fix.kind === 'repair' ? 'Repair started' : 'Install started', 'Progress is shown on the Tools page and in the status bar.');
      } else if (fix.kind === 'start_ollama') {
        const r = await api.startOllama();
        if (r.running) toast.success('Ollama is running', r.message);
        else toast.error('Ollama is not answering yet', r.next_action ?? r.message);
      } else if (fix.kind === 'resume_queue' || fix.kind === 'retry_queue') {
        await api.retryDependencyInstall();
      } else if (fix.kind === 'recheck') {
        await api.dependencies({ refresh: true });
      } else if (fix.kind === 'open_tools') {
        nav('/tools');
      }
    } catch (e) {
      toast.error(`${fix.label} failed`, e);
    } finally {
      setBusy(false);
      onDone();
      dependenciesChanged();
    }
  };

  const run = (fix: DepFix) => {
    if (fix.confirm || fix.kind === 'repair') setConfirm(fix);
    else void go(fix);
  };

  const dialog = (
    <ConfirmDialog
      open={!!confirm}
      title={confirm?.label ?? 'Repair'}
      body={<p>The damaged copy is replaced with a fresh, checksum-verified download. Projects that use it pause until the repair finishes.</p>}
      confirmLabel={confirm?.label ?? 'Repair'}
      onClose={() => setConfirm(null)}
      onConfirm={() => {
        const f = confirm;
        setConfirm(null);
        if (f) void go(f);
      }}
    />
  );
  return { run, busy, dialog };
}

export function FixButton({ fix, run, busy, primary = true, testId }: { fix: DepFix; run: (f: DepFix) => void; busy: boolean; primary?: boolean; testId?: string }) {
  if (fix.kind === 'link' && fix.url) {
    return (
      <a className="btn sm" href={fix.url} target="_blank" rel="noreferrer" data-testid={testId}>
        {fix.label}
      </a>
    );
  }
  return (
    <button type="button" className={`btn sm${primary ? ' primary' : ''}`} disabled={busy} onClick={() => run(fix)} data-testid={testId}>
      {fix.label}
    </button>
  );
}

/** Asked once on first run: install what projects need automatically? (default off) */
export function FirstRunAsk({ report, onDone }: { report: DependencyReport; onDone: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const uid = useId();
  if (report.settings.asked) return null;
  const answer = async (yes: boolean) => {
    setBusy(true);
    try {
      const r = await api.putDependencySettings(yes);
      if (yes) toast.success('Automatic installs are on', r.install ? `Installing ${r.install.total} tool${r.install.total === 1 ? '' : 's'} now.` : 'Everything needed is already installed.');
    } catch (e) {
      toast.error('The choice could not be saved', e);
    } finally {
      setBusy(false);
      onDone();
      dependenciesChanged();
    }
  };
  return (
    <div className="banner warn" role="region" aria-labelledby={`${uid}-q`} data-testid="dep-first-run">
      <span id={`${uid}-q`}>
        <strong>Install the tools your projects need automatically?</strong> Rebuild Studio downloads only pinned, checksum-verified tools into its own tools folder
        {report.install_all ? ` (right now: ${report.install_all.label.replace(/^Install what's missing /, '')})` : ''}. It never installs anything else on your PC. You can change this later on the Tools page.
      </span>
      <span className="spacer" />
      <span className="btn-group">
        <button type="button" className="btn sm primary" disabled={busy} onClick={() => answer(true)}>
          Yes, install automatically
        </button>
        <button type="button" className="btn sm" disabled={busy} onClick={() => answer(false)}>
          No, I will click Install
        </button>
      </span>
    </div>
  );
}

/** Green / amber / red status bar with one plain sentence and the fix button. Hidden when the controller cannot report. */
export function DependencyStatusBar() {
  const res = useDependencyReport();
  const fixer = useDepFix(res.reload);
  const r = res.data;
  if (!r || res.error) return null;
  const tone = TONE[r.overall] ?? 'warn';
  return (
    <>
      <FirstRunAsk report={r} onDone={res.reload} />
      <footer className={`dep-bar ${tone}`} role="status" aria-live="polite" data-testid="dep-status" data-state={r.overall}>
        <span className={`dot ${tone}`} aria-hidden="true" />
        <strong className="dep-word">{WORD[r.overall] ?? r.overall}:</strong>
        <span className="dep-sentence">{r.sentence}</span>
        {r.next_action && <span className="muted">{r.next_action}</span>}
        <span className="spacer" />
        {r.queue.running && r.queue.percent != null && <span className="nums small">{Math.floor(r.queue.percent)}%</span>}
        {r.fix && <FixButton fix={r.fix} run={fixer.run} busy={fixer.busy} testId="dep-fix" />}
        {r.fix?.kind !== 'open_tools' && (
          <Link className="btn sm" to="/tools">
            Details
          </Link>
        )}
      </footer>
      {fixer.dialog}
    </>
  );
}

/** Combined progress of the install queue with per-item status, errors (what happened + next action), Cancel and Retry. */
export function QueueProgress({ queue, onChanged, title = 'Installing' }: { queue: DepQueue; onChanged: () => void; title?: ReactNode }) {
  const api = useApi();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!queue.items.length || queue.state === 'idle') return null;
  const act = async (fn: () => Promise<unknown>, fail: string) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toast.error(fail, e);
    } finally {
      setBusy(false);
      onChanged();
      dependenciesChanged();
    }
  };
  const heading =
    queue.state === 'running' ? title : queue.state === 'done' ? 'Install finished' : queue.state === 'interrupted' ? 'Install interrupted' : queue.state === 'cancelled' ? 'Install cancelled' : 'Install finished with errors';
  const progressText = `${queue.done} of ${queue.total} done · ${bytes(queue.bytes_done)} of ${bytes(queue.bytes_total)}`;
  return (
    <section className="card" aria-label="Install progress" data-testid="dep-queue" data-state={queue.state}>
      <div className="card-head">
        <h3>{heading}</h3>
        <span className="btn-group">
          {queue.running && (
            <button type="button" className="btn sm" disabled={busy} onClick={() => act(() => api.cancelDependencyInstall(), 'Cancel failed')}>
              Cancel
            </button>
          )}
          {!queue.running && queue.state !== 'done' && (
            <button type="button" className="btn sm primary" disabled={busy} onClick={() => act(() => api.retryDependencyInstall(), 'Retry failed')} data-testid="dep-retry">
              {queue.state === 'interrupted' ? 'Resume install' : 'Retry'}
            </button>
          )}
        </span>
      </div>
      <div className="progress">
        <div style={{ fontWeight: 500 }}>All items</div>
        {queue.percent != null ? (
          <div className="bar" role="progressbar" aria-label="Combined install progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(queue.percent)} aria-valuetext={progressText}>
            <span style={{ width: `${Math.min(100, queue.percent)}%` }} />
          </div>
        ) : (
          <div className="bar unknown" role="progressbar" aria-label="Combined install progress" aria-valuetext={progressText} />
        )}
        <div className="nums">{progressText}</div>
      </div>
      <ol className="dep-items">
        {queue.items.map((it) => (
          <li key={it.name} data-testid={`dep-item-${it.name}`} data-status={it.status}>
            <span className={`dot ${it.status === 'done' ? 'ok' : it.status === 'failed' || it.status === 'skipped' ? 'bad' : 'warn'}`} aria-hidden="true" />{' '}
            <strong>{it.title}</strong> <span className="muted">– {ITEM[it.status] ?? it.status}{it.status === 'installing' && it.message ? `: ${it.message}` : ''}</span>
            {it.error && (
              <div className="callout bad" role="alert" style={{ marginTop: 4 }}>
                <div>
                  <strong>What happened: </strong>
                  {it.error.message}
                </div>
                <div>
                  <strong>Next: </strong>
                  {it.error.next_action ?? 'Press Retry.'}
                </div>
              </div>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

const ITEM: Record<string, string> = {
  queued: 'waiting',
  installing: 'installing',
  done: 'installed',
  failed: 'failed',
  skipped: 'not installed (a tool it needs failed)',
  cancelled: 'cancelled, nothing installed',
  interrupted: 'interrupted when the app closed',
};
