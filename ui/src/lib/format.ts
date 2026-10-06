export function timeAgo(fromMs: number | null | undefined, now: number): string {
  if (fromMs == null || !Number.isFinite(fromMs)) return 'never';
  const s = Math.max(0, Math.round((now - fromMs) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s} s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min ago`;
  return `${Math.floor(h / 24)} days ago`;
}

export function parseTime(t: string | number | null | undefined): number | null {
  if (t == null) return null;
  if (typeof t === 'number') return t < 1e12 ? t * 1000 : t; // controller heartbeat_at is epoch seconds
  const v = Date.parse(t);
  return Number.isNaN(v) ? null : v;
}

export function duration(ms: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${sec} s`;
  return `${sec} s`;
}

export function clockTime(t: string | number | null | undefined): string {
  const v = parseTime(t);
  if (v == null) return '—';
  return new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function dateTime(t: string | number | null | undefined): string {
  const v = parseTime(t);
  if (v == null) return '—';
  return new Date(v).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function usd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return 'unknown';
  return `$${n.toFixed(n < 1 ? 3 : 2)}`;
}

export function bytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function shortHash(h: string | null | undefined, n = 10): string {
  if (!h) return '—';
  const clean = h.replace(/^sha256:/, '');
  return clean.length > n ? clean.slice(0, n) + '…' : clean;
}

export function humanize(key: string): string {
  const s = key.replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function plural(n: number, one: string, many = one + 's'): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error ?? new Error('read failed'));
    r.onload = () => {
      const res = String(r.result ?? '');
      const i = res.indexOf(',');
      resolve(i >= 0 ? res.slice(i + 1) : res);
    };
    r.readAsDataURL(file);
  });
}

/** Human description of a comparison tolerance. Approximate rules are never called exact or pixel-perfect. */
export function describeTolerance(tol: Record<string, unknown> | null | undefined): { exact: boolean; text: string } {
  const entries = Object.entries(tol ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== '' && v !== false);
  if (!entries.length) return { exact: true, text: 'Exact match required (no tolerance declared)' };
  const parts = entries.map(([k, v]) => {
    const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
    if (/^(max|threshold|limit)/i.test(k) || /ratio|percent|pixels|delta|diff|epsilon|ms$/i.test(k)) return `${humanize(k)} ≤ ${val}`;
    return `${humanize(k)}: ${val}`;
  });
  return { exact: false, text: `Not exact — declared tolerance: ${parts.join(', ')}` };
}
