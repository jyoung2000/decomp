"""Find, download and register local AI models (GGUF files from the Hugging Face Hub; Ollama library pulls by name).

Network rules (enforced in ``_check_hf_url`` / ``require_loopback``):

* search and file listings use the official Hugging Face Hub API on ``huggingface.co`` only
  (``/api/models?search=&filter=gguf``, ``/api/models/<repo>``, ``/api/models/<repo>/tree/<rev>``);
* downloads use ``https://huggingface.co/<repo>/resolve/<commit>/<file>`` and follow redirects only to Hugging Face's own CDN
  hosts (``*.huggingface.co``, ``*.hf.co``), https only; an optional user-provided HF token is sent to ``huggingface.co``
  itself and never to a redirect host, and is never logged or returned;
* everything else (Ollama ``/api/blobs``, ``/api/create``, ``/api/pull``, ``/api/delete``) goes to a loopback server.

Downloads reuse the tool-setup patterns (``tool_setup.ToolSetupError`` error shape, chunked streaming with progress events, cancel
via an Event, verify-before-activate): the file is streamed to ``<name>.part`` (resumable with ``Range`` after an interruption),
its SHA-256 is checked against the LFS ``oid`` the Hub reports, and only then renamed into place. A wrong hash deletes the file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .ids import now_iso
from .providers.local_ai import INSTALL_PAGES, LOOPBACK_HOSTS, LocalAI, ollama_models_folder, require_loopback
from .tool_setup import CHUNK, ToolSetupError

log = logging.getLogger("rebuild.local_models")

HF_BASE = "https://huggingface.co"
HF_HOST_SUFFIXES = (".huggingface.co", ".hf.co")
HF_HOSTS = {"huggingface.co", "hf.co"}
PERMISSIVE = {"apache-2.0", "mit", "bsd", "bsd-2-clause", "bsd-3-clause", "bsd-3-clause-clear", "cc0-1.0", "cc-by-4.0", "unlicense",
              "isc", "zlib", "bsl-1.0", "mpl-2.0", "artistic-2.0", "afl-3.0", "ecl-2.0", "postgresql", "ncsa", "pddl", "odc-by"}
_QUANT = re.compile(r"(?i)(?<![A-Za-z0-9])(IQ[1-4]_(?:XXS|XS|S|M|NL)|Q[2-8]_K(?:_[SML]|_XL)?|Q[4-8]_[01]|Q4_0_\d_\d|"
                    r"TQ[12]_0|MXFP4|BF16|F16|FP16|F32)(?![A-Za-z0-9])")
_SPLIT = re.compile(r"(?i)-\d{5}-of-\d{5}\.gguf$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}$")
_OLLAMA_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,79}(/[a-zA-Z0-9][a-zA-Z0-9._\-]{0,79})?(:[a-zA-Z0-9][a-zA-Z0-9._\-]{0,79})?$")
FREE_SPACE_MARGIN = 64 * 1024 * 1024


def default_models_dir(data_dir: Path) -> Path:
    """%LOCALAPPDATA%\\RebuildStudio\\models on Windows (the data dir's ``models`` folder elsewhere)."""
    return Path(data_dir) / "models"


def license_of(tags: list[str] | None, card: dict[str, Any] | None = None) -> str | None:
    lic = (card or {}).get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if isinstance(lic, str) and lic:
        return lic.lower()
    for t in tags or []:
        if isinstance(t, str) and t.startswith("license:"):
            return t.split(":", 1)[1].lower()
    return None


def quant_of(filename: str) -> str | None:
    m = _QUANT.search(Path(filename).name)
    return m.group(1).upper() if m else None


def ram_hint_gb(size_bytes: int | None) -> float | None:
    """Rough memory needed to run it: weights + ~15% + about 0.6 GB for an 8k context."""
    if not size_bytes:
        return None
    return round(size_bytes * 1.15 / 1e9 + 0.6, 1)


def ollama_name_for(repo: str, filename: str) -> str:
    base = repo.split("/", 1)[-1]
    base = re.sub(r"(?i)[-_.]?gguf$", "", base)
    q = (quant_of(filename) or "").lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", f"rs-{base}-{q}".lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80].rstrip("-.")


@dataclass
class _Job:
    job_id: str
    kind: str                       # download | pull
    title: str
    cancel: threading.Event = field(default_factory=threading.Event)
    phase: str = "queued"
    bytes_done: int = 0
    bytes_total: int = 0
    resumed_from: int = 0
    speed_bps: float | None = None
    eta_s: float | None = None
    message: str = ""
    error: dict[str, Any] | None = None
    finished: bool = False
    started_at: str = field(default_factory=now_iso)
    result: dict[str, Any] | None = None
    repo: str | None = None
    file: str | None = None
    dest: str | None = None
    last_emit: float = 0.0
    _samples: list[tuple[float, int]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        pct = round(100 * self.bytes_done / self.bytes_total, 1) if self.bytes_total else None
        return {"job_id": self.job_id, "kind": self.kind, "title": self.title, "phase": self.phase, "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total, "percent": pct, "resumed_from": self.resumed_from, "speed_bps": self.speed_bps,
                "eta_s": self.eta_s, "message": self.message, "error": self.error, "finished": self.finished,
                "cancelled": self.phase == "cancelled", "started_at": self.started_at, "result": self.result, "repo": self.repo,
                "file": self.file, "dest": self.dest}


class _Cancelled(Exception):
    pass


class ModelLibrary:
    def __init__(self, local_ai: LocalAI, *, data_dir: Path, events: Any = None, secrets: Any = None, hf_base: str | None = None,
                 allow_insecure_loopback: bool = False, transport: httpx.BaseTransport | None = None, timeout_s: float = 30.0):
        self.local_ai = local_ai
        self.data_dir = Path(data_dir)
        self.events = events
        self.secrets = secrets
        self.hf_base = (hf_base or os.environ.get("REBUILD_HF_BASE") or HF_BASE).rstrip("/")
        self.allow_insecure_loopback = allow_insecure_loopback
        self.transport = transport
        self.timeout_s = timeout_s
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._check_hf_url(self.hf_base + "/api/models")

    # ------------------------------------------------------------------ url policy
    def _check_hf_url(self, url: str) -> str:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme == "https" and (host in HF_HOSTS or host.endswith(HF_HOST_SUFFIXES)):
            return url
        if self.allow_insecure_loopback and u.scheme in ("http", "https") and (host in LOOPBACK_HOSTS or host.startswith("127.")):
            return url
        raise ToolSetupError("host_not_allowed", f"Refusing to contact {host or url}: models are downloaded from huggingface.co only.",
                             affected=url, status=400, next_action="Pick a model from the search results.")

    def _client(self, *, read_timeout: float | None = None) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=httpx.Timeout(self.timeout_s, read=read_timeout or self.timeout_s),
                            follow_redirects=False, trust_env=(urlsplit(self.hf_base).hostname or "") not in LOOPBACK_HOSTS,
                            headers={"User-Agent": "RebuildStudio-Models/1"})

    def _token(self) -> str | None:
        ref = self.local_ai.settings().get("hf_token_ref")
        if not ref or self.secrets is None:
            return None
        try:
            return self.secrets.get(ref)
        except Exception:  # noqa: BLE001
            return None

    def _auth_for(self, url: str) -> dict[str, str]:
        tok = self._token()
        base_host = urlsplit(self.hf_base).hostname
        if tok and urlsplit(url).hostname == base_host:
            return {"authorization": f"Bearer {tok}"}
        return {}

    def _hf_json(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        url = self._check_hf_url(f"{self.hf_base}{path}")
        try:
            with self._client() as c:
                r = c.get(url, params=params, headers=self._auth_for(url))
        except httpx.HTTPError as e:
            raise ToolSetupError("offline", f"Could not reach {urlsplit(self.hf_base).hostname} ({type(e).__name__}).", retryable=True,
                                 status=502, next_action="Check your internet connection and try again.") from e
        if r.status_code in (401, 403):
            raise ToolSetupError("gated", "Hugging Face refused access to this model (it is gated or private).", status=403,
                                 next_action="Open the model page on huggingface.co, accept its terms, then add a Hugging Face token "
                                             "under 'Hugging Face token' and try again.")
        if r.status_code == 404:
            raise ToolSetupError("not_found", "That model or file does not exist on Hugging Face.", status=404,
                                 next_action="Search again and pick a listed model.")
        if r.status_code != 200:
            raise ToolSetupError("hub_error", f"Hugging Face answered HTTP {r.status_code}.", retryable=r.status_code >= 500 or r.status_code == 429,
                                 status=502, next_action="Try again in a minute.")
        try:
            return r.json()
        except ValueError as e:
            raise ToolSetupError("hub_error", "Hugging Face sent an unexpected answer.", status=502, retryable=True) from e

    # ------------------------------------------------------------------ config
    def models_dir(self) -> Path:
        d = self.local_ai.settings().get("models_dir")
        return Path(d) if d else default_models_dir(self.data_dir)

    def config(self) -> dict[str, Any]:
        st = self.local_ai.settings()
        return {"models_dir": str(self.models_dir()), "default_models_dir": str(default_models_dir(self.data_dir)),
                "hf_host": urlsplit(self.hf_base).hostname, "has_hf_token": bool(st.get("hf_token_ref")),
                "ollama_models_folder": ollama_models_folder(), "num_ctx_cap": st.get("num_ctx_cap"),
                "install_pages": INSTALL_PAGES}

    def check_folder(self, path: str | Path, need_bytes: int = 0) -> dict[str, Any]:
        raw = str(path).strip().strip('"')
        if not raw:
            raise ToolSetupError("bad_folder", "Choose a folder for downloaded models.", status=400)
        p = Path(os.path.expandvars(raw)).expanduser()
        if not p.is_absolute():
            raise ToolSetupError("bad_folder", f"'{raw}' is not a full path.", status=400, next_action="Choose a folder with Browse… or type a full path such as D:\\AI models.")
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / f".rs-write-test-{uuid.uuid4().hex[:6]}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            raise ToolSetupError("not_writable", f"Rebuild Studio cannot write to {p} ({e.strerror or e}).", status=400, affected=str(p),
                                 next_action="Pick a folder you own, for example inside your user folder or on a data drive.") from e
        free = shutil.disk_usage(p).free
        if need_bytes and free < need_bytes + FREE_SPACE_MARGIN:
            raise ToolSetupError("no_space", f"Not enough free space in {p}: {free / 1e9:.1f} GB free, {need_bytes / 1e9:.1f} GB needed.",
                                 status=409, affected=str(p), next_action="Free some space or choose a folder on another drive.")
        return {"path": str(p), "free_bytes": free, "writable": True}

    def set_models_dir(self, path: str) -> dict[str, Any]:
        chk = self.check_folder(path)
        self.local_ai.save_settings(models_dir=chk["path"])
        return {**self.config(), "free_bytes": chk["free_bytes"]}

    def set_hf_token(self, token: str | None) -> dict[str, Any]:
        if self.secrets is None:
            raise ToolSetupError("unavailable", "The credential store is unavailable.", status=503)
        st = self.local_ai.settings()
        old = st.get("hf_token_ref")
        ref = None
        if token:
            token = token.strip()
            if not re.fullmatch(r"[A-Za-z0-9_\-]{8,200}", token):
                raise ToolSetupError("bad_token", "That does not look like a Hugging Face access token.", status=400,
                                     next_action="Create a read token at huggingface.co → Settings → Access Tokens and paste it.")
            ref = self.secrets.put(token)
        self.local_ai.save_settings(hf_token_ref=ref)
        if old and old != ref:
            self.secrets.delete(old)
        return self.config()

    # ------------------------------------------------------------------ search
    def search(self, q: str, limit: int = 20) -> dict[str, Any]:
        q = (q or "").strip()
        if not q or len(q) > 100:
            raise ToolSetupError("bad_query", "Type part of a model name to search (for example 'qwen2.5 coder').", status=400)
        limit = max(1, min(int(limit), 50))
        params: list[tuple[str, Any]] = [("search", q), ("filter", "gguf"), ("sort", "downloads"), ("direction", "-1"), ("limit", limit)]
        params += [("expand[]", k) for k in ("downloads", "likes", "tags", "gated", "lastModified", "pipeline_tag")]
        rows = self._hf_json("/api/models", params)
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            lic = license_of(r.get("tags"))
            gated = r.get("gated")
            out.append({"repo": r["id"], "downloads": r.get("downloads"), "likes": r.get("likes"), "license": lic,
                        "license_permissive": lic in PERMISSIVE, "gated": bool(gated) if gated is not None else None,
                        "pipeline_tag": r.get("pipeline_tag"), "updated": r.get("lastModified"),
                        "page": f"{self.hf_base}/{r['id']}"})
        return {"query": q, "results": out, "source": urlsplit(self.hf_base).hostname}

    def files(self, repo: str) -> dict[str, Any]:
        if not _REPO.match(repo or ""):
            raise ToolSetupError("bad_repo", f"'{repo}' is not a Hugging Face model id (owner/name).", status=400)
        info = self._hf_json(f"/api/models/{repo}")
        rev = str(info.get("sha") or "main")
        tree = self._hf_json(f"/api/models/{repo}/tree/{quote(rev, safe='')}", {"recursive": "1"})
        lic = license_of(info.get("tags"), info.get("cardData") if isinstance(info.get("cardData"), dict) else None)
        gated = info.get("gated")
        files = []
        for f in tree if isinstance(tree, list) else []:
            if not isinstance(f, dict) or f.get("type") != "file" or not str(f.get("path", "")).lower().endswith(".gguf"):
                continue
            lfs = f.get("lfs") if isinstance(f.get("lfs"), dict) else {}
            sha = lfs.get("oid") if isinstance(lfs.get("oid"), str) and re.fullmatch(r"[0-9a-f]{64}", lfs.get("oid") or "") else None
            size = int(lfs.get("size") or f.get("size") or 0)
            name = str(f["path"])
            kind = "vision_projector" if "mmproj" in name.lower() else "model"
            split = bool(_SPLIT.search(name))
            note = None
            if split:
                note = "Split into several files; not supported here yet. Pick a single-file quantization."
            elif kind == "vision_projector":
                note = "Vision add-on file, not a model on its own."
            elif not sha:
                note = "The Hub did not report a SHA-256 for this file, so it cannot be verified."
            files.append({"path": name, "size_bytes": size, "sha256": sha, "quant": quant_of(name), "kind": kind, "split": split,
                          "ram_hint_gb": ram_hint_gb(size), "downloadable": kind == "model" and not split and bool(sha), "note": note})
        files.sort(key=lambda x: (not x["downloadable"], x["size_bytes"]))
        needs_ack = (lic not in PERMISSIVE) or bool(gated)
        return {"repo": repo, "revision": rev, "license": lic, "license_permissive": lic in PERMISSIVE, "license_ack_required": needs_ack,
                "gated": bool(gated), "gated_mode": gated if isinstance(gated, str) else None, "needs_token": bool(gated) and not self._token(),
                "has_token": bool(self._token()), "page": f"{self.hf_base}/{repo}", "files": files,
                "license_note": (None if not needs_ack else
                                 (f"This model is gated: accept its terms on {self.hf_base}/{repo} first." if gated else
                                  f"License '{lic or 'not stated'}' is not a standard permissive license; read it on the model page "
                                  "and confirm you accept it."))}

    # ------------------------------------------------------------------ jobs
    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.view() for j in sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)]

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                raise ToolSetupError("unknown_job", "No such download.", status=404)
            return j.view()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                raise ToolSetupError("unknown_job", "No such download.", status=404)
            if j.finished:
                raise ToolSetupError("not_running", "That download already finished.", status=409, next_action="Nothing to cancel.")
            j.cancel.set()
            return j.view()

    def join(self, job_id: str, timeout: float | None = None) -> bool:
        th = self._threads.get(job_id)
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, payload)
        except Exception:  # noqa: BLE001
            pass

    def _progress(self, j: _Job, *, phase: str | None = None, done: int | None = None, total: int | None = None,
                  message: str | None = None, force: bool = False) -> None:
        with self._lock:
            changed = phase is not None and phase != j.phase
            if phase is not None:
                j.phase = phase
            if total is not None:
                j.bytes_total = total
            if done is not None:
                j.bytes_done = done
                now = time.monotonic()
                j._samples.append((now, done))
                j._samples = [s for s in j._samples if now - s[0] <= 5.0]
                if len(j._samples) >= 2 and j._samples[-1][0] > j._samples[0][0]:
                    dt = j._samples[-1][0] - j._samples[0][0]
                    j.speed_bps = max(0.0, (j._samples[-1][1] - j._samples[0][1]) / dt)
                    j.eta_s = round((j.bytes_total - done) / j.speed_bps, 1) if j.speed_bps and j.bytes_total > done else None
            if message is not None:
                j.message = message
            now = time.monotonic()
            emit = changed or force or now - j.last_emit > 0.5
            if emit:
                j.last_emit = now
            view = j.view()
        if emit:
            self._emit("ai.local.download.progress", view)

    def _start(self, j: _Job, target: Callable[[_Job], None]) -> dict[str, Any]:
        with self._lock:
            for other in self._jobs.values():
                if not other.finished and other.kind == j.kind and other.title == j.title:
                    raise ToolSetupError("busy", f"{j.title} is already in progress.", status=409, next_action="Wait for it or cancel it.")
            self._jobs[j.job_id] = j

        def run() -> None:
            try:
                target(j)
                with self._lock:
                    j.phase, j.finished = "done", True
                self._emit("ai.local.download.done", j.view())
            except _Cancelled:
                with self._lock:
                    j.phase, j.finished, j.message = "cancelled", True, j.message or "Cancelled."
                self._emit("ai.local.download.cancelled", j.view())
            except ToolSetupError as e:
                with self._lock:
                    j.phase, j.finished, j.error, j.message = "failed", True, e.to_dict(), e.message
                self._emit("ai.local.download.failed", j.view())
            except Exception as e:  # noqa: BLE001
                log.exception("model job failed")
                with self._lock:
                    err = ToolSetupError("internal", f"Unexpected error: {type(e).__name__}", retryable=True, next_action="Try again.")
                    j.phase, j.finished, j.error, j.message = "failed", True, err.to_dict(), err.message
                self._emit("ai.local.download.failed", j.view())

        th = threading.Thread(target=run, name=f"model-{j.kind}-{j.job_id}", daemon=True)
        self._threads[j.job_id] = th
        th.start()
        return j.view()

    # ------------------------------------------------------------------ download
    def start_download(self, repo: str, path: str, *, dest_dir: str | None = None, accept_license: bool = False,
                       register: bool = True) -> dict[str, Any]:
        meta = self.files(repo)
        f = next((x for x in meta["files"] if x["path"] == path), None)
        if f is None:
            raise ToolSetupError("not_found", f"{path} is not a GGUF file in {repo}.", status=404, next_action="Pick a listed file.")
        if not f["downloadable"]:
            raise ToolSetupError("not_downloadable", f["note"] or "This file cannot be downloaded here.", status=409)
        if meta["gated"] and not meta["has_token"]:
            raise ToolSetupError("gated_needs_token",
                                 f"{repo} is gated: Hugging Face only gives it to signed-in users who accepted its terms.", status=409,
                                 url=meta["page"],
                                 next_action=f"Accept the terms on {meta['page']}, then paste a Hugging Face read token under "
                                             "'Hugging Face token' (optional; stored in the credential store) and try again.")
        if meta["license_ack_required"] and not accept_license:
            raise ToolSetupError("license_ack_required", meta["license_note"] or "Please confirm the model license.", status=409,
                                 url=meta["page"], next_action="Read the license on the model page and tick 'I accept the license'.")
        folder = Path(self.check_folder(dest_dir or self.models_dir())["path"])
        target_dir = folder / re.sub(r"[^A-Za-z0-9._-]+", "__", repo)
        name = Path(path).name
        final = target_dir / name
        part_size = (target_dir / (name + ".part")).stat().st_size if (target_dir / (name + ".part")).is_file() else 0
        self.check_folder(folder, max(0, f["size_bytes"] - part_size))
        j = _Job(job_id=uuid.uuid4().hex[:12], kind="download", title=f"{repo}/{name}", repo=repo, file=path, dest=str(final),
                 bytes_total=f["size_bytes"], message="Waiting to start")
        spec = {"repo": repo, "path": path, "revision": meta["revision"], "sha256": f["sha256"], "size": f["size_bytes"],
                "quant": f["quant"], "license": meta["license"], "license_accepted": bool(accept_license), "final": final,
                "register": register}
        return self._start(j, lambda job: self._download_job(job, spec))

    def _download_job(self, j: _Job, spec: dict[str, Any]) -> None:
        final: Path = spec["final"]
        final.parent.mkdir(parents=True, exist_ok=True)
        part = final.with_name(final.name + ".part")
        want, size = spec["sha256"], int(spec["size"])
        if final.is_file() and final.stat().st_size == size:
            self._progress(j, phase="verifying", done=size, total=size, message="Already downloaded; checking it", force=True)
            if _sha256(final, j.cancel) == want:
                rec = self._record(spec, final)
                return self._finish_registration(j, rec, spec)
            final.unlink()
        url = f"{self.hf_base}/{spec['repo']}/resolve/{quote(spec['revision'], safe='')}/{quote(spec['path'])}"
        try:
            digest = self._stream(j, url, part, size)
        except _Cancelled:
            _unlink(part)
            j.message = "Cancelled. The partial download was deleted."
            raise
        self._progress(j, phase="verifying", message="Verifying SHA-256", force=True)
        if digest != want:
            _unlink(part)
            raise ToolSetupError("checksum_mismatch", "The downloaded file does not match the SHA-256 Hugging Face publishes for it; "
                                 "it was deleted and nothing was added.", retryable=True, affected=spec["path"],
                                 next_action="Retry the download. If it fails again, pick another quantization or repository.")
        os.replace(part, final)
        rec = self._record(spec, final)
        self._finish_registration(j, rec, spec)

    def _stream(self, j: _Job, url: str, part: Path, size: int) -> str:
        """Download ``url`` into ``part`` (resuming an existing partial file with Range). Returns the sha256 of the whole file."""
        h = hashlib.sha256()
        have = part.stat().st_size if part.is_file() else 0
        if have > size:
            _unlink(part)
            have = 0
        if have:
            with open(part, "rb") as fh:          # re-hash what is already on disk so the final digest covers every byte
                while True:
                    if j.cancel.is_set():
                        raise _Cancelled()
                    b = fh.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
        j.resumed_from = have
        self._progress(j, phase="downloading", done=have, total=size,
                       message=f"Resuming from {have / 1e6:.0f} MB" if have else "Downloading", force=True)
        if have == size:
            return h.hexdigest()
        cur = url
        try:
            with self._client(read_timeout=60.0) as c:
                for _ in range(8):
                    self._check_hf_url(cur)
                    headers = {**self._auth_for(cur)} if cur == url else {}     # never forward the token after a redirect
                    if have:
                        headers["range"] = f"bytes={have}-"
                    with c.stream("GET", cur, headers=headers) as r:
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            cur = urljoin(cur, r.headers["location"])
                            continue
                        if r.status_code in (401, 403):
                            raise ToolSetupError("gated", "Hugging Face refused the download (gated model or terms not accepted).",
                                                 status=403, next_action="Accept the model's terms on its page and add a Hugging Face token, then retry.")
                        if r.status_code == 416 and have:
                            return h.hexdigest() if have == size else self._restart(j, part, h)
                        if r.status_code == 200 and have:
                            h = hashlib.sha256()   # the server ignored Range: start over
                            have = 0
                            j.resumed_from = 0
                        elif r.status_code not in (200, 206):
                            raise ToolSetupError("download_failed", f"The download server answered HTTP {r.status_code}.",
                                                 retryable=r.status_code >= 500 or r.status_code in (408, 429), status=502,
                                                 next_action="Retry in a few minutes; a partial download is kept and resumed.")
                        with open(part, "ab" if have else "wb") as fo:
                            done = have
                            for chunk in r.iter_bytes(CHUNK):
                                if j.cancel.is_set():
                                    raise _Cancelled()
                                done += len(chunk)
                                if done > size:
                                    fo.close()
                                    _unlink(part)
                                    raise ToolSetupError("size_mismatch", "The server sent more data than the file should have; it was deleted.",
                                                         retryable=True, next_action="Retry the download.")
                                fo.write(chunk)
                                h.update(chunk)
                                self._progress(j, done=done)
                        self._progress(j, done=done, force=True)
                        if done != size:
                            raise ToolSetupError("interrupted", f"The download stopped at {done / 1e6:.0f} of {size / 1e6:.0f} MB.",
                                                 retryable=True, next_action="Press Retry: it continues from where it stopped.")
                        return h.hexdigest()
                raise ToolSetupError("download_failed", "Too many redirects.", retryable=True, status=502)
        except httpx.HTTPError as e:
            got = part.stat().st_size if part.is_file() else 0
            raise ToolSetupError("interrupted", f"The connection dropped ({type(e).__name__}) after {got / 1e6:.0f} MB.",
                                 retryable=True, next_action="Press Retry: it continues from where it stopped.") from e

    def _restart(self, j: _Job, part: Path, h: Any) -> str:
        _unlink(part)
        raise ToolSetupError("interrupted", "The partial file did not match the server; it was deleted.", retryable=True,
                             next_action="Retry the download.")

    # ------------------------------------------------------------------ registry
    def _registry(self) -> list[dict[str, Any]]:
        return [d for d in (self.local_ai.settings().get("downloads") or []) if isinstance(d, dict)]

    def _save_registry(self, rows: list[dict[str, Any]]) -> None:
        self.local_ai.save_settings(downloads=rows)

    def _record(self, spec: dict[str, Any], final: Path) -> dict[str, Any]:
        with self._lock:
            rows = [r for r in self._registry() if r.get("path") != str(final)]
            rec = {"id": uuid.uuid4().hex[:12], "repo": spec["repo"], "file": spec["path"], "path": str(final), "size_bytes": spec["size"],
                   "sha256": spec["sha256"], "quant": spec["quant"], "license": spec["license"],
                   "license_accepted": spec["license_accepted"], "revision": spec["revision"], "downloaded_at": now_iso(),
                   "registered_as": None, "server": None, "status": "downloaded"}
            rows.append(rec)
            self._save_registry(rows)
            return rec

    def _update_record(self, rec_id: str, **kv: Any) -> dict[str, Any]:
        with self._lock:
            rows = self._registry()
            for r in rows:
                if r.get("id") == rec_id:
                    r.update(kv)
                    self._save_registry(rows)
                    return r
        raise ToolSetupError("unknown_model", "No such downloaded model.", status=404)

    def downloads(self) -> list[dict[str, Any]]:
        out = []
        for r in self._registry():
            p = Path(r.get("path") or "")
            out.append({**r, "exists": p.is_file(), "folder": str(p.parent)})
        return out

    def _ollama_root(self) -> str | None:
        snap = self.local_ai.snapshot(max_age_s=5.0)
        for s in snap.get("servers", []):
            if s["kind"] == "ollama" and s["found"]:
                return s["endpoint"][: -len("/v1")]
        return None

    def _other_server(self) -> str | None:
        snap = self.local_ai.snapshot(max_age_s=5.0)
        return next((s["name"] for s in snap.get("servers", []) if s["found"] and s["kind"] != "ollama"), None)

    def _finish_registration(self, j: _Job, rec: dict[str, Any], spec: dict[str, Any]) -> None:
        if not spec.get("register", True):
            j.result = rec
            j.message = "Downloaded and verified."
            return
        rec = self.register(rec["id"], job=j)
        j.result = rec
        j.message = rec.get("status_text") or "Done."

    def register(self, rec_id: str, *, job: _Job | None = None) -> dict[str, Any]:
        rec = next((r for r in self._registry() if r.get("id") == rec_id), None)
        if rec is None:
            raise ToolSetupError("unknown_model", "No such downloaded model.", status=404)
        path = Path(rec["path"])
        if not path.is_file():
            raise ToolSetupError("file_missing", f"{path} is no longer there.", status=409, next_action="Download it again.")
        root = self._ollama_root()
        if root is None:
            other = self._other_server()
            txt = (f"Downloaded and verified. {other} is running: load this file from {path.parent} in {other}."
                   if other else "Downloaded and verified - needs a local AI server (Ollama or LM Studio) to run.")
            return self._update_record(rec_id, status="needs_server" if not other else "needs_import", status_text=txt,
                                       install_pages=INSTALL_PAGES)
        name = ollama_name_for(rec["repo"], rec["file"])
        if job is not None:
            self._progress(job, phase="registering", done=0, total=int(rec["size_bytes"] or path.stat().st_size),
                           message=f"Adding it to Ollama as {name}", force=True)
        digest = f"sha256:{rec['sha256']}"
        blob = f"{root}/api/blobs/{digest}"
        require_loopback(blob)
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0, write=600.0), trust_env=False) as c:
                head = c.head(blob)
                if head.status_code != 200:
                    total = path.stat().st_size

                    def body() -> Iterator[bytes]:
                        sent = 0
                        with open(path, "rb") as fh:
                            while True:
                                b = fh.read(CHUNK)
                                if not b:
                                    break
                                sent += len(b)
                                if job is not None:
                                    if job.cancel.is_set():
                                        raise _Cancelled()
                                    self._progress(job, done=sent)
                                yield b
                    up = c.post(blob, content=body(), headers={"content-length": str(total)})
                    if up.status_code not in (200, 201):
                        raise ToolSetupError("register_failed", f"Ollama refused the file upload (HTTP {up.status_code}: {up.text[:200]}).",
                                             retryable=True, status=502, next_action="Check that Ollama is running and has disk space, then press 'Add to Ollama'.")
                url = f"{root}/api/create"
                require_loopback(url)
                cr = c.post(url, json={"model": name, "files": {path.name: digest}, "stream": False})
                if cr.status_code != 200:
                    raise ToolSetupError("register_failed", f"Ollama could not create the model (HTTP {cr.status_code}: {cr.text[:200]}).",
                                         retryable=True, status=502, next_action="Update Ollama (0.5 or newer), then press 'Add to Ollama'.")
        except httpx.HTTPError as e:
            raise ToolSetupError("register_failed", f"Could not talk to Ollama ({type(e).__name__}).", retryable=True, status=502,
                                 next_action="Start Ollama, then press 'Add to Ollama'.") from e
        rec = self._update_record(rec_id, registered_as=name, server="ollama", status="registered",
                                  status_text=f"Ready: added to Ollama as {name}.")
        try:
            self.local_ai.detect()
        except Exception:  # noqa: BLE001
            pass
        return rec

    def remove(self, rec_id: str, *, unregister: bool = False) -> dict[str, Any]:
        rec = next((r for r in self._registry() if r.get("id") == rec_id), None)
        if rec is None:
            raise ToolSetupError("unknown_model", "No such downloaded model.", status=404)
        unregistered = None
        if unregister and rec.get("registered_as"):
            root = self._ollama_root()
            if root is None:
                raise ToolSetupError("server_missing", "Ollama is not running, so the registered model cannot be removed from it.",
                                     status=409, next_action="Start Ollama and try again, or remove only the file.")
            url = f"{root}/api/delete"
            require_loopback(url)
            with httpx.Client(timeout=30.0, trust_env=False) as c:
                r = c.request("DELETE", url, json={"model": rec["registered_as"]})
            if r.status_code not in (200, 404):
                raise ToolSetupError("unregister_failed", f"Ollama could not remove {rec['registered_as']} (HTTP {r.status_code}).",
                                     status=502, retryable=True)
            unregistered = rec["registered_as"]
        p = Path(rec["path"])
        _unlink(p)
        _unlink(p.with_name(p.name + ".part"))
        try:
            p.parent.rmdir()
        except OSError:
            pass
        with self._lock:
            self._save_registry([r for r in self._registry() if r.get("id") != rec_id])
        if unregistered:
            try:
                self.local_ai.detect()
            except Exception:  # noqa: BLE001
                pass
        return {"removed": rec_id, "path": str(p), "unregistered": unregistered}

    # ------------------------------------------------------------------ Ollama library pull (by name)
    def start_pull(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not _OLLAMA_NAME.match(name) or "://" in name:
            raise ToolSetupError("bad_name", f"'{name}' is not an Ollama model name (for example qwen2.5-coder:7b).", status=400)
        root = self._ollama_root()
        if root is None:
            raise ToolSetupError("server_missing", "Ollama is not running on this PC.", status=409, url=INSTALL_PAGES["ollama"],
                                 next_action=f"Install or start Ollama ({INSTALL_PAGES['ollama']}), then press 'Detect again'.")
        j = _Job(job_id=uuid.uuid4().hex[:12], kind="pull", title=name, dest=ollama_models_folder(), message="Asking Ollama to pull")
        return self._start(j, lambda job: self._pull_job(job, root, name))

    def _pull_job(self, j: _Job, root: str, name: str) -> None:
        url = f"{root}/api/pull"
        require_loopback(url)
        layers: dict[str, tuple[int, int]] = {}
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0), trust_env=False) as c:
                with c.stream("POST", url, json={"model": name, "stream": True}) as r:
                    if r.status_code != 200:
                        r.read()
                        raise ToolSetupError("pull_failed", f"Ollama could not pull {name} (HTTP {r.status_code}: {r.text[:200]}).",
                                             status=502, next_action="Check the name on ollama.com/library and try again.")
                    self._progress(j, phase="downloading", message="Downloading through Ollama", force=True)
                    for line in r.iter_lines():
                        if j.cancel.is_set():
                            raise _Cancelled()
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("error"):
                            raise ToolSetupError("pull_failed", f"Ollama: {str(ev['error'])[:300]}", status=502,
                                                 next_action="Check the model name on ollama.com/library.")
                        if ev.get("digest") and ev.get("total"):
                            layers[ev["digest"]] = (int(ev.get("completed") or 0), int(ev["total"]))
                        done = sum(a for a, _ in layers.values())
                        total = sum(b for _, b in layers.values())
                        self._progress(j, done=done, total=total, message=str(ev.get("status") or "")[:120])
                        if ev.get("status") == "success":
                            break
        except httpx.HTTPError as e:
            raise ToolSetupError("pull_failed", f"Lost the connection to Ollama ({type(e).__name__}).", retryable=True, status=502,
                                 next_action="Make sure Ollama is still running and try again; Ollama resumes its own downloads.") from e
        j.result = {"model": name, "server": "ollama", "folder": ollama_models_folder()}
        j.message = f"Pulled {name} into Ollama's model folder."
        try:
            self.local_ai.detect()
        except Exception:  # noqa: BLE001
            pass


def _sha256(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
