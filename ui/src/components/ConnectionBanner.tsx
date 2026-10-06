import { computeStaleness } from '../lib/events';
import { duration, timeAgo } from '../lib/format';
import { useNow, useStore, useStoreSelector } from '../lib/store';

export function MockBanner() {
  const mock = useStoreSelector((s) => s.isMock);
  if (!mock) return null;
  return (
    <div className="banner mock" role="note" data-testid="mock-banner">
      <span aria-hidden="true">◆</span>
      Demo data – mock controller. Nothing shown here comes from a real rebuild.
    </div>
  );
}

export function useStaleness() {
  const now = useNow(1000);
  const snap = useStoreSelector((s) => s.clientSnap);
  const hb = useStoreSelector((s) => s.health?.heartbeat_seconds ?? null);
  const st = computeStaleness({ now, lastEventAt: snap?.lastEventAt ?? null, heartbeatSeconds: hb, status: snap?.status ?? 'idle', connectedAt: snap?.connectedAt });
  return { ...st, now, snap, heartbeatSeconds: hb };
}

export function ConnectionBanner() {
  const store = useStore();
  const healthError = useStoreSelector((s) => s.healthError);
  const { stale, reason, ageMs, now, snap, heartbeatSeconds } = useStaleness();
  const lastSeq = snap?.lastSeq ?? 0;

  if (!snap) {
    if (!healthError) {
      return (
        <div className="banner warn" role="status" data-testid="conn-banner" data-state="connecting">
          <span className="dot warn" aria-hidden="true" /> Connecting to the controller…
        </div>
      );
    }
    return (
      <div className="banner bad" role="alert" data-testid="conn-banner" data-state="disconnected">
        <span className="dot bad" aria-hidden="true" />
        <strong>Disconnected.</strong> The controller is not answering. Affected: all live data and actions. Next: Rebuild Studio retries automatically; restart the app if this persists.
      </div>
    );
  }

  const status = snap.status;
  const retryIn = snap.nextRetryAt ? Math.max(0, Math.ceil((snap.nextRetryAt - now) / 1000)) : null;

  if (status === 'reconnecting' || status === 'disconnected' || status === 'connecting' || status === 'idle') {
    const bad = status === 'disconnected';
    return (
      <div className={`banner ${bad ? 'bad' : 'warn'}`} role={bad ? 'alert' : 'status'} data-testid="conn-banner" data-state={status}>
        <span className={`dot ${bad ? 'bad' : 'warn'}`} aria-hidden="true" />
        <strong>{bad ? 'Disconnected' : status === 'reconnecting' ? 'Reconnecting' : 'Connecting'}</strong>
        <span>
          Last event seq <span className="mono">{lastSeq}</span> · received {timeAgo(snap.lastEventAt, now)}
          {retryIn != null && ` · next attempt in ${retryIn} s (attempt ${snap.reconnectAttempt})`}
        </span>
        <span>Shown state may be out of date; missed events replay automatically after reconnect.</span>
        <span className="spacer" />
        <button type="button" className="btn sm" onClick={() => store.client?.reconnectNow()}>
          Retry now
        </button>
      </div>
    );
  }

  if (stale) {
    return (
      <div className="banner warn" role="status" data-testid="conn-banner" data-state="stale">
        <span className="dot warn" aria-hidden="true" />
        <strong>Connected, but no events</strong>
        <span>
          {reason === 'never_received' ? 'No events received since connecting' : `Nothing received for ${duration(ageMs)}`} (heartbeat expected every {heartbeatSeconds ?? 30} s). Progress shown may be stale. Last seq{' '}
          <span className="mono">{lastSeq}</span>.
        </span>
      </div>
    );
  }

  return (
    <div className="banner ok" role="status" data-testid="conn-banner" data-state="connected">
      <span className="dot ok" aria-hidden="true" /> Connected · last event {timeAgo(snap.lastEventAt, now)} · seq <span className="mono">{lastSeq}</span>
    </div>
  );
}
