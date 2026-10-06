"""Regenerates tests/data/managed/js/** (tiny JS/Electron samples). Run manually:

    /opt/rebuild-tools/venv/bin/python make_js_samples.py

Uses @electron/asar 4.3.1 (pinned in /opt/rebuild-tools/asar/package.json) to pack app.asar. The committed outputs are
what the tests read; the tests never need node.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "js"
ASAR = Path("/opt/rebuild-tools/asar/node_modules/.bin/asar")

APP_SRC = """\
export function greet(name) {
  return `hello ${name}`;
}
export const VERSION = "0.1.0";
"""
UTIL_SRC = """\
export function add(a, b) {
  return a + b;
}
"""


def smap(file: str, sources: dict[str, str | None], extra_sources: list[str] | None = None) -> str:
    names = list(sources) + (extra_sources or [])
    return json.dumps({
        "version": 3, "file": file, "sources": names,
        "sourcesContent": [sources.get(n) for n in names],
        "names": [], "mappings": "AAAA",
    }, indent=1)


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def webpack_bundle(map_name: str | None) -> str:
    s = ("/******/ (() => { // webpackBootstrap\n/******/ \tvar __webpack_modules__ = ({\n"
         '/***/ "./src/app.js": ((module) => { module.exports = { greet: (n) => `hello ${n}` }; }),\n'
         "/******/ \t});\n/******/ \tfunction __webpack_require__(id) { return __webpack_modules__[id]; }\n"
         '/******/ \t(self["webpackChunktiny"] = self["webpackChunktiny"] || []);\n'
         "/******/ })();\n")
    return s + (f"//# sourceMappingURL={map_name}\n" if map_name else "")


def vite_bundle(map_url: str | None) -> str:
    s = ('const e=import.meta.env;import"./modulepreload-polyfill.js";'
         'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/index-3f9a1c.css"])))=>i.map(i=>d[i]);'
         "console.log(e.MODE);\n")
    return s + (f"//# sourceMappingURL={map_url}\n" if map_url else "")


def esbuild_bundle() -> str:
    return ('"use strict";\nvar __defProp = Object.defineProperty;\nvar __commonJS = (cb, mod) => function __require() {\n'
            "  return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;\n};\n"
            "// node_modules/left-pad/index.js\nvar require_left_pad = __commonJS({\n"
            '  "node_modules/left-pad/index.js"(exports2, module2) { module2.exports = (s) => " " + s; }\n});\n'
            "// src/main.js\nvar leftPad = require_left_pad();\n")


def build_app_tree(root: Path) -> None:
    write(root / "package.json", json.dumps({
        "name": "tiny-electron", "productName": "Tiny Electron", "version": "0.1.0", "main": "main.js",
        "dependencies": {}, "devDependencies": {"electron": "31.0.0", "webpack": "5.90.0"}}, indent=2))
    write(root / "main.js", "const { app, BrowserWindow } = require('electron');\n"
          "app.whenReady().then(() => { const w = new BrowserWindow({}); w.loadFile('renderer/index.html'); });\n")
    write(root / "preload.js", "// preload\n")
    write(root / "renderer/index.html", '<!doctype html><html><head><link rel="manifest" href="manifest.webmanifest">'
          '</head><body><script src="bundle.js"></script>'
          "<script>navigator.serviceWorker.register('sw.js');</script></body></html>\n")
    write(root / "renderer/bundle.js", webpack_bundle("bundle.js.map"))
    write(root / "renderer/bundle.js.map", smap("bundle.js", {
        "webpack://tiny/./src/app.js": APP_SRC, "webpack://tiny/./src/util.js": UTIL_SRC,
        "webpack://tiny/../../outside/escape.js": "// hostile path in source map; must be sanitised\n",
        "webpack://tiny/./node_modules/dep/index.js": None}))
    write(root / "renderer/sw.js", "self.addEventListener('install', (e) => { self.skipWaiting(); });\n"
          "self.addEventListener('fetch', (e) => {});\n")
    write(root / "renderer/manifest.webmanifest", json.dumps({
        "name": "Tiny Electron", "short_name": "Tiny", "start_url": "./index.html", "display": "standalone",
        "icons": [{"src": "icon.png", "sizes": "192x192"}]}, indent=1))
    write(root / "assets/index-3f9a1c.js", vite_bundle("index-3f9a1c.js.map"))
    write(root / "assets/index-3f9a1c.js.map", smap("index-3f9a1c.js", {"../../src/main.ts": "export const main = 1;\n"}))
    write(root / "vendor/esbuild-out.js", esbuild_bundle())
    (root / "native").mkdir(exist_ok=True)
    (root / "native/addon.node").write_bytes(b"\x7fELF-fake-native-addon")


def build_electron_app() -> None:
    work = HERE / "_tmp_app_src"
    if work.exists():
        shutil.rmtree(work)
    build_app_tree(work)
    dest = OUT / "electron_app"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "resources").mkdir(parents=True)
    subprocess.run([str(ASAR), "pack", str(work), str(dest / "resources/app.asar"), "--unpack", "*.node"], check=True)
    for marker in ("resources.pak", "chrome_100_percent.pak", "tiny-electron.exe"):
        (dest / marker).write_bytes(b"")
    shutil.rmtree(work)


def build_web_app() -> None:
    root = OUT / "web_app"
    if root.exists():
        shutil.rmtree(root)
    write(root / "index.html", '<!doctype html><html><head><link rel="manifest" href="manifest.webmanifest">'
          '</head><body><script type="module" src="/assets/index-3f9a1c.js"></script>'
          "<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js')</script></body></html>\n")
    write(root / "package.json", json.dumps({"name": "tiny-web", "version": "1.0.0", "devDependencies": {"vite": "5.0.0"}}))
    write(root / "assets/index-3f9a1c.js", vite_bundle("index-3f9a1c.js.map"))
    write(root / "assets/index-3f9a1c.js.map", smap("index-3f9a1c.js", {"../../src/main.ts": "export const main = 1;\n",
                                                                     "../../src/app.ts": "export const app = 2;\n"}))
    inline = smap("inline.js", {"inline-src.js": "export const inline = true;\n"})
    b64 = base64.b64encode(inline.encode()).decode()
    write(root / "assets/inline.js", f'console.log("inline");\n//# sourceMappingURL=data:application/json;base64,{b64}\n')
    write(root / "assets/no-map.js", "console.log('no map');\n//# sourceMappingURL=missing.js.map\n")
    write(root / "sw.js", "self.addEventListener('install', () => self.skipWaiting());\n")
    write(root / "manifest.webmanifest", json.dumps({"name": "Tiny Web", "start_url": "/", "display": "browser"}))
    write(root / "assets/index-3f9a1c.css", "body{margin:0}\n")


if __name__ == "__main__":
    if not ASAR.exists():
        sys.exit(f"asar CLI not found at {ASAR}")
    build_electron_app()
    build_web_app()
    print("ok")
