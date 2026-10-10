import { useEffect, useState } from 'react';
import { FixButton, QueueProgress, dependenciesChanged, useDepFix } from './DependencyStatus';
import { useToast } from './Toasts';
import { bytes } from '../lib/format';
import { useApi, useResource } from '../lib/store';
import type { DepQueue, Preflight } from '../lib/types';

/** GET /cases/{id}/preflight (skipped while the project runs); polls while what it needs is being installed. */
export function usePreflight(caseId: string, running: boolean) {
  const api = useApi();
  const res = useResource(() => (caseId && !running ? api.preflight(caseId) : Promise.resolve(null)), [api, caseId, running]);
  const [queue, setQueue] = useState<DepQueue | null>(null);
  const installing = !!res.data?.installing;
  const reload = res.reload;
  useEffect(() => {
    if (!installing) return;
    let alive = true;
    const tick = () => {
      api.dependencyQueue().then((q) => alive && setQueue(q), () => undefined);
      reload();
    };
    tick();
    const id = window.setInterval(tick, 1500);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [installing, reload, api]);
  const blocked = !!res.data && !res.data.ok;
  useEffect(() => {
    // installs may also be started from the status bar or the Tools page: follow them until Start is allowed
    const id = blocked ? window.setInterval(reload, 5000) : undefined;
    window.addEventListener('rs-dependencies-changed', reload);
    return () => {
      if (id) window.clearInterval(id);
      window.removeEventListener('rs-dependencies-changed', reload);
    };
  }, [blocked, reload]);
  // an unreachable/older controller (no preflight endpoint) never blocks Start here; the server still checks on Start
  return { data: res.error ? null : res.data ?? null, queue: installing ? queue : null, reload };
}

/** "Before this project can start": what is missing and why, one "Install what's missing (N items, X MB)" action. */
export function PreflightCard({ pf, queue, onChanged }: { pf: Preflight; queue: DepQueue | null; onChanged: () => void }) {
  const api = useApi();
  const toast = useToast();
  const fixer = useDepFix(onChanged);
  const [busy, setBusy] = useState(false);
  const install = async (body: { case_id?: string; items?: string[] }, what: string) => {
    setBusy(true);
    try {
      await api.installDependencies(body);
      toast.success('Install started', `${what}. Start becomes available when it finishes.`);
    } catch (e) {
      toast.error('The install could not start', e);
    } finally {
      setBusy(false);
      onChanged();
      dependenciesChanged();
    }
  };
  const optional = pf.optional_install;
  if (pf.ok) {
    if (!optional) return null;
    return (
      <div className="callout info" role="status" data-testid="preflight-optional" style={{ marginBottom: 12 }}>
        <div>
          Optional for this project: {pf.optional.map((o) => `${o.title} (${o.why})`).join(', ')}.
        </div>
        <div className="btn-group">
          <button type="button" className="btn sm" disabled={busy} onClick={() => install({ items: optional.items }, optional.label)}>
            {optional.label.replace("Install what's missing", 'Install optional tools')}
          </button>
        </div>
      </div>
    );
  }
  const blocking = pf.services.filter((s) => pf.blocking_services.includes(s.name));
  return (
    <section className="callout bad" aria-labelledby="preflight-title" data-testid="preflight" style={{ marginBottom: 12 }}>
      <div className="ttl" id="preflight-title">
        Before this project can start
      </div>
      <div id="preflight-sentence">{pf.sentence}</div>
      {pf.missing.length > 0 && (
        <ul className="bullets">
          {pf.missing.map((m) => (
            <li key={m.name} data-testid={`preflight-missing-${m.name}`}>
              <strong>{m.title}</strong> – {m.why}
              {m.download_bytes ? ` (${bytes(m.download_bytes)} download)` : ''}
              {!m.installable && m.blocked_reason ? `. Cannot be installed: ${m.blocked_reason}` : ''}
            </li>
          ))}
        </ul>
      )}
      {blocking.map((s) => (
        <div key={s.name} data-testid={`preflight-service-${s.name}`}>
          <strong>{s.title ?? s.name}: </strong>
          {s.sentence} {s.next_action ?? ''} {s.action && <FixButton fix={s.action} run={fixer.run} busy={fixer.busy} />}
        </div>
      ))}
      {pf.host.map((h) => (
        <div key={h.name}>
          <strong>{h.title ?? h.name}: </strong>
          {h.sentence} {h.next_action ?? ''}
        </div>
      ))}
      <div className="btn-group" style={{ marginTop: 6 }}>
        {pf.install && !pf.installing && (
          <button type="button" className="btn primary" disabled={busy} onClick={() => install({ case_id: pf.case_id }, pf.install!.label)} data-testid="preflight-install">
            {pf.install.label}
          </button>
        )}
        {optional && !pf.installing && (
          <button type="button" className="btn" disabled={busy} onClick={() => install({ items: optional.items }, optional.label)}>
            {optional.label.replace("Install what's missing", 'Also install optional tools')}
          </button>
        )}
        <button type="button" className="btn" disabled={busy} onClick={onChanged}>
          Recheck
        </button>
      </div>
      {queue && <QueueProgress queue={queue} onChanged={onChanged} title="Installing what this project needs" />}
      {fixer.dialog}
    </section>
  );
}
