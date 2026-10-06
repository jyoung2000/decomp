import { defineConfig, devices } from '@playwright/test';

// UI tests run against the built UI served by the mock controller (ui/mock/server.mjs).
// Browsers come from PLAYWRIGHT_BROWSERS_PATH (preinstalled); never run `playwright install` here.
const PORT = Number(process.env.E2E_PORT ?? 8790);
export const E2E_TOKEN = 'e2e-token';

export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1920, height: 1080 },
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: `node mock/server.mjs --port ${PORT} --token ${E2E_TOKEN} --heartbeat 2 --serve dist`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: 'pipe',
  },
});
