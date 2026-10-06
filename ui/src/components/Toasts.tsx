import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { describeError } from '../lib/api';

export interface Toast {
  id: number;
  kind: 'info' | 'success' | 'error';
  title: string;
  message?: string;
}

interface ToastApi {
  push: (t: Omit<Toast, 'id'>) => void;
  error: (title: string, e: unknown) => void;
  success: (title: string, message?: string) => void;
}

const Ctx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const push = useCallback(
    (t: Omit<Toast, 'id'>) => {
      const id = nextId.current++;
      setToasts((list) => [...list.slice(-4), { ...t, id }]);
      setTimeout(() => dismiss(id), t.kind === 'error' ? 12000 : 5000);
    },
    [dismiss],
  );
  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (title, message) => push({ kind: 'success', title, message }),
      error: (title, e) => {
        const d = describeError(e);
        push({ kind: 'error', title, message: [d.what, d.affected && `Affected: ${d.affected}`, d.next && `Next: ${d.next}`].filter(Boolean).join(' ') });
      },
    }),
    [push],
  );
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite" aria-relevant="additions" role="region" aria-label="Notifications">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
            <div className="toast-body">
              <div style={{ fontWeight: 600 }}>{t.title}</div>
              {t.message && <div className="small muted">{t.message}</div>}
            </div>
            <button type="button" className="btn ghost icon sm" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}>
              ✕
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const c = useContext(Ctx);
  if (!c) throw new Error('ToastProvider missing');
  return c;
}
