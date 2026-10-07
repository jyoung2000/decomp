import { defineConfig, devices } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

// Default: UI tests run against the built UI served by the mock controller (ui/mock/server.mjs).
// REAL_CONTROLLER=1: only e2e/real-controller.spec.ts runs, against `vite` dev proxied to a running Python controller
//   (REAL_CONTROLLER_URL, default http://127.0.0.1:8765; token from REAL_CONTROLLER_TOKEN or <REAL_CONTROLLER_DATA>/controller.json).
// Browsers come from PLAYWRIGHT_BROWSERS_PATH (preinstalled); never run `playwright install` here.
const REAL_AI = process.env.REAL_AI === '1';
const REAL = process.env.REAL_CONTROLLER === '1' || REAL_AI;
const PORT = Number(process.env.E2E_PORT ?? 8790);
const REAL_UI_PORT = Number(process.env.REAL_UI_PORT ?? 5199);
export const E2E_TOKEN = 'e2e-token';

// REAL_AI=1: only e2e/ai-real-controller.spec.ts runs, against the controller started by controller/tests/support/live_ui_server.py
//   (REAL_AI_DATA = its --data dir; port and token come from <dir>/controller.json).
function aiInfo(): { port?: number; token?: string } {
  try {
    return JSON.parse(fs.readFileSync(path.join(process.env.REAL_AI_DATA ?? '', 'controller.json'), 'utf8'));
  } catch {
    return {};
  }
}

function realToken(): string {
  if (REAL_AI) return aiInfo().token ?? '';
  if (process.env.REAL_CONTROLLER_TOKEN) return process.env.REAL_CONTROLLER_TOKEN;
  const dir = process.env.REAL_CONTROLLER_DATA;
  if (dir) {
    try {
      return JSON.parse(fs.readFileSync(path.join(dir, 'controller.json'), 'utf8')).token ?? '';
    } catch {
      /* reported by the spec */
    }
  }
  return '';
}

// @playwright/test 1.56.1 resolves chromium-1194 / chromium_headless_shell-1194, which are preinstalled under
// PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers, so no executablePath override is needed.

export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  testMatch: REAL_AI ? ['ai-real-controller.spec.ts'] : REAL ? ['real-controller.spec.ts'] : ['*.spec.ts'],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: REAL ? 20 * 60_000 : 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: REAL ? `http://127.0.0.1:${REAL_UI_PORT}` : `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1920, height: 1080 },
    // PW_CHROMIUM_PATH: use an already-installed Chromium when the bundled revision is not present (never download one here).
    ...(process.env.PW_CHROMIUM_PATH ? { launchOptions: { executablePath: process.env.PW_CHROMIUM_PATH } } : {}),
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: REAL
    ? {
        command: `npx vite --host 127.0.0.1 --port ${REAL_UI_PORT} --strictPort`,
        url: `http://127.0.0.1:${REAL_UI_PORT}/`,
        reuseExistingServer: true,
        timeout: 60_000,
        stdout: 'pipe',
        env: { VITE_CONTROLLER_URL: REAL_AI ? `http://127.0.0.1:${aiInfo().port ?? 8765}` : (process.env.REAL_CONTROLLER_URL ?? 'http://127.0.0.1:8765'), VITE_CONTROLLER_TOKEN: realToken() },
      }
    : {
        command: `node mock/server.mjs --port ${PORT} --token ${E2E_TOKEN} --heartbeat 2 --serve dist`,
        url: `http://127.0.0.1:${PORT}/index.html`,
        reuseExistingServer: false,
        timeout: 30_000,
        stdout: 'pipe',
      },
});
