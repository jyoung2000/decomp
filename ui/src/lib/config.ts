// Resolves how to reach the controller.
// 1. Tauri shell: window.__REBUILD_STUDIO__ = {baseUrl, token} (authoritative).
// 2. Vite dev: baseUrl "/__controller" (proxied) + VITE_CONTROLLER_TOKEN.
// 3. Static serving by the mock controller: same origin + ?token= in the page URL (dev/test only).

export interface StudioConfig {
  baseUrl: string;
  token: string;
  /** true when explicitly flagged as the mock controller by the environment */
  mock: boolean;
  source: 'tauri' | 'vite' | 'url' | 'none';
}

declare global {
  interface Window {
    __REBUILD_STUDIO__?: { baseUrl: string; token: string; mock?: boolean };
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  }
}

export function resolveConfig(win: Window = window): StudioConfig {
  const injected = win.__REBUILD_STUDIO__;
  if (injected && typeof injected.baseUrl === 'string') {
    return { baseUrl: injected.baseUrl.replace(/\/$/, ''), token: injected.token ?? '', mock: !!injected.mock, source: 'tauri' };
  }
  const env = import.meta.env ?? {};
  if (env.DEV && env.VITE_CONTROLLER_TOKEN !== undefined) {
    return { baseUrl: '/__controller', token: env.VITE_CONTROLLER_TOKEN ?? '', mock: env.VITE_MOCK === '1', source: 'vite' };
  }
  let token = '';
  try {
    const params = new URLSearchParams(win.location.search);
    token = params.get('token') ?? '';
    if (token) win.sessionStorage.setItem('rs.token', token);
    else token = win.sessionStorage.getItem('rs.token') ?? '';
  } catch {
    /* storage unavailable */
  }
  if (env.DEV) return { baseUrl: '/__controller', token, mock: env.VITE_MOCK === '1', source: 'vite' };
  return { baseUrl: '', token, mock: false, source: token ? 'url' : 'none' };
}

export function wsUrl(cfg: StudioConfig, since: number, win: Window = window): string {
  const base = new URL(cfg.baseUrl + '/ws', win.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  base.searchParams.set('token', cfg.token);
  base.searchParams.set('since', String(since));
  return base.toString();
}
