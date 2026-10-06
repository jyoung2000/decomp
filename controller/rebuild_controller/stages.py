"""Stage implementations for the rebuild pipeline. Each stage is a pure function of its StageContext and durable state."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .adapters.contract import Availability
from .jobs import JobState
from .backends.inventory import build_dependency_graph, inventory_root
from .jobs.runner import StageContext, StageError, StageRegistry

PROFILE_STAGE = {"native_pe": "analyze_module", "native_elf": "analyze_module", "dotnet": "recover_managed", "unity_mono": "recover_managed",
                 "godot": "recover_engine", "electron": "recover_web", "web": "recover_web"}
UNSUPPORTED_PROFILES = {"unity_il2cpp": "Unity IL2CPP: Cpp2IL profile is experimental/unverified in this build",
                        "gamemaker": "GameMaker: UndertaleModTool profile is experimental/unverified in this build",
                        "android": "Android: jadx/Apktool profile is experimental/unverified in this build",
                        "unreal": "Unreal: CUE4Parse profile is experimental/unverified in this build",
                        "jvm": "JVM: jadx profile is experimental/unverified in this build",
                        "native_macho": "Mach-O: no backend in this build"}


def studio_of(ctx: StageContext):
    return ctx.services["studio"]


def register_stages(reg: StageRegistry) -> None:
    reg.add("inventory", stage_inventory)
    reg.add("dependency_graph", stage_dependency_graph)
    reg.add("analyze_module", stage_analyze_module)
    reg.add("recover_managed", stage_recover_managed)
    reg.add("recover_engine", stage_recover_engine)
    reg.add("recover_web", stage_recover_web)
    reg.add("discover_features", stage_discover_features)
    reg.add("capture_original", stage_capture_original)
    reg.add("reconstruct", stage_reconstruct)
    reg.add("build_candidate", stage_build_candidate)
    reg.add("compare_candidate", stage_compare_candidate)
    reg.add("repair", stage_repair)
    reg.add("deliver", stage_deliver)
    from .pipeline import stage_barrier
    reg.add("barrier", stage_barrier)


# ------------------------------------------------------------------ analysis
def stage_inventory(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    root = Path(case["source_root"])
    inv = inventory_root(root, ctx.limits, progress=lambda p: ctx.progress(**p))
    ev = st.cases.add_evidence(case["case_id"], "inventory", "Installation inventory", body=inv, inputs={"root": str(root), "limit": ctx.limits.max_inventory_files},
                               meta={"file_count": inv["file_count"], "module_count": inv["module_count"], "truncated": inv["truncated"], "profile": inv["profile"]["primary"]})
    st.plan.add_evidence(st.plan.milestone_id(case["case_id"], "M-ANALYSIS"), ev["evidence_id"])
    modules = []
    for f in inv["files"]:
        d = f.get("detect", {})
        if f["path"] in inv["modules"] and f.get("sha256"):
            mid = st.cases.add_module(case["case_id"], f["path"], f["sha256"], f["size"], d["format"], d["profile"], d.get("arch"), {"flags": d.get("flags", {}), "bits": d.get("bits")})
            modules.append((mid, f["path"], d["profile"]))
    unsupported = []
    created = []
    for mid, rel, profile in modules:
        stage = PROFILE_STAGE.get(profile)
        if stage is None:
            unsupported.append({"module": rel, "profile": profile, "reason": UNSUPPORTED_PROFILES.get(profile, f"no backend for profile {profile}")})
            continue
        if profile == "web" and not rel.endswith((".js", ".mjs", ".cjs", ".asar")):
            continue
        j = st.jobs.create(case["case_id"], stage, f"{stage.replace('_', ' ')}: {rel}", {"module_id": mid}, depends_on=[ctx.job.job_id], milestone_id="M-RECOVERY")
        st.plan.link_job(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), j.job_id)
        created.append(j.job_id)
    if inv["profile"]["primary"] in ("web", "electron") and not created:
        j = st.jobs.create(case["case_id"], "recover_web", "recover web application", {"root": True}, depends_on=[ctx.job.job_id], milestone_id="M-RECOVERY")
        st.plan.link_job(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), j.job_id); created.append(j.job_id)
    # fan-in: the recovery summary (and everything after it) waits for the jobs created here
    for bj in st.jobs.list(case["case_id"]):
        if bj.stage == "barrier" and bj.state in (JobState.QUEUED, JobState.BLOCKED):
            for jid in created:
                st.jobs.add_dependency(bj.job_id, jid)
    for u in unsupported:
        st.plan.add_item(case["case_id"], title=f"Unsupported: {u['module']}", outcome=u["reason"], kind="unsupported", reason="detected unsupported profile")
    ctx.progress(files_scanned=inv["file_count"], modules=inv["module_count"], recovery_jobs=len(created), unsupported=len(unsupported))
    return {"evidence_id": ev["evidence_id"], "profile": inv["profile"], "modules": len(modules), "recovery_jobs": created, "unsupported": unsupported,
            "truncated": inv["truncated"], "unknown_scope": inv["unknown_scope"]}


def stage_dependency_graph(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    evs = st.cases.list_evidence(ctx.job.case_id, kind="inventory")
    if not evs:
        raise StageError("inventory evidence missing", retry=False)
    inv = st.cases.evidence_body(evs[-1]["evidence_id"])
    g = build_dependency_graph(inv)
    # package.json dependencies for web/electron
    root = Path(inv["root"])
    for f in inv["files"]:
        if f["path"].endswith("package.json") and "node_modules" not in f["path"]:
            try:
                pj = json.loads((root / f["path"]).read_text("utf-8"))
                for dep in list(pj.get("dependencies", {})) + list(pj.get("devDependencies", {})):
                    g["external"].setdefault(f["path"], []).append(dep)
            except Exception:
                pass
    ev = st.cases.add_evidence(ctx.job.case_id, "dependency_graph", "Dependency graph", body=g, inputs={"inventory": evs[-1]["evidence_id"]})
    st.plan.add_evidence(st.plan.milestone_id(ctx.job.case_id, "M-ASSETS"), ev["evidence_id"])
    return {"evidence_id": ev["evidence_id"], "edges": len(g["edges"]), "external": sum(len(v) for v in g["external"].values())}


def _backend(ctx: StageContext, backend_id: str, what: str):
    st = studio_of(ctx)
    try:
        b = st.registry.get(backend_id)
    except KeyError:
        raise StageError(f"backend {backend_id} not registered", blocker=f"install {what}")
    info = st.registry.info(backend_id)
    if info.availability == Availability.MISSING:
        detail = "; ".join(t.detail for t in info.tools if t.availability == Availability.MISSING)
        raise StageError(f"{what} is not available: {detail}", blocker=f"install {what} (Settings → Dependencies)")
    return b


def _result(res, what: str) -> dict[str, Any]:
    if not res.ok:
        raise StageError(f"{what} failed: {res.error}")
    return {"data": res.data, "evidence_ids": res.evidence_ids, "truncated": res.truncated}


def stage_analyze_module(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    mod = st.cases.get_module(ctx.job.inputs["module_id"])
    case = st.cases.get_case(ctx.job.case_id)
    path = Path(case["source_root"]) / mod["rel_path"]
    b = _backend(ctx, "rizin", "Rizin")
    out: dict[str, Any] = {"module_id": mod["module_id"], "evidence_ids": []}
    for op in ("info", "imports", "exports", "strings", "analyze", "functions"):
        res = b.call(op, ctx, case_id=case["case_id"], module_id=mod["module_id"])
        r = _result(res, f"rizin {op}")
        out["evidence_ids"] += r["evidence_ids"]
        out[op] = {k: v for k, v in r["data"].items() if k in ("count", "functions", "decompiler", "rizin_version", "truncated", "arch", "bits")}
        ctx.progress(module=mod["rel_path"], op=op)
    funcs = res.data.get("functions") or []
    out["function_count"] = len(funcs) if isinstance(funcs, list) else res.data.get("count")
    # decompile a bounded set of functions as evidence (all functions are available on demand via briefing)
    decompiled, errors = 0, 0
    names = [f.get("name") for f in funcs if isinstance(f, dict) and f.get("name")] if isinstance(funcs, list) else []
    limit = int(case.get("settings", {}).get("decompile_limit", 200))
    for i, name in enumerate(names[:limit]):
        try:
            r = b.call("decompile", ctx, case_id=case["case_id"], module_id=mod["module_id"], function=name)
            if r.ok:
                decompiled += 1; out["evidence_ids"] += r.evidence_ids
            else:
                errors += 1
        except Exception:
            errors += 1
        if i % 10 == 0:
            ctx.progress(module=mod["rel_path"], functions_decompiled=decompiled, functions_total=len(names), decompile_errors=errors)
    out.update({"decompiled": decompiled, "decompile_errors": errors, "decompile_limit": limit, "functions_total": len(names)})
    ev = st.cases.add_evidence(case["case_id"], "module_report", f"Native analysis: {mod['rel_path']}", body=out, module_id=mod["module_id"],
                               inputs={"module_sha": mod["sha256"], "ops": "info,imports,exports,strings,analyze,functions,decompile", "limit": limit}, producer="rizin")
    st.plan.add_evidence(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), ev["evidence_id"])
    return out


def stage_recover_managed(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    mod = st.cases.get_module(ctx.job.inputs["module_id"])
    case = st.cases.get_case(ctx.job.case_id)
    path = Path(case["source_root"]) / mod["rel_path"]
    b = _backend(ctx, "ilspy", "ILSpy (ilspycmd)")
    out_dir = st.cases.case_root(case["case_id"]) / "recovered" / mod["module_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = _result(b.call("metadata", ctx, case_id=case["case_id"], module_id=mod["module_id"], module_path=str(path)), "ilspy metadata")
    dec = _result(b.call("decompile", ctx, case_id=case["case_id"], module_id=mod["module_id"], module_path=str(path), out_dir=str(out_dir)), "ilspy decompile")
    out = {"module_id": mod["module_id"], "out_dir": str(out_dir), "metadata": meta["data"], "decompile": dec["data"], "evidence_ids": meta["evidence_ids"] + dec["evidence_ids"]}
    ev = st.cases.add_evidence(case["case_id"], "module_report", f"Managed recovery: {mod['rel_path']}", body=out, module_id=mod["module_id"],
                               inputs={"module_sha": mod["sha256"], "backend": "ilspy"}, producer="ilspy")
    st.plan.add_evidence(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), ev["evidence_id"])
    return out


def stage_recover_engine(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    mod = st.cases.get_module(ctx.job.inputs["module_id"])
    case = st.cases.get_case(ctx.job.case_id)
    path = Path(case["source_root"]) / mod["rel_path"]
    b = _backend(ctx, "gdre", "GDRE tools")
    out_dir = st.cases.case_root(case["case_id"]) / "recovered" / mod["module_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    det = _result(b.call("detect", ctx, case_id=case["case_id"], module_id=mod["module_id"], path=str(path)), "gdre detect")
    rec = _result(b.call("recover", ctx, case_id=case["case_id"], module_id=mod["module_id"], pck=str(path), out_dir=str(out_dir)), "gdre recover")
    out = {"module_id": mod["module_id"], "out_dir": str(out_dir), "detect": det["data"], "recover": rec["data"], "evidence_ids": det["evidence_ids"] + rec["evidence_ids"]}
    ev = st.cases.add_evidence(case["case_id"], "module_report", f"Engine recovery: {mod['rel_path']}", body=out, module_id=mod["module_id"],
                               inputs={"module_sha": mod["sha256"], "backend": "gdre"}, producer="gdre")
    st.plan.add_evidence(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), ev["evidence_id"])
    return out


def stage_recover_web(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    root = Path(case["source_root"])
    b = _backend(ctx, "jsweb", "JS/web extractor")
    out_dir = st.cases.case_root(case["case_id"]) / "recovered" / "web"
    out_dir.mkdir(parents=True, exist_ok=True)
    mid = ctx.job.inputs.get("module_id")
    ins = _result(b.call("inspect", ctx, case_id=case["case_id"], module_id=mid, root=str(root)), "web inspect")
    ext = _result(b.call("extract", ctx, case_id=case["case_id"], module_id=mid, root=str(root), out_dir=str(out_dir)), "web extract")
    out = {"out_dir": str(out_dir), "inspect": ins["data"], "extract": ext["data"], "evidence_ids": ins["evidence_ids"] + ext["evidence_ids"]}
    ev = st.cases.add_evidence(case["case_id"], "module_report", "Web/JS recovery", body=out, module_id=mid, inputs={"root": str(root), "backend": "jsweb"}, producer="jsweb")
    st.plan.add_evidence(st.plan.milestone_id(case["case_id"], "M-RECOVERY"), ev["evidence_id"])
    return out


# ------------------------------------------------------------------ features & baseline
def stage_discover_features(ctx: StageContext) -> dict[str, Any]:
    """Static hypotheses + declared scenarios → feature ledger. Runtime evidence (capture) upgrades origin later."""
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    lp = case.get("launch_profile", {})
    added = []
    scenarios = list(lp.get("scenarios", []))
    if lp.get("baseline_file"):
        from .fixture_oracle import load_baseline_file
        try:
            for sc in load_baseline_file(Path(lp["baseline_file"]), Path(case["source_root"])).get("scenarios", []):
                if sc.get("feature_id") and sc["feature_id"] not in {x.get("feature_id") for x in scenarios}:
                    scenarios.append({"id": sc["id"], "feature_id": sc["feature_id"], "title": sc.get("title", sc["id"]), "description": "declared by frozen oracle"})
        except Exception as e:
            ctx.log(f"baseline file could not be read for feature discovery: {e}")
    for sc in scenarios:
        fid = sc.get("feature_id") or f"scenario.{sc['id']}"
        f = st.ledger.add(case["case_id"], sc.get("title", fid), description=sc.get("description", ""), origin="user", critical=bool(sc.get("critical")),
                          impl_status="planned", feature_id=fid)
        added.append(f["feature_id"])
        st.plan.add_item(case["case_id"], title=f"Feature: {f['title']}", outcome=f"Behaviour of '{f['title']}' matches the original in declared channels", kind="feature",
                         parent_id=st.plan.milestone_id(case["case_id"], "M-IMPL"), feature_id=f["feature_id"], acceptance=[f"comparator passes for {f['feature_id']}"],
                         item_id=f"{case['case_id']}:F:{f['feature_id']}", reason="declared scenario")
    # static hypotheses from recovered evidence (exports, menus, strings): recorded as 'static' origin, unplanned until mapped
    hyps = 0
    for ev in st.cases.list_evidence(case["case_id"], kind="module_report"):
        body = st.cases.evidence_body(ev["evidence_id"]) or {}
        exports = (body.get("exports") or {}).get("count") if isinstance(body.get("exports"), dict) else None
        if exports:
            hyps += 1
            st.ledger.add(case["case_id"], f"Exported API surface of {ev['title']}", description=f"{exports} exports", origin="static", impl_status="unplanned",
                          feature_id=f"static.exports.{ev['module_id']}", evidence_ids=[ev["evidence_id"]])
    pwa = None
    for ev in st.cases.list_evidence(case["case_id"], kind="module_report"):
        body = st.cases.evidence_body(ev["evidence_id"]) or {}
        ins = (body.get("inspect") or {}) if isinstance(body, dict) else {}
        if ins.get("service_worker") or ins.get("manifest"):
            pwa = st.ledger.add(case["case_id"], "Offline/PWA behaviour", description="service worker + web manifest detected", origin="static", impl_status="planned",
                                feature_id="static.pwa", evidence_ids=[ev["evidence_id"]])
            hyps += 1
    unknown = st.plan.milestone_id(case["case_id"], "D-UNKNOWN")
    if lp.get("execute_original"):
        st.plan.update_item(unknown, status="completed", blockers=[], outcome="Static discovery and authorized runtime observation finished for declared scenarios; undeclared behaviour remains unknown")
    else:
        st.plan.update_item(unknown, status="blocked", blockers=["runtime observation not authorized: enable 'execute original' in the launch configuration"])
    st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-FEATURES"), status="completed")
    st.plan.revise(case["case_id"], f"feature discovery: {len(added)} declared features, {hyps} static hypotheses")
    return {"declared": added, "static_hypotheses": hyps, "pwa": bool(pwa)}


def stage_capture_original(ctx: StageContext) -> dict[str, Any]:
    """Run declared scenarios against the ORIGINAL (authorized by launch profile) and freeze the baseline. Or freeze a fixture oracle."""
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    lp = case.get("launch_profile", {})
    if lp.get("baseline_file"):
        from .fixture_oracle import load_baseline_file
        bl = load_baseline_file(Path(lp["baseline_file"]), Path(case["source_root"]))
        bl = _import_screenshots(st, case["case_id"], bl, Path(lp["baseline_file"]).parent)
        ev = st.verifier.freeze_baseline(case["case_id"], bl, producer="fixture_oracle", title="Fixture oracle baseline")
        return {"evidence_id": ev["evidence_id"], "source": "fixture_oracle", "scenarios": len(bl.get("scenarios", []))}
    if not lp.get("execute_original"):
        raise StageError("original execution not authorized", blocker="enable 'execute original' in launch configuration to capture a baseline")
    launch = lp.get("launch")
    if not launch:
        raise StageError("launch profile has no launch spec", blocker="configure how the original is started")
    root = Path(case["source_root"])
    work_root = st.cases.case_root(case["case_id"]) / "capture"
    if work_root.exists():
        shutil.rmtree(work_root)
    bl: dict[str, Any] = {"kind": lp.get("kind", "cli"), "launch": launch, "scenarios": [], "tolerance": lp.get("tolerance", {})}
    scenarios = lp.get("scenarios", [])
    if bl["kind"] == "web":
        from .comparators.web import run_web_scenario
        from .previews import _free_port, _QuietHandler
        import http.server, threading
        from functools import partial
        site = root / launch.get("root", ".")
        port = _free_port()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(_QuietHandler, directory=str(site)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for i, sc in enumerate(scenarios):
                rec = run_web_scenario(f"http://127.0.0.1:{port}/{launch.get('entry', 'index.html')}", sc, work_root / sc["id"])
                if rec.get("screenshot"):
                    rec["screenshot_sha"] = st.cases.blobs.put_file(Path(rec["screenshot"]))
                bl["scenarios"].append({**sc, "expected": rec})
                ctx.progress(scenarios_done=i + 1, scenarios_total=len(scenarios))
        finally:
            srv.shutdown(); srv.server_close()
    else:
        from .comparators.cli import run_steps, snapshot_work
        for i, sc in enumerate(scenarios):
            w = work_root / sc["id"]
            runs = run_steps(launch, root, sc["steps"], w, timeout=float(sc.get("timeout", 60)), setup_files=sc.get("setup_files"))
            bl["scenarios"].append({**sc, "expected": {"steps": runs, "files": snapshot_work(w)}})
            ctx.progress(scenarios_done=i + 1, scenarios_total=len(scenarios))
    ev = st.verifier.freeze_baseline(case["case_id"], bl, producer="capture_original", title="Captured original baseline")
    for sc in scenarios:
        fid = sc.get("feature_id") or f"scenario.{sc['id']}"
        try:
            f = st.ledger.get(fid)
            st.ledger.set_impl(fid, f["impl_status"], evidence_ids=[ev["evidence_id"]])
            st.db.update("features", "feature_id", fid, {"origin": "runtime"})
        except KeyError:
            pass
    return {"evidence_id": ev["evidence_id"], "source": "capture_original", "scenarios": len(scenarios)}


def _import_screenshots(st, case_id: str, bl: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    for sc in bl.get("scenarios", []):
        exp = sc.get("expected", {})
        shot = exp.get("screenshot")
        if shot and not Path(shot).is_absolute():
            p = base_dir / shot
            if p.exists():
                exp["screenshot_sha"] = st.cases.blobs.put_file(p)
                exp["screenshot"] = str(p)
    return bl


# ------------------------------------------------------------------ reconstruction, build, compare, repair
def stage_reconstruct(ctx: StageContext) -> dict[str, Any]:
    from .reconstruct import reconstruct
    return reconstruct(ctx)


def stage_build_candidate(ctx: StageContext) -> dict[str, Any]:
    from .builders import build
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    cid = ctx.job.inputs["candidate_id"]
    cand = st.candidates.get(cid)
    st.candidates.mark_building(cid)
    src = Path(cand["source_dir"])
    staging = st.cases.case_root(case["case_id"]) / "candidates" / cid / "dist.staging"
    final = st.cases.case_root(case["case_id"]) / "candidates" / cid / "dist"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        info = build(ctx, cand["target_language"], src, staging)
    except StageError as e:
        st.candidates.mark_failed(cid, str(e))
        raise
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)  # atomic publication of the candidate dist
    log_ev = st.cases.add_evidence(case["case_id"], "build_log", f"Build log {cid}", body_bytes=(info.get("build_log") or "").encode("utf-8"),
                                   inputs={"candidate": cid}, producer="builder") if info.get("build_log") else None
    c = st.candidates.mark_built(cid, final, build_log_evidence=log_ev["evidence_id"] if log_ev else None)
    st.db.update("candidates", "candidate_id", cid, {"meta": {**cand["meta"], "launch": info["launch"], "build": {k: v for k, v in info.items() if k != "build_log"}}})
    st.ledger.mark_stale(case["case_id"], f"new candidate {cid} built")
    # publish a real preview for the new candidate
    launch = info["launch"]
    if launch["type"] == "web":
        plaunch = {"type": "browser", "root": str(final / launch.get("root", ".")), "entry": launch.get("entry", "index.html")}
    else:
        plaunch = {"type": "native", "command": [str(final / launch["path"])], "cwd": str(final)}
    feats = st.ledger.list(case["case_id"])
    st.previews.publish(case["case_id"], cid, kind="real", title=f"Candidate r{c['revision']} ({cand['target_language']})", launch=plaunch,
                        available=[f["title"] for f in feats if f["impl_status"] in ("runnable", "in_progress")] or ["runnable build"],
                        incomplete=[f["title"] for f in feats if f["impl_status"] in ("blocked", "unsupported", "planned")],
                        requirements=["Windows 10/11 x64" if launch["type"] == "exe" else "any modern browser"],
                        steps=[f"Try: {f['title']}" for f in feats[:5]], plan_revision=st.plan.current_revision(case["case_id"]))
    st.feedback.mark_stale_for_candidate(case["case_id"], cid)
    st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-BUILD"), status="completed", files=[str(final)])
    return {"candidate_id": cid, "dist": str(final), "build_hash": c["build_hash"], "launch": launch, "pwa": info.get("pwa")}


def stage_compare_candidate(ctx: StageContext) -> dict[str, Any]:
    st = studio_of(ctx)
    case = st.cases.get_case(ctx.job.case_id)
    cid = ctx.job.inputs["candidate_id"]
    rep = st.verifier.verify_candidate(case["case_id"], cid, feature_ids=ctx.job.inputs.get("feature_ids") or None, progress=lambda p: ctx.progress(**p))
    st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-COMPARE"), status="completed" if rep["summary"]["errors"] == 0 else "failed",
                        evidence_ids=[rep["evidence_id"]])
    for fid, verdict in rep["feature_verdicts"].items():
        iid = f"{case['case_id']}:F:{fid}"
        try:
            st.plan.update_item(iid, status="completed" if verdict == "verified" else "failed", evidence_ids=[rep["evidence_id"]])
        except KeyError:
            pass
    failed = rep["summary"]["failed"] + rep["summary"]["errors"]
    if failed:
        policy = case.get("ai_policy", {})
        attempts = int(ctx.job.inputs.get("repair_attempt", 0))
        if policy.get("mode") in ("assist_on_failure", "assisted") and attempts < int(policy.get("max_repairs", 3)):
            j = st.jobs.create(case["case_id"], "repair", f"Repair candidate (attempt {attempts + 1})", {"candidate_id": cid, "repair_attempt": attempts + 1, "report": rep["evidence_id"]},
                               depends_on=[ctx.job.job_id], milestone_id="M-FIX")
            st.plan.link_job(st.plan.milestone_id(case["case_id"], "M-FIX"), j.job_id)
        else:
            st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-FIX"), status="blocked",
                                blockers=[f"{failed} scenario(s) failed; AI policy '{policy.get('mode', 'no_ai')}' does not allow automatic repair or attempts exhausted. Review Comparisons or enable AI assistance."])
    else:
        st.plan.update_item(st.plan.milestone_id(case["case_id"], "M-FIX"), status="completed")
    return {"candidate_id": cid, "summary": rep["summary"], "feature_verdicts": rep["feature_verdicts"], "evidence_id": rep["evidence_id"]}


def stage_repair(ctx: StageContext) -> dict[str, Any]:
    from .reconstruct import repair
    return repair(ctx)


# ------------------------------------------------------------------ delivery
def stage_deliver(ctx: StageContext) -> dict[str, Any]:
    from .export.deliver import deliver
    return deliver(ctx)
