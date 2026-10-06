// Thin wrapper over the Tauri shell commands. In a plain browser every function reports `available: false`
// and the UI falls back to text inputs / showing paths.
import { invoke, isTauri } from '@tauri-apps/api/core';

export function hasTauri(): boolean {
  try {
    return isTauri() || typeof window.__TAURI_INTERNALS__ !== 'undefined';
  } catch {
    return false;
  }
}

export async function pickFolder(title: string, defaultPath?: string): Promise<string | null> {
  if (!hasTauri()) return null;
  const r = await invoke<string | null>('pick_folder', { title, defaultPath: defaultPath ?? null });
  return typeof r === 'string' && r ? r : null;
}

export async function openPath(path: string): Promise<boolean> {
  if (!hasTauri()) return false;
  await invoke('open_path', { path });
  return true;
}

/** Launch a preview through the shell (native command or a browser URL). */
export async function launchPreview(args: { previewId: string; url?: string; command?: string[] | string; instanceId?: string }): Promise<boolean> {
  if (!hasTauri()) return false;
  await invoke('launch_preview', {
    previewId: args.previewId,
    url: args.url ?? null,
    command: Array.isArray(args.command) ? args.command : args.command ? [args.command] : null,
    instanceId: args.instanceId ?? null,
  });
  return true;
}
