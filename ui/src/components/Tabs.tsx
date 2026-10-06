import { NavLink } from 'react-router-dom';
import type { KeyboardEvent } from 'react';

/** Route-backed tab strip. Arrow keys move between tabs; Enter/Space activates (native link behaviour). */
export function RouteTabs({ tabs, label }: { tabs: { to: string; label: string; badge?: number | string | null; testId?: string }[]; label: string }) {
  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const links = Array.from(e.currentTarget.querySelectorAll<HTMLAnchorElement>('a.tab'));
    const i = links.indexOf(document.activeElement as HTMLAnchorElement);
    if (i < 0) return;
    e.preventDefault();
    const n = e.key === 'Home' ? 0 : e.key === 'End' ? links.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + links.length) % links.length;
    links[n].focus();
  };
  return (
    <nav className="tabs" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <NavLink key={t.to} to={t.to} className="tab" data-testid={t.testId} end>
          {t.label}
          {t.badge != null && t.badge !== 0 && (
            <>
              {' '}
              <span className="badge-count">{t.badge}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

/** Local (non-route) tabs following the ARIA tabs pattern. */
export function LocalTabs<T extends string>({ tabs, value, onChange, label }: { tabs: { id: T; label: string }[]; value: T; onChange: (v: T) => void; label: string }) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = tabs.findIndex((t) => t.id === value);
    let n = i;
    if (e.key === 'ArrowRight') n = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = tabs.length - 1;
    else return;
    e.preventDefault();
    onChange(tabs[n].id);
    const btn = e.currentTarget.querySelectorAll<HTMLButtonElement>('[role=tab]')[n];
    btn?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-controls={`panel-${t.id}`} aria-selected={value === t.id} tabIndex={value === t.id ? 0 : -1} className="tab" onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}
