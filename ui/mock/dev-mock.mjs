#!/usr/bin/env node
// `npm run dev:mock`: starts the mock controller and Vite (proxying /__controller to it) together.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const port = process.env.MOCK_PORT ?? '8799';
const token = process.env.MOCK_TOKEN ?? 'mock-token';
const children = [];
const run = (cmd, args, env = {}) => {
  const c = spawn(cmd, args, { cwd: root, stdio: 'inherit', env: { ...process.env, ...env }, shell: process.platform === 'win32' });
  children.push(c);
  c.on('exit', (code) => {
    for (const o of children) if (o !== c) o.kill();
    process.exit(code ?? 0);
  });
  return c;
};
run(process.execPath, [path.join(here, 'server.mjs'), '--port', port, '--token', token, '--heartbeat', process.env.MOCK_HEARTBEAT ?? '5', '--auto', process.env.MOCK_AUTO_MS ?? '4000']);
run(process.execPath, [path.join(root, 'node_modules', 'vite', 'bin', 'vite.js')], {
  VITE_CONTROLLER_URL: `http://127.0.0.1:${port}`,
  VITE_CONTROLLER_TOKEN: token,
  VITE_MOCK: '1',
});
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => children.forEach((c) => c.kill(sig)));
