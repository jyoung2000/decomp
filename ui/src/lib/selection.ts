import { useEffect, useState } from 'react';

const KEY = 'rs.selectedCase';
const listeners = new Set<(id: string | null) => void>();

export function getSelectedCase(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setSelectedCase(id: string | null) {
  try {
    if (id) localStorage.setItem(KEY, id);
    else localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  for (const fn of listeners) fn(id);
}

/** Selected project, persisted in localStorage and mirrored by the `/projects/:caseId` URL. */
export function useSelectedCase(): string | null {
  const [id, setId] = useState<string | null>(getSelectedCase);
  useEffect(() => {
    listeners.add(setId);
    return () => {
      listeners.delete(setId);
    };
  }, []);
  return id;
}
