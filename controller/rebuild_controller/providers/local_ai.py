"""Local AI on this PC: detect local model servers, describe their models in plain language, keep connections in sync.

Only well-known LOOPBACK endpoints are probed (never anything else, never a cloud host):

* Ollama            http://127.0.0.1:11434  ``/api/version``, ``/api/tags``, ``/api/show``
* LM Studio         http://127.0.0.1:1234   ``/v1/models`` (+ ``/api/v0/models`` for type / context / quantization when present)
* llama.cpp server  http://127.0.0.1:8080   ``/v1/models``, ``/props`` (the server's loaded context size)

For every server found a ``provider=local`` connection labelled e.g. "Ollama (this PC)" is created once (found again by its
endpoint, never duplicated) and re-discovered. A server that disappears keeps its connection (state ``unreachable``) so
ladders that use it fall through instead of breaking. Ladders are never rewritten here: ``use_detected`` applies the existing
``all_local`` / ``local_first`` preset only when the user asks (docs/AI_LADDER.md section 8).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from ..ids import now_iso
from .connections import TASKS, ConnectionStore
from .ollama import _caps as _ollama_caps, _context_length
from .ollama_chat import DEFAULT_NUM_CTX_CAP

log = logging.getLogger("rebuild.local_ai")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
PROBE_TIMEOUT_S = 1.5
SHOW_TIMEOUT_S = 6.0
MAX_MODELS = 60
MIN_CODE_CONTEXT = 16384
SMALL_MODEL_B = 4.0


class NotLoopback(ValueError):
    pass


def require_loopback(url: str) -> str:
    """Refuse anything that is not plain http(s) to a loopback address. Detection never talks to another host."""
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (host in LOOPBACK_HOSTS or host.startswith("127.")):
        raise NotLoopback(f"refusing to probe non-loopback address {url!r}")
    return url


@dataclass(frozen=True)
class ServerSpec:
    kind: str          # ollama | lmstudio | llamacpp
    name: str          # "Ollama"
    root: str          # http://127.0.0.1:11434

    @property
    def endpoint(self) -> str:
        return self.root.rstrip("/") + "/v1"

    @property
    def label(self) -> str:
        return f"{self.name} (this PC)"


DEFAULT_SERVERS = (
    ServerSpec("ollama", "Ollama", "http://127.0.0.1:11434"),
    ServerSpec("lmstudio", "LM Studio", "http://127.0.0.1:1234"),
    ServerSpec("llamacpp", "llama.cpp server", "http://127.0.0.1:8080"),
)

INSTALL_PAGES = {"ollama": "https://ollama.com/download", "lmstudio": "https://lmstudio.ai/download",
                 "llamacpp": "https://github.com/ggml-org/llama.cpp/releases"}

TASK_LABEL = {"implementation": "Writing implementations", "repair": "Repairing builds", "naming": "Naming functions and variables", "visual_review": "Reviewing screenshots",
              "verification_assist": "Suggesting test scenarios", "knowledge": "Extracting reusable knowledge"}

_CODER = re.compile(r"(?i)(coder|codestral|devstral|starcoder|codellama|codegemma|deepseek-coder|qwen3-coder|granite-code)")
_SIZE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*([bm])\b")
_EMBED = re.compile(r"(?i)(embed|bge-|e5-|minilm|nomic-bert|rerank)")
_VISION_ONLY = re.compile(r"(?i)(llava|moondream|bakllava|minicpm-v|llama3\.2-vision|-vl\b|vl:|qwen2\.5vl)")
_VISION_NAME = re.compile(r"(?i)(llava|vision|-vl\b|vl:|moondream|bakllava|minicpm-v|qwen2\.5vl|gemma3|gemma4)")


def parameter_billions(*sources: Any) -> float | None:
    for src in sources:
        if src is None:
            continue
        if isinstance(src, (int, float)) and src > 0:
            return float(src) / 1e9 if src > 1e6 else float(src)
        m = _SIZE.search(str(src))
        if m:
            v = float(m.group(1))
            return v / 1000.0 if m.group(2).lower() == "m" else v
    return None


def ollama_models_folder() -> str:
    """Where Ollama keeps pulled models (OLLAMA_MODELS, else %USERPROFILE%\\.ollama\\models). Reported, never changed."""
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        return env
    return str(Path.home() / ".ollama" / "models")


# ====================================================================================== plain-language judgement
def judge(m: Mapping[str, Any], *, num_ctx_cap: int | None = None) -> dict[str, Any]:
    """Per Rebuild Studio task: ``{task: {ok, note}}`` plus ``quick_only``, ``excluded`` and a one-line ``summary``.

    Rules: embedding-only models are excluded; implementation/repair need code ability and >= 16k context; visual review
    needs vision; models under 4B parameters are marked "quick tasks only". Unknown facts are said to be unknown."""
    caps = m.get("capabilities") or {}
    name = str(m.get("id") or "")
    pb = m.get("parameter_b")
    ctx = m.get("context_window")
    eff = min(ctx, num_ctx_cap) if (ctx and num_ctx_cap and m.get("server") == "ollama") else ctx
    embedding_only = caps.get("completion") is False or (caps.get("embedding") is True and not caps.get("completion")) \
        or (caps.get("completion") is None and bool(_EMBED.search(name)) and m.get("type") in (None, "embeddings", "embedding"))
    if m.get("type") in ("embeddings", "embedding"):
        embedding_only = True
    out: dict[str, Any] = {}
    if embedding_only:
        for t in TASKS:
            out[t] = {"ok": False, "note": "embedding model: it turns text into numbers and cannot write answers"}
        return {"tasks": out, "quick_only": False, "excluded": True, "suitable": False,
                "summary": "Not suitable: embedding-only model (cannot write text)."}
    small = pb is not None and pb < SMALL_MODEL_B
    coder = bool(_CODER.search(name))
    vision_focused = bool(_VISION_ONLY.search(name))
    code_ability = "coding model" if coder else ("general model" if (pb is None or pb >= 7) else "small general model")
    vision = caps.get("vision")
    for t in TASKS:
        ok, note = True, ""
        if t in ("implementation", "repair"):
            if small:
                ok, note = False, f"too small for code work ({pb:g}B): quick tasks only"
            elif not coder and pb is not None and pb < 7:
                ok, note = False, f"{pb:g}B general model: weak at code"
            elif vision_focused:
                ok, note = False, "image-description model: weak at code"
            elif eff is not None and eff < MIN_CODE_CONTEXT:
                ok, note = False, f"its {eff // 1024}k context is below the 16k this task needs"
            else:
                note = code_ability + ("" if eff else "; context size unknown")
        elif t == "visual_review":
            if vision is True:
                note = "can read screenshots"
            elif vision is False or (vision is None and not _VISION_NAME.search(name)):
                ok, note = False, "cannot read images"
            else:
                note = "image support not confirmed by the server"
        elif t == "verification_assist":
            if eff is not None and eff < 8192:
                ok, note = False, f"its {max(1, eff // 1024)}k context is too small"
            else:
                note = "quick tasks only" if small else ""
        elif t == "knowledge":
            if eff is not None and eff < 8192:
                ok, note = False, f"its {max(1, eff // 1024)}k context is too small"
            else:
                note = "quick tasks only" if small else ""
        out[t] = {"ok": ok, "note": note}
    good = [TASK_LABEL[t].lower() for t in TASKS if out[t]["ok"]]
    if not good:
        summary = "Not suitable for Rebuild Studio tasks."
    else:
        summary = "Good for " + ", ".join(good) + "." + (" Quick tasks only." if small else "")
    return {"tasks": out, "quick_only": small, "excluded": False, "suitable": bool(good), "summary": summary}


# ====================================================================================== probing
def _get(c: httpx.Client, url: str) -> httpx.Response | None:
    require_loopback(url)
    try:
        return c.get(url)
    except (httpx.HTTPError, OSError):
        return None


def _json(r: httpx.Response | None) -> Any:
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _probe_ollama(c: httpx.Client, spec: ServerSpec, previous: Mapping[str, Any] | None) -> dict[str, Any] | None:
    v = _json(_get(c, f"{spec.root}/api/version"))
    if not isinstance(v, dict) or "version" not in v:
        return None
    tags = _json(_get(c, f"{spec.root}/api/tags"))
    rows = tags.get("models") if isinstance(tags, dict) else None
    rows = [r for r in (rows or []) if isinstance(r, dict) and (r.get("name") or r.get("model"))][:MAX_MODELS]
    signature = json.dumps(sorted((str(r.get("name") or r.get("model")), str(r.get("digest") or r.get("modified_at") or ""))
                                  for r in rows))
    prev_models = {m["id"]: m for m in (previous or {}).get("models") or []} if previous and previous.get("signature") == signature else {}
    models = []
    for r in rows:
        mid = str(r.get("name") or r.get("model"))
        if mid in prev_models:
            models.append(prev_models[mid])
            continue
        d = r.get("details") or {}
        rec: dict[str, Any] = {"id": mid, "server": "ollama", "size_bytes": r.get("size"), "family": d.get("family"),
                               "parameter_size": d.get("parameter_size"), "quantization": d.get("quantization_level"),
                               "format": d.get("format"),
                               "context_window": d.get("context_length") if isinstance(d.get("context_length"), int) else None,
                               "capabilities": {"completion": None, "vision": None, "tools": None, "thinking": None, "source": "unknown"}}
        url = f"{spec.root}/api/show"
        require_loopback(url)
        try:
            s = c.post(url, json={"model": mid}, timeout=SHOW_TIMEOUT_S)
            show = s.json() if s.status_code == 200 else None
        except (httpx.HTTPError, ValueError, OSError):
            show = None
        if isinstance(show, dict):
            rec["capabilities"] = _ollama_caps(show)
            rec["context_window"] = _context_length(show.get("model_info")) or rec["context_window"]
        rec["parameter_b"] = parameter_billions(d.get("parameter_size"), mid)
        models.append(rec)
    return {"version": v.get("version"), "models": models, "signature": signature}


def _probe_openai_like(c: httpx.Client, spec: ServerSpec) -> dict[str, Any] | None:
    data = _json(_get(c, f"{spec.root}/v1/models"))
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    ids = [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")][:MAX_MODELS]
    extra: dict[str, dict[str, Any]] = {}
    version = None
    if spec.kind == "lmstudio":
        v0 = _json(_get(c, f"{spec.root}/api/v0/models"))
        for r in (v0.get("data") if isinstance(v0, dict) else None) or []:
            if isinstance(r, dict) and r.get("id"):
                extra[str(r["id"])] = r
    props: dict[str, Any] = {}
    if spec.kind == "llamacpp":
        p = _json(_get(c, f"{spec.root}/props"))
        if isinstance(p, dict):
            props = p
            version = p.get("build_info")
    models = []
    for mid in ids:
        e = extra.get(mid, {})
        typ = e.get("type")                       # llm | vlm | embeddings
        caps: dict[str, Any] = {"completion": None if not typ else typ != "embeddings",
                                "vision": True if typ == "vlm" else (False if typ in ("llm", "embeddings") else None),
                                "tools": None, "thinking": None, "source": "lmstudio /api/v0/models" if e else "unknown"}
        ctx = e.get("loaded_context_length") or e.get("max_context_length")
        if spec.kind == "llamacpp":
            n_ctx = ((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx"))
            ctx = n_ctx if isinstance(n_ctx, int) else None
            caps["source"] = "llama.cpp /props" if props else "unknown"
            mods = props.get("modalities") if isinstance(props.get("modalities"), dict) else {}
            if "vision" in mods:
                caps["vision"] = bool(mods["vision"])
        models.append({"id": mid, "server": spec.kind, "type": typ, "size_bytes": None, "family": e.get("arch"),
                       "parameter_size": None, "quantization": e.get("quantization"), "format": "gguf" if spec.kind == "llamacpp" else None,
                       "context_window": ctx if isinstance(ctx, int) and ctx > 0 else None, "capabilities": caps,
                       "state": e.get("state"), "parameter_b": parameter_billions(mid)})
    return {"version": version, "models": models, "signature": json.dumps(sorted(ids))}


# ====================================================================================== service
class LocalAI:
    """Detection + connection sync. Thread-safe; detection results are cached (``snapshot``)."""

    def __init__(self, store: ConnectionStore | None, events: Any = None, *, servers: Iterable[ServerSpec] | None = None,
                 transport: httpx.BaseTransport | None = None, state_path: Path | None = None, timeout_s: float = PROBE_TIMEOUT_S):
        self.store = store
        self.events = events
        self.servers = tuple(servers or DEFAULT_SERVERS)
        for s in self.servers:
            require_loopback(s.root)
        self.transport = transport
        self.timeout_s = timeout_s
        self.state_path = state_path
        self._lock = threading.RLock()
        self._last: dict[str, Any] | None = None
        self._last_at = 0.0
        self._running = False

    # ---------------------------------------------------------------- settings (data_dir/local_ai.json)
    def settings(self) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        if self.state_path and self.state_path.is_file():
            try:
                doc = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
        doc.setdefault("num_ctx_cap", DEFAULT_NUM_CTX_CAP)
        return doc

    def save_settings(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            doc = self.settings()
            doc.update(values)
            if self.state_path:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
                os.replace(tmp, self.state_path)
            return doc

    # ---------------------------------------------------------------- detection
    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=httpx.Timeout(self.timeout_s, connect=min(1.0, self.timeout_s)),
                            follow_redirects=False, trust_env=False)

    def _find_connection(self, spec: ServerSpec) -> dict[str, Any] | None:
        if self.store is None:
            return None
        want = urlsplit(spec.endpoint)
        for c in self.store.list():
            if c["provider"] != "local":
                continue
            u = urlsplit(c.get("endpoint") or "")
            same_host = (u.hostname or "").lower() in LOOPBACK_HOSTS or (u.hostname or "").startswith("127.")
            if same_host and u.port == want.port:
                return c
        return None

    def detect(self, *, sync_connections: bool = True) -> dict[str, Any]:
        with self._lock:
            prev = {s["kind"]: s for s in (self._last or {}).get("servers", [])}
        servers: list[dict[str, Any]] = []
        cap = int(self.settings().get("num_ctx_cap") or DEFAULT_NUM_CTX_CAP)

        def probe(spec: ServerSpec) -> tuple[dict[str, Any] | None, int]:
            t0 = time.monotonic()
            try:
                with self._client() as c:
                    info = _probe_ollama(c, spec, prev.get(spec.kind)) if spec.kind == "ollama" else _probe_openai_like(c, spec)
            except NotLoopback:
                raise
            except Exception as e:  # noqa: BLE001 - one odd server never fails detection
                log.info("local server probe %s failed: %s", spec.kind, type(e).__name__)
                info = None
            return info, int((time.monotonic() - t0) * 1000)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(self.servers)), thread_name_prefix="local-ai-probe") as pool:
            probed = list(pool.map(probe, self.servers))       # in parallel: a closed port costs ~1 s on Windows
        for spec, (info, ms) in zip(self.servers, probed):
            row: dict[str, Any] = {"kind": spec.kind, "name": spec.name, "label": spec.label, "endpoint": spec.endpoint,
                                   "found": info is not None, "version": (info or {}).get("version"), "probe_ms": ms,
                                   "install_page": INSTALL_PAGES.get(spec.kind), "models": [], "connection_id": None,
                                   "connection_state": None, "signature": (info or {}).get("signature")}
            if info is not None:
                for m in info["models"]:
                    m.update(judge({**m, "server": spec.kind}, num_ctx_cap=cap))
                    if spec.kind == "ollama" and m.get("context_window"):
                        m["effective_context"] = min(int(m["context_window"]), cap)
                row["models"] = info["models"]
            if spec.kind == "ollama":
                row["models_folder"] = ollama_models_folder()
            if sync_connections and self.store is not None:
                self._sync(spec, row)
            servers.append(row)
        result = {"detected_at": now_iso(), "servers": servers, "num_ctx_cap": cap}
        result.update(self._ladder_advice(servers))
        with self._lock:
            self._last, self._last_at = result, time.monotonic()
        changed = {s["kind"]: s["found"] for s in servers} != {k: v.get("found") for k, v in prev.items()} if prev else True
        self._emit("ai.local.detected", {"found": [s["kind"] for s in servers if s["found"]],
                                         "models": sum(len(s["models"]) for s in servers), "changed": changed})
        return result

    def _sync(self, spec: ServerSpec, row: dict[str, Any]) -> None:
        conn = self._find_connection(spec)
        store = self.store
        assert store is not None
        if not row["found"]:
            if conn is not None:
                store.set_state(conn["connection_id"], "unreachable", detail=f"{spec.name} is not running on this PC")
                row["connection_id"], row["connection_state"] = conn["connection_id"], "unreachable"
            return
        if conn is None:
            conn = store.create("local", spec.label, endpoint=spec.endpoint, auth_mode="local")
        lim = {"detected": spec.kind, "detected_at": now_iso()}
        if spec.kind == "ollama":
            lim["server"] = "ollama"
            lim["num_ctx_cap"] = int(self.settings().get("num_ctx_cap") or DEFAULT_NUM_CTX_CAP)
            if row.get("version"):
                lim["server_version"] = row["version"]
        store.set_limits(conn["connection_id"], **lim)
        try:
            p = store.probe(conn["connection_id"], capabilities=False)
        except Exception as e:  # noqa: BLE001
            log.info("re-discovery of %s failed: %s", spec.kind, type(e).__name__)
            p = store.get(conn["connection_id"])
        self._merge_facts(conn["connection_id"], row)
        row["connection_id"], row["connection_state"] = conn["connection_id"], p.get("state")

    def _merge_facts(self, connection_id: str, row: dict[str, Any]) -> None:
        """Copy detection facts (LM Studio / llama.cpp context, capabilities) onto the connection's model entries."""
        store = self.store
        assert store is not None
        from ..store.db import loads
        r = store.db.query_one("SELECT models FROM connections WHERE connection_id=?", (connection_id,))
        if r is None:
            return
        models = loads(r["models"], [])
        facts = {m["id"]: m for m in row["models"]}
        changed = False
        for m in models:
            f = facts.get(m.get("id"))
            if not f:
                continue
            if not m.get("context_window") and f.get("context_window"):
                m["context_window"] = f["context_window"]
                changed = True
            caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
            fc = f.get("capabilities") or {}
            if any(caps.get(k) is None and fc.get(k) is not None for k in ("completion", "vision", "tools", "thinking")):
                m["capabilities"] = {**fc, **{k: v for k, v in caps.items() if v is not None}}
                changed = True
            meta = m.setdefault("meta", {})
            local = {"server": f.get("server"), "size_bytes": f.get("size_bytes"), "parameter_size": f.get("parameter_size"),
                     "quantization": f.get("quantization"), "summary": f.get("summary"), "quick_only": f.get("quick_only")}
            if meta.get("local") != local:
                meta["local"] = local
                changed = True
        if changed:
            store.db.update("connections", "connection_id", connection_id, {"models": models, "updated_at": now_iso()})

    # ---------------------------------------------------------------- ladder advice (never applied silently)
    def ladder_state(self) -> dict[str, Any]:
        if self.store is None:
            return {"state": "unavailable", "tasks": {}}
        tasks = {}
        for t in TASKS:
            ents = self.store.route_entries(t)
            tasks[t] = {"entries": len(ents), "rationale": self.store.rationale(t) if ents else None}
        if not any(v["entries"] for v in tasks.values()):
            state = "empty"
        elif all((v["rationale"] or "").startswith("preset:") or not v["entries"] for v in tasks.values()):
            state = "preset"
        else:
            state = "user"
        return {"state": state, "tasks": tasks}

    def _ladder_advice(self, servers: list[dict[str, Any]]) -> dict[str, Any]:
        suitable = sum(1 for s in servers if s["found"] for m in s["models"] if m.get("suitable"))
        lad = self.ladder_state()
        has_cloud = False
        if self.store is not None:
            from .connections import locality_of
            has_cloud = any(locality_of(c) == "cloud" and c["auth_mode"] != "subscription_handoff" for c in self.store.list())
        if lad["state"] == "empty":
            advice = "No AI ladder is set yet: use the detected local models (runs on this PC, free)." if suitable else None
        elif lad["state"] == "preset":
            advice = "Your ladder came from a preset; you can refresh it with the detected local models." if suitable else None
        else:
            advice = ("You edited your ladder yourself; it is left alone. 'Use detected local models' replaces it only if you "
                      "confirm.") if suitable else None
        return {"suitable_models": suitable, "ladder": lad, "recommend_use": bool(suitable) and lad["state"] in ("empty", "preset"),
                "recommended_preset": "local_first" if has_cloud else "all_local", "advice": advice}

    def snapshot(self, *, max_age_s: float | None = None) -> dict[str, Any]:
        """Last detection result; re-detects when there is none or it is older than ``max_age_s``."""
        with self._lock:
            last, age = self._last, time.monotonic() - self._last_at
        if last is None or (max_age_s is not None and age > max_age_s):
            return self.detect()
        out = dict(last)
        out.update(self._ladder_advice(out["servers"]))
        out["age_s"] = round(age, 1)
        return out

    def detect_async(self) -> threading.Thread:
        def run() -> None:
            try:
                self.detect()
            except Exception:  # noqa: BLE001
                log.exception("startup local AI detection failed")
        th = threading.Thread(target=run, name="local-ai-detect", daemon=True)
        th.start()
        return th

    def use_detected(self, *, apply: bool, preset_name: str | None = None) -> dict[str, Any]:
        from .ladder import preset
        if self.store is None:
            raise ValueError("connections service unavailable")
        name = preset_name or self.snapshot().get("recommended_preset") or "all_local"
        if name not in ("all_local", "local_first"):
            raise ValueError("preset must be all_local or local_first")
        before = self.ladder_state()
        r = preset(self.store, name, apply=apply)
        r["replaces_user_ladder"] = before["state"] == "user"
        return r

    def set_num_ctx_cap(self, cap: int) -> dict[str, Any]:
        if not isinstance(cap, int) or cap < 2048 or cap > 1_048_576:
            raise ValueError("max context must be between 2048 and 1048576 tokens")
        self.save_settings(num_ctx_cap=cap)
        if self.store is not None:
            for c in self.store.list():
                if c["provider"] == "local" and (c.get("limits") or {}).get("server") == "ollama":
                    self.store.set_limits(c["connection_id"], num_ctx_cap=cap)
        with self._lock:
            self._last = None
        return self.settings()

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, payload)
        except Exception:  # noqa: BLE001
            pass
