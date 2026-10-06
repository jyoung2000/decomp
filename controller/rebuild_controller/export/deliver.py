"""Atomic publication of source/ dist/ evidence/ reports/ into the output root, with a manifest covering every shipped file."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..ids import now_iso, sha256_file
from ..jobs.runner import StageContext, StageError
from ..paths import PathPolicyError, assert_output_not_in_source, resolve_final
from .report import write_reports


def deliver(ctx: StageContext) -> dict[str, Any]:
    st = ctx.services["studio"]
    case = st.cases.get_case(ctx.job.case_id)
    cid = ctx.job.inputs["candidate_id"]
    cand = st.candidates.get(cid)
    out_root = Path(case["output_root"])
    assert_output_not_in_source(out_root, Path(case["source_root"]))
    staging = st.cases.case_root(case["case_id"]) / "publish.staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "source").mkdir(parents=True)
    src = Path(cand["source_dir"])
    shutil.copytree(src, staging / "source", dirs_exist_ok=True, ignore=shutil.ignore_patterns("target", "node_modules", ".preview-state"))
    if cand["dist_dir"] and Path(cand["dist_dir"]).exists():
        shutil.copytree(cand["dist_dir"], staging / "dist", dirs_exist_ok=True)
    else:
        (staging / "dist").mkdir()
        (staging / "dist" / "NOT_BUILT.txt").write_text(f"candidate {cid} build status: {cand['build_status']}\n")
    ev_dir = staging / "evidence"; ev_dir.mkdir()
    _export_evidence(st, case["case_id"], ev_dir)
    rep_dir = staging / "reports"; rep_dir.mkdir()
    report = write_reports(st, case["case_id"], cid, rep_dir)
    from .report import export_plan
    export_plan(st, case["case_id"], rep_dir)   # project-plan.json/html ship inside the manifest
    # manifest covering every shipped file (written last, includes itself as 'manifest.json' entry without hash)
    files = []
    for p in sorted(staging.rglob("*")):
        if p.is_file():
            files.append({"path": p.relative_to(staging).as_posix(), "size": p.stat().st_size, "sha256": sha256_file(p)})
    manifest = {"case_id": case["case_id"], "candidate_id": cid, "build_hash": cand["build_hash"], "published_at": now_iso(), "files": files,
                "note": "manifest.json itself is listed without a hash", "verification": st.candidates.get(cid)["verification"]}
    files.append({"path": "manifest.json", "size": None, "sha256": None})
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=1))
    # publish: new output directory; preserve unrelated existing files, report conflicts
    out_root.mkdir(parents=True, exist_ok=True)
    conflicts = []
    for sub in ("source", "dist", "evidence", "reports", "manifest.json"):
        dest = out_root / sub
        if dest.exists():
            prev = _previous_manifest(out_root)
            if prev is None:
                conflicts.append(str(dest))
                continue
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        os.replace(staging / sub, dest) if (staging / sub).exists() else None
    shutil.rmtree(staging, ignore_errors=True)
    if conflicts:
        st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-DELIVER"), status="blocked",
                            blockers=[f"output conflict: {', '.join(conflicts)} exist and were not written by Rebuild Studio; choose an empty output folder or remove them"])
        raise StageError(f"output conflicts: {conflicts}", blocker="existing non-Rebuild-Studio files in output; pick another folder", )
    st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-DELIVER"), status="completed", files=[str(out_root)])
    st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-PACKAGE"), status="completed" if cand["build_status"] == "built" else "failed", files=[str(out_root / "dist")])
    st.cases.set_case_status(case["case_id"], "delivered")
    st.plan.revise(case["case_id"], "delivered output")
    return {"output_root": str(out_root), "files": len(files), "report": report.get("summary"), "verification": manifest["verification"]}


def _previous_manifest(out_root: Path) -> dict[str, Any] | None:
    m = out_root / "manifest.json"
    if not m.exists():
        return None
    try:
        d = json.loads(m.read_text())
        return d if "case_id" in d and "files" in d else None
    except ValueError:
        return None


def _export_evidence(st, case_id: str, ev_dir: Path) -> None:
    index = []
    for ev in st.cases.list_evidence(case_id, include_stale=True):
        entry = {k: ev[k] for k in ("evidence_id", "kind", "title", "module_id", "revision", "producer", "created_at", "stale", "input_hash", "blob_sha", "meta")}
        index.append(entry)
        if ev["blob_sha"] and ev["kind"] in ("baseline", "verification_report", "inventory", "dependency_graph", "module_report", "candidate_manifest"):
            p = st.cases.blobs.path_for(ev["blob_sha"])
            if p.exists() and p.stat().st_size < 50_000_000:
                shutil.copyfile(p, ev_dir / f"{ev['evidence_id']}.json")
    (ev_dir / "index.json").write_text(json.dumps(index, indent=1, default=str))
    (ev_dir / "features.json").write_text(json.dumps(st.ledger.list(case_id), indent=1, default=str))
    (ev_dir / "comparisons.json").write_text(json.dumps(st.verifier.comparisons(case_id), indent=1, default=str))
    (ev_dir / "modules.json").write_text(json.dumps(st.cases.modules(case_id), indent=1, default=str))
    (ev_dir / "provenance.json").write_text(json.dumps({"backends": st.doctor(), "exported_at": now_iso()}, indent=1, default=str))
