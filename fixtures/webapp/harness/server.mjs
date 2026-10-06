import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.json': 'application/json', '.txt': 'text/plain',
};

/** Static server on 127.0.0.1:<ephemeral>. `overrides` maps "/path" -> string body served instead of the file. */
export function startServer(root) {
  const overrides = new Map();
  const hits = [];
  const state = { down: false };
  const server = http.createServer((req, res) => {
    if (state.down) { req.socket.destroy(); return; } // simulate an unreachable network
    const url = new URL(req.url, 'http://x');
    let p = decodeURIComponent(url.pathname);
    if (p.endsWith('/')) p += 'index.html';
    hits.push(p);
    const headers = { 'Cache-Control': 'no-store' };
    if (overrides.has(p)) {
      res.writeHead(200, { ...headers, 'Content-Type': TYPES[path.extname(p)] || 'application/octet-stream' });
      return res.end(overrides.get(p));
    }
    const file = path.join(root, p);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('not found');
    }
    res.writeHead(200, { ...headers, 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({
        base: `http://127.0.0.1:${server.address().port}`,
        overrides, hits, state,
        close: () => new Promise((r) => server.close(r)),
      });
    });
  });
}
