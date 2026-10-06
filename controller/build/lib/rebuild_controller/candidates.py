"""Candidates: staged reconstruction outputs with manifests, build hashes and last-known-good tracking."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .cases import CaseStore
from .events import EventLog
from .ids import new_id, now_iso, sha256_file, stable_json_hash
from .paths import PathPolicyError, resolve_final, is_within
from .store.db import Database, loads

MAX_PROPOSED_FILES = 2000
MAX_PROPOSED_BYTES = 64 * 1024 * 1024


def tree_manifest(root: Path) -> dict[str, Any]:
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(root).as_posix()
            if rel.startswith("target/") or "/node_modules/" in ("/" + rel):
                continue
            files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)})
    return {"files": files, "count": len(files), "tree_sha": stable_json_hash([(f["path"], f["sha256"]) for f in files])}


class CandidateStore:
    def __init__(self, db: Database, events: EventLog, cases: CaseStore):
        self.db = db
        self.events = events
        self.cases = cases

    def _next_revision(self, case_id: str) -> int:
        return self.db.query_one("SELECT COALESCE(MAX(revision),0)+1 AS r FROM candidates WHERE case_id=?", (case_id,))["r"]

    def create(self, case_id: str, *, target_language: str, output_type: str, plan_revision: int, source_dir: Path | None = None,
               meta: dict[str, Any] | None = None) -> dict[str, Any]:
        cid = new_id("cand")
        rev = self._next_revision(case_id)
        root = self.cases.case_root(case_id) / "candidates" / cid
        src = root / "source"
        src.mkdir(parents=True, exist_ok=True)
        if source_dir is not None:
            shutil.copytree(source_dir, src, dirs_exist_ok=True)
        self.db.insert("candidates", {"candidate_id": cid, "case_id": case_id, "revision": rev, "target_language": target_language,
                                      "output_type": output_type, "source_dir": str(src), "dist_dir": None, "build_hash": None,
                                      "manifest_sha": None, "plan_revision": plan_revision, "build_status": "pending",
                                      "verification": "untested", "last_known_good": 0, "created_at": now_iso(), "meta": meta or {}})
        c = self.get(cid)
        self.events.emit("candidate.created", {"candidate": c}, case_id=case_id)
        return c

    def propose(self, case_id: str, files: dict[str, str], *, note: str, author: str, base_candidate: str | None, plan_revision: int) -> dict[str, Any]:
        """Model-facing: write proposed files into a NEW candidate's staging source (never the original or baseline)."""
        if len(files) > MAX_PROPOSED_FILES:
            raise ValueError("too many files in proposal")
        total = sum(len(v.encode("utf-8")) for v in files.values())
        if total > MAX_PROPOSED_BYTES:
            raise ValueError("proposal too large")
        case = self.cases.get_case(case_id)
        base_src = Path(self.get(base_candidate)["source_dir"]) if base_candidate else None
        c = self.create(case_id, target_language=case["target_language"], output_type=case["output_type"], plan_revision=plan_revision,
                        source_dir=base_src, meta={"note": note[:2000], "author": author, "base_candidate": base_candidate, "proposed_files": sorted(files)})
        src = Path(c["source_dir"])
        for rel, content in files.items():
            relp = Path(rel)
            if relp.is_absolute() or ".." in relp.parts or not rel.strip():
                raise PathPolicyError(f"proposed path not allowed: {rel}")
            dest = src / relp
            if not is_within(resolve_final(dest.parent if dest.parent.exists() else src), resolve_final(src)):
                raise PathPolicyError(f"proposed path escapes candidate: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        return self.get(c["candidate_id"])

    def get(self, candidate_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
        if not r:
            raise KeyError(candidate_id)
        r["meta"] = loads(r["meta"], {})
        r["last_known_good"] = bool(r["last_known_good"])
        return r

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self.get(r["candidate_id"]) for r in self.db.query("SELECT candidate_id FROM candidates WHERE case_id=? ORDER BY revision", (case_id,))]

    def mark_building(self, candidate_id: str) -> None:
        self.db.update("candidates", "candidate_id", candidate_id, {"build_status": "building"})

    def mark_built(self, candidate_id: str, dist_dir: Path, *, build_log_evidence: str | None = None) -> dict[str, Any]:
        man = tree_manifest(dist_dir)
        src_man = tree_manifest(Path(self.get(candidate_id)["source_dir"]))
        build_hash = stable_json_hash({"dist": man["tree_sha"], "source": src_man["tree_sha"]})[:16]
        c = self.get(candidate_id)
        ev = self.cases.add_evidence(c["case_id"], "candidate_manifest", f"Candidate {candidate_id} manifest",
                                     body={"dist": man, "source": src_man, "build_hash": build_hash, "build_log": build_log_evidence},
                                     inputs={"candidate": candidate_id, "build_hash": build_hash}, producer="builder")
        self.db.update("candidates", "candidate_id", candidate_id, {"build_status": "built", "dist_dir": str(dist_dir), "build_hash": build_hash,
                                                                   "manifest_sha": ev["blob_sha"], "verification": "untested"})
        c = self.get(candidate_id)
        self.events.emit("candidate.built", {"candidate": c}, case_id=c["case_id"])
        return c

    def mark_failed(self, candidate_id: str, error: str) -> None:
        c = self.get(candidate_id)
        self.db.update("candidates", "candidate_id", candidate_id, {"build_status": "failed", "meta": {**c["meta"], "build_error": error[:4000]}})
        self.events.emit("candidate.failed", {"candidate_id": candidate_id, "error": error[:2000]}, case_id=c["case_id"])

    def set_verification(self, candidate_id: str, verification: str, *, writer: str) -> None:
        if writer != "verifier":
            raise PermissionError("only the verifier may write candidate verification")
        c = self.get(candidate_id)
        fields: dict[str, Any] = {"verification": verification}
        if verification == "verified":
            self.db.execute("UPDATE candidates SET last_known_good=0 WHERE case_id=?", (c["case_id"],))
            fields["last_known_good"] = 1
        self.db.update("candidates", "candidate_id", candidate_id, fields)

    def last_known_good(self, case_id: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT candidate_id FROM candidates WHERE case_id=? AND last_known_good=1", (case_id,))
        return self.get(r["candidate_id"]) if r else None

    def manifest(self, candidate_id: str) -> dict[str, Any] | None:
        c = self.get(candidate_id)
        return self.cases.blobs.get_json(c["manifest_sha"]) if c["manifest_sha"] else None
