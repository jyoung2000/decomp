import { useEffect, useId, useState, type ReactNode } from 'react';
import { ConfirmDialog } from './Dialog';
import { ErrorCallout } from './ErrorCallout';
import { useToast } from './Toasts';
import { useApi, useResource, useStore } from '../lib/store';
import type { IsolationInfo, OriginalConsent } from '../lib/types';

export const PERMISSION_RE = /original execution needs your permission|original execution not authorized|runtime observation not authorized/i;

/** Loads the recorded consent and keeps it fresh (own actions and `case.consent` events from the controller). */
export function useConsent(caseId: string) {
  const api = useApi();
  const store = useStore();
  const [tick, setTick] = useState(0);
  useEffect(() => store.onControllerEvent((ev) => ev.kind === 'case.consent' && ev.case_id === caseId && setTick((t) => t + 1)), [store, caseId]);
  const res = useResource(() => api.consent(caseId), [api, caseId], tick);
  return { ...res, reload: () => setTick((t) => t + 1) };
}

/** What the controller says about this computer's protection (GET /isolation). Failing to load is not fatal: the text stays accurate. */
export function useIsolation(): IsolationInfo | null {
  const api = useApi();
  return useResource(() => api.isolation(), [api]).data ?? null;
}

/**
 * Plain-language WHY and WHAT-PROTECTS text for running the original program. The statements about protection come from
 * docs/ISOLATION.md and GET /isolation; nothing here claims more than the controller reports (network is not blocked by default).
 */
export function OriginalRunExplainer({ isolation, id, compact = false }: { isolation: IsolationInfo | null; id?: string; compact?: boolean }) {
  const win = isolation ? isolation.platform.startsWith('win') : true;
  if (compact)
    return (
      <p id={id} data-testid="original-run-summary">
        <strong>Why:</strong> a meaningful comparison needs the original’s real behaviour (what it prints, which files it writes, how it ends); otherwise Rebuild Studio can only guess from reading its code. <strong>What protects you:</strong>{' '}
        {win ? 'it runs as a reduced-rights Windows process' : 'it runs in its own process group'} with time and memory limits and its own work folder. <strong>What does not:</strong> the network is <strong>not blocked</strong> by default, and the program can still read files your account can read.
        {isolation?.network_blocking ? ` (On this computer: ${isolation.network_blocking}.)` : ''} It is damage limitation, not a guarantee against malicious software.
      </p>
    );
  return (
    <div className="stack" id={id} data-testid="original-run-explainer">
      <p>
        <strong>Why:</strong> to tell whether a rebuilt program behaves like the original, Rebuild Studio needs to see what the original really does (what it prints, which files it writes, how it ends). Without that, comparisons can only rely on guesses from reading the program’s code.
      </p>
      <p>
        <strong>What protects you:</strong>
      </p>
      <ul className="bullets small">
        <li>{win ? 'It runs as a reduced-rights Windows process (low integrity), so it cannot write to your documents, desktop or registry.' : 'It runs in its own process group with CPU, memory and file-size limits.'}</li>
        <li>It has time and memory limits, and everything it starts is stopped when the run ends.</li>
        <li>It gets its own work folder; the files it creates go there, and your API keys and other environment secrets are not passed to it.</li>
      </ul>
      <p>
        <strong>What does not protect you:</strong>
      </p>
      <ul className="bullets small">
        <li data-testid="network-truth">
          The network is <strong>not blocked</strong> by default: if the program connects to the internet, it can{isolation?.network_blocking ? ` (${isolation.network_blocking})` : ''}.
        </li>
        <li>It can still read files your account can read.</li>
        <li>This limits damage but is not a guarantee against malicious software. If you do not trust the program, run Rebuild Studio inside a disposable virtual machine.</li>
      </ul>
      {!isolation && <p className="xs muted">Rebuild Studio could not check this computer’s protection level just now; the points above describe its default behaviour.</p>}
    </div>
  );
}

const VIA: Record<string, string> = { create_case: 'when the project was created', api: 'in Rebuild Studio' };

function when(at: string | null): string {
  if (!at) return 'at an unknown time';
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
}

const DISMISS_KEY = (caseId: string) => `rs.consent.keepoff.${caseId}`;

/** Grant / revoke control for “run the original program”. `variant="banner"` is shown only when the plan needs the original. */
export function ConsentCard({ caseId, variant = 'card', onChanged, needed = true, onlyWhenGranted = false }: { caseId: string; variant?: 'card' | 'banner'; onChanged?: () => void; needed?: boolean; onlyWhenGranted?: boolean }) {
  const api = useApi();
  const toast = useToast();
  const consent = useConsent(caseId);
  const isolation = useIsolation();
  const [dialog, setDialog] = useState<'grant' | 'revoke' | null>(null);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(DISMISS_KEY(caseId)) === '1';
    } catch {
      return false;
    }
  });
  const descId = useId();

  const c: OriginalConsent | undefined = consent.data;
  const set = async (allow: boolean) => {
    setBusy(true);
    try {
      await api.setConsent(caseId, allow, allow ? 'Allowed in the workspace' : 'Revoked in the workspace');
      try {
        localStorage.removeItem(DISMISS_KEY(caseId));
      } catch {
        /* storage may be unavailable */
      }
      setDismissed(false);
      toast.success(allow ? 'Running the original is allowed for this project' : 'Running the original is no longer allowed');
      consent.reload();
      onChanged?.();
    } catch (e) {
      toast.error(allow ? 'Could not record the permission' : 'Could not revoke the permission', e);
    } finally {
      setBusy(false);
    }
  };
  const keepOff = () => {
    try {
      localStorage.setItem(DISMISS_KEY(caseId), '1');
    } catch {
      /* storage may be unavailable */
    }
    setDismissed(true);
  };

  if (consent.error && !c) return <ErrorCallout error={consent.error} title="The permission to run the original could not be loaded" onRetry={consent.reload} />;
  if (!c) return <p className="small muted" data-testid="consent-loading">Checking whether running the original is allowed…</p>;

  let body: ReactNode;
  if (c.allowed && variant === 'banner') return null;
  if (!c.allowed && onlyWhenGranted) return null;
  if (c.allowed) {
    const last = [...(c.history ?? [])].reverse().find((h) => h.allowed);
    body = (
      <section className="callout info" aria-label="Permission to run the original" data-testid="consent-granted">
        <div className="ttl">Running the original program is allowed</div>
        <div id={descId}>
          You allowed this {c.via ? VIA[c.via] ?? `(${c.via})` : ''} on {when(c.at)}
          {last?.note ? ` — “${last.note}”` : ''}. Rebuild Studio runs it only inside the limits listed under “What protects you”, and only to record how it behaves.
        </div>
        <div>
          <button type="button" className="btn sm" onClick={() => setDialog('revoke')} disabled={busy} data-testid="consent-revoke">
            Revoke permission
          </button>
        </div>
      </section>
    );
  } else if (variant === 'banner' && (!needed || dismissed)) {
    return null;
  } else {
    body = (
      <section className="callout warn" aria-label="Permission to run the original" data-testid="consent-missing" role="region">
        <div className="ttl">Rebuild Studio has not been allowed to run the original program</div>
        <div id={descId}>
          {needed ? 'The plan needs to see how the original behaves, but ' : ''}it will not run the original until you say so. Without it, behaviour cannot be compared and the rebuilt result stays “not verified”. Nothing is run if you keep this off.
        </div>
        <div className="row">
          <button type="button" className="btn primary sm" onClick={() => setDialog('grant')} disabled={busy} data-testid="consent-grant">
            Allow running the original…
          </button>
          {variant === 'banner' && (
            <button type="button" className="btn sm" onClick={keepOff} aria-describedby={descId} data-testid="consent-keep-off">
              Keep it off
            </button>
          )}
        </div>
      </section>
    );
  }

  return (
    <>
      {body}
      <ConfirmDialog
        open={dialog === 'grant'}
        title="Allow Rebuild Studio to run the original program?"
        body={<OriginalRunExplainer isolation={isolation} />}
        confirmLabel="Allow for this project"
        onConfirm={() => void set(true)}
        onClose={() => setDialog(null)}
      />
      <ConfirmDialog
        open={dialog === 'revoke'}
        title="Stop allowing the original to run?"
        body={<p>Rebuild Studio will not run the original again for this project until you allow it again. Behaviour that was already recorded is kept.</p>}
        confirmLabel="Revoke permission"
        danger
        onConfirm={() => void set(false)}
        onClose={() => setDialog(null)}
      />
    </>
  );
}

/** Grant button for a blocked job whose message is the permission message. */
export function GrantInline({ caseId, onChanged }: { caseId: string; onChanged?: () => void }) {
  const api = useApi();
  const toast = useToast();
  const isolation = useIsolation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="btn sm primary" onClick={() => setOpen(true)} data-testid="blocked-grant">
        Grant permission…
      </button>
      <ConfirmDialog
        open={open}
        title="Allow Rebuild Studio to run the original program?"
        body={<OriginalRunExplainer isolation={isolation} />}
        confirmLabel="Allow for this project"
        onConfirm={() => {
          api.setConsent(caseId, true, 'Allowed from the blocked step').then(
            () => {
              toast.success('Running the original is allowed for this project', 'Press Resume to continue the blocked step.');
              onChanged?.();
            },
            (e) => toast.error('Could not record the permission', e),
          );
        }}
        onClose={() => setOpen(false)}
      />
    </>
  );
}
