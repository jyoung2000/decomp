/// <reference types="vitest" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Dev: the UI talks to `/__controller/*`, which Vite proxies to the controller (or the mock controller).
// Production (Tauri): the shell injects window.__REBUILD_STUDIO__ = {baseUrl, token}; no proxy is involved.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.VITE_CONTROLLER_URL || 'http://127.0.0.1:8765';
  return {
    base: './',
    plugins: [react()],
    server: {
      port: Number(env.VITE_PORT || 5173),
      strictPort: false,
      proxy: {
        '/__controller': {
          target,
          ws: true,
          changeOrigin: false,
          rewrite: (p: string) => p.replace(/^\/__controller/, ''),
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
      target: 'es2021',
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test-setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      css: false,
    },
  };
});
