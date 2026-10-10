import { useState } from 'react';
import { aiControl, type Cooldown } from '../../lib/aiControl';
import { dateTime } from '../../lib/format';
import { useApi } from '../../lib/store';
import { useToast } from '../Toasts';

const WHAT: Record<string, string> = { credits_exhausted: 'out of credits', usage_limit: 'usage limit reached' };

/** Badge on a rung whose provider is paused (credits / usage exhausted). */
export function CooldownBadge({ c }: { c: Cooldown | null | undefined }) {
  if (!c) return null;
  return (
    <span className="chip warn" title={c.text ?? c.reason ?? ''} data-testid="cooldown-badge">
      Paused{c.scope === 'free_models' ? ' (free models)' : ''}: {WHAT[c.outcome] ?? c.outcome.replace(/_/g, ' ')}
      {c.until ? ` · until ${dateTime(c.until)}` : ''}
    </span>
  );
}

/** Every paused provider, with a Clear button. Every task skips a paused provider until the cooldown ends or is cleared. */
export function CooldownList({ cooldowns, onCleared }: { cooldowns: Cooldown[]; onCleared: () => void }) {
  const api = useApi();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  if (!cooldowns.length) return null;
  const clear = async (c: Cooldown) => {
    setBusy(c.connection_id);
    try {
      await aiControl(api).clearCooldown(c.connection_id);
      toast.success(`${c.connection_label ?? c.provider ?? 'Provider'} is no longer paused`, 'Every task may use it again.');
      onCleared();
    } catch (e) {
      toast.error('The cooldown was not cleared', e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="callout warn" role="region" aria-label="Paused providers" data-testid="cooldown-list">
      <div className="ttl">Paused providers</div>
      <p className="small">A provider that ran out of credits or hit its usage limit is skipped by every task until the time shown. Clear it once you have topped it up.</p>
      <ul className="list">
        {cooldowns.map((c) => (
          <li key={c.connection_id} className="row">
            <strong>{c.connection_label ?? c.connection_id}</strong>
            <CooldownBadge c={c} />
            <button type="button" className="btn sm" onClick={() => clear(c)} disabled={busy != null}>
              {busy === c.connection_id ? 'Clearing…' : 'Clear cooldown'}
              <span className="sr-only"> for {c.connection_label ?? c.connection_id}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
