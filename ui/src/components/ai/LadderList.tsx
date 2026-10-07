import { useEffect, useRef, type KeyboardEvent } from 'react';
import { rungKey } from '../../lib/ai';
import type { LadderEntry } from '../../lib/types';
import { Availability, Capabilities, LocalityBadge, PriceLabel } from './Badges';

/**
 * One task's ladder: position 1 is the primary, 2.. are fallbacks tried in order.
 * Keyboard: Tab to a row, Alt+Up / Alt+Down moves it, the Move and Remove buttons work with Enter or Space.
 */
export function LadderList({ id, label, entries, onChange, readOnlyReason }: { id: string; label: string; entries: LadderEntry[]; onChange: (e: LadderEntry[]) => void; readOnlyReason?: string }) {
  const focusKey = useRef<string | null>(null);
  // Runs on the render that applied the move/remove (keyed on `entries`), not on an earlier render where the ref was
  // already set but the parent's state had not changed yet (that cleared the key too early on slower CI runners).
  useEffect(() => {
    if (!focusKey.current) return;
    const el = document.getElementById(`${id}-rung-${focusKey.current}`);
    if (el) {
      el.focus();
      focusKey.current = null;
    }
  }, [entries, id]);
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= entries.length) return;
    const n = [...entries];
    [n[i], n[j]] = [n[j], n[i]];
    focusKey.current = String(j);
    onChange(n);
  };
  const remove = (i: number) => {
    const n = entries.filter((_, k) => k !== i);
    focusKey.current = n.length ? String(Math.min(i, n.length - 1)) : null;
    onChange(n);
    if (!n.length) requestAnimationFrame(() => document.getElementById(`${id}-empty`)?.focus());
  };
  const onKey = (e: KeyboardEvent<HTMLLIElement>, i: number) => {
    if (e.target !== e.currentTarget || !e.altKey) return;
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      move(i, -1);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      move(i, 1);
    }
  };
  if (!entries.length)
    return (
      <p className="small muted" id={`${id}-empty`} tabIndex={-1} data-testid={`${id}-empty`}>
        No model for this task. {readOnlyReason ?? 'Work that needs it will be skipped or marked “needs AI” in the plan.'}
      </p>
    );
  return (
    <ol className="ladder" aria-label={label} data-testid={`${id}-ladder`}>
      {entries.map((e, i) => {
        const n = i + 1;
        const upWhy = `${id}-up-why`;
        return (
          <li key={rungKey(e)} id={`${id}-rung-${i}`} tabIndex={0} className="rung" onKeyDown={(ev) => onKey(ev, i)} aria-label={`${n === 1 ? 'Primary' : `Fallback ${n - 1}`}: ${e.model}, ${e.locality === 'local' ? 'local' : e.locality === 'cloud' ? 'cloud' : 'unknown location'}. Alt plus arrow keys move it.`} data-testid={`${id}-rung-${i}`}>
            <span className="rung-pos" aria-hidden="true">
              {n}
            </span>
            <div className="rung-main">
              <div className="row">
                <strong className="mono wrap-any">{e.model}</strong>
                <LocalityBadge locality={e.locality} full />
                <span className="small muted">{e.connection_label ?? e.provider ?? e.connection_id}</span>
                {n === 1 ? <span className="chip outline">Primary</span> : <span className="chip queue">Fallback {n - 1}</span>}
              </div>
              <div className="row small">
                <Availability a={e.availability} />
                <Capabilities entry={e} />
                <span aria-hidden="true">·</span>
                <PriceLabel entry={e} />
              </div>
            </div>
            <div className="btn-group">
              <button type="button" className="btn sm icon" aria-label={`Move ${e.model} up`} disabled={i === 0} aria-describedby={i === 0 ? `${upWhy}-first` : undefined} onClick={() => move(i, -1)}>
                ↑
              </button>
              <button type="button" className="btn sm icon" aria-label={`Move ${e.model} down`} disabled={i === entries.length - 1} aria-describedby={i === entries.length - 1 ? `${upWhy}-last` : undefined} onClick={() => move(i, 1)}>
                ↓
              </button>
              <button type="button" className="btn sm" aria-label={`Remove ${e.model}`} onClick={() => remove(i)}>
                Remove
              </button>
            </div>
          </li>
        );
      })}
      <li className="sr-only" aria-hidden="false">
        <span id={`${id}-up-why-first`}>Already first in the ladder.</span>
        <span id={`${id}-up-why-last`}>Already last in the ladder.</span>
      </li>
    </ol>
  );
}
