import type { ReactNode } from 'react';

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty" role="status">
      <div className="empty-title">{title}</div>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="empty" role="status" aria-live="polite">
      <p className="muted">Loading {what}…</p>
    </div>
  );
}
