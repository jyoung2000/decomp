"""Cases, modules and the content-addressed evidence store."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .events import EventLog
from .ids import new_id, now_iso, sha256_bytes, stable_json_hash
from .paths import PathPolicyError, RootSet, resolve_final
from .store.db import Database, loads


class BlobStore:
    """Content-addressed blobs under blobs/<aa>/<sha>. Writes are atomic (temp + rename)."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put_bytes(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        dest = self.path_for(sha)
        if dest.exists():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return sha

    def put_json(self, obj: Any) -> str:
        return self.put_bytes(json.dumps(obj, sort_keys=True, indent=1, default=str).encode("utf-8"))

    def put_file(self, path: Path) -> str:
        from .ids import sha256_file
        sha = sha256_file(path)
        dest = self.path_for(sha)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".tmp-{new_id('b')}")
            shutil.copyfile(path, tmp)
            os.replace(tmp, dest)
        return sha

    def get_bytes(self, sha: str) -> bytes:
        return self.path_for(sha).read_bytes()

    def get_json(self, sha: str) -> Any:
        return json.loads(self.get_bytes(sha))

    def has(self, sha: str) -> bool:
        return self.path_for(sha).exists()


class CaseStore:
    def __init__(self, db: Database, events: EventLog, settings: Settings):
        self.db = db
        self.events = events
        self.settings = settings
        self.blobs = BlobStore(settings.blobs_dir)

    # -- cases ----------------------------------------------------------
    def create_case(self, *, name: str, source_root: str, output_root: str, target_language: str, output_type: str,
                    ai_policy: dict[str, Any] | None = None, launch_profile: dict[str, Any] | None = None,
                    settings: dict[str, Any] | None = None) -> dict[str, Any]:
        case_id = new_id("case")
        case_root = self.settings.cases_dir / case_id
        RootSet(Path(source_root), Path(output_root), case_root, install_root=_install_root()).validate()
        case_root.mkdir(parents=True, exist_ok=True)
        (case_root / "staging").mkdir(exist_ok=True)
        ts = now_iso()
        row = {
            "case_id": case_id, "created_at": ts, "updated_at": ts, "name": name,
            "source_root": str(resolve_final(source_root)), "output_root": str(resolve_final(output_root)),
            "target_language": target_language, "output_type": output_type,
            "ai_policy": ai_policy or {"mode": "no_ai"}, "launch_profile": launch_profile or {"execute_original": False},
            "status": "created", "app_version": __version__, "settings": settings or {},
        }
        self.db.insert("cases", row)
        self.events.emit("case.created", {"case": self.get_case(case_id)}, case_id=case_id)
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if not r:
            raise KeyError(case_id)
        for k in ("ai_policy", "launch_profile", "settings"):
            r[k] = loads(r[k], {})
        return r

    def list_cases(self) -> list[dict[str, Any]]:
        return [self.get_case(r["case_id"]) for r in self.db.query("SELECT case_id FROM cases ORDER BY created_at DESC")]

    def set_case_status(self, case_id: str, status: str, **extra: Any) -> None:
        self.db.update("cases", "case_id", case_id, {"status": status, "updated_at": now_iso(), **extra})
        self.events.emit("case.status", {"case_id": case_id, "status": status}, case_id=case_id)

    def case_root(self, case_id: str) -> Path:
        return self.settings.cases_dir / case_id

    def is_resumable(self, case_id: str) -> tuple[bool, str]:
        """A case may be resumed only if owned by this app and its schema/app version is compatible."""
        try:
            c = self.get_case(case_id)
        except KeyError:
            return False, "unknown case"
        if not (self.case_root(case_id) / "staging").exists():
            return False, "case workspace missing"
        major = c["app_version"].split(".")[0]
        if major != __version__.split(".")[0]:
            return False, f"case created by incompatible app version {c['app_version']}"
        return True, "ok"

    # -- modules --------------------------------------------------------
    def add_module(self, case_id: str, rel_path: str, sha256: str, size: int, fmt: str, profile: str,
                   arch: str | None = None, meta: dict[str, Any] | None = None) -> str:
        module_id = "mod_" + stable_json_hash({"case": case_id, "path": rel_path, "sha": sha256})[:20]
        self.db.upsert("modules", {"module_id": module_id, "case_id": case_id, "rel_path": rel_path, "sha256": sha256,
                                   "size": size, "format": fmt, "profile": profile, "arch": arch, "meta": meta or {}}, "module_id")
        return module_id

    def modules(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM modules WHERE case_id=? ORDER BY rel_path", (case_id,))
        for r in rows:
            r["meta"] = loads(r["meta"], {})
        return rows

    def get_module(self, module_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM modules WHERE module_id=?", (module_id,))
        if not r:
            raise KeyError(module_id)
        r["meta"] = loads(r["meta"], {})
        return r

    # -- evidence -------------------------------------------------------
    def add_evidence(self, case_id: str, kind: str, title: str, *, body: Any = None, body_bytes: bytes | None = None,
                     module_id: str | None = None, meta: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None,
                     producer: str = "controller") -> dict[str, Any]:
        """Store evidence; identical (kind, module, input_hash) with same content is deduplicated into the same id."""
        blob_sha = None
        if body_bytes is not None:
            blob_sha = self.blobs.put_bytes(body_bytes)
        elif body is not None:
            blob_sha = self.blobs.put_json(body)
        input_hash = stable_json_hash(inputs or {})
        existing = self.db.query_one(
            "SELECT * FROM evidence WHERE case_id=? AND kind=? AND IFNULL(module_id,'')=IFNULL(?,'') AND input_hash=? AND IFNULL(blob_sha,'')=IFNULL(?,'') AND stale=0",
            (case_id, kind, module_id, input_hash, blob_sha))
        if existing:
            existing["meta"] = loads(existing["meta"], {})
            return existing
        rev = self.db.query_one("SELECT COALESCE(MAX(revision),0)+1 AS r FROM evidence WHERE case_id=?", (case_id,))["r"]
        eid = new_id("ev")
        row = {"evidence_id": eid, "case_id": case_id, "revision": rev, "kind": kind, "module_id": module_id, "title": title,
               "blob_sha": blob_sha, "meta": meta or {}, "input_hash": input_hash, "producer": producer, "created_at": now_iso(), "stale": 0}
        self.db.insert("evidence", row)
        self.events.emit("evidence.added", {"evidence_id": eid, "kind": kind, "title": title, "module_id": module_id, "revision": rev,
                                            "producer": producer}, case_id=case_id)
        row["meta"] = meta or {}
        return row

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
        if not r:
            raise KeyError(evidence_id)
        r["meta"] = loads(r["meta"], {})
        return r

    def evidence_body(self, evidence_id: str, *, max_bytes: int | None = None) -> Any:
        r = self.get_evidence(evidence_id)
        if not r["blob_sha"]:
            return None
        data = self.blobs.get_bytes(r["blob_sha"])
        truncated = False
        if max_bytes is not None and len(data) > max_bytes:
            data, truncated = data[:max_bytes], True
        try:
            obj = json.loads(data) if not truncated else None
        except ValueError:
            obj = None
        if obj is not None:
            return obj
        return {"text": data.decode("utf-8", "replace"), "truncated": truncated, "total_bytes": len(self.blobs.get_bytes(r["blob_sha"]))}

    def list_evidence(self, case_id: str, kind: str | None = None, module_id: str | None = None, include_stale: bool = False) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM evidence WHERE case_id=?", [case_id]
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if module_id:
            sql += " AND module_id=?"; params.append(module_id)
        if not include_stale:
            sql += " AND stale=0"
        sql += " ORDER BY revision"
        rows = self.db.query(sql, tuple(params))
        for r in rows:
            r["meta"] = loads(r["meta"], {})
        return rows

    def search_evidence(self, case_id: str, query: str, *, kinds: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Lexical search over titles, metadata and (small) JSON bodies. Bounded result view."""
        q = query.lower()
        out = []
        for r in self.list_evidence(case_id):
            if kinds and r["kind"] not in kinds:
                continue
            hay = (r["title"] + " " + json.dumps(r["meta"])).lower()
            hit = q in hay
            if not hit and r["blob_sha"]:
                p = self.blobs.path_for(r["blob_sha"])
                if p.stat().st_size <= 2_000_000:
                    hit = q in p.read_text("utf-8", "replace").lower()
            if hit:
                out.append({k: r[k] for k in ("evidence_id", "kind", "title", "module_id", "revision", "meta")})
                if len(out) >= limit:
                    break
        return out

    def invalidate_evidence(self, case_id: str, *, kinds: list[str] | None = None, module_id: str | None = None, reason: str = "inputs changed") -> int:
        sql, params = "UPDATE evidence SET stale=1 WHERE case_id=? AND stale=0", [case_id]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds)); params += kinds
        if module_id:
            sql += " AND module_id=?"; params.append(module_id)
        n = self.db.execute(sql, tuple(params)).rowcount
        if n:
            self.events.emit("evidence.invalidated", {"count": n, "kinds": kinds, "module_id": module_id, "reason": reason}, case_id=case_id)
        return n


def _install_root() -> Path | None:
    env = os.environ.get("REBUILD_STUDIO_INSTALL")
    return Path(env) if env else None
