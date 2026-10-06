#!/usr/bin/env python3
"""Generic scenario runner for CLI fixtures (pecli, dotnetapp).

Modes
  generate  run harness/scenarios.json against the ORIGINAL (via launcher from harness/config.json)
            and write expected/scenarios.json (frozen oracle).
  check     run the same scenarios against a candidate launcher and compare to expected/scenarios.json.
            Exit status 0 only if every scenario matches; mismatches are listed.

Scenario schema (harness/scenarios.json -> list):
  id, feature, description,
  files: {name: {"from_original": relpath} | {"text": str} | {"hex": str}}   (workdir setup)
  steps: [{"args": [..], "stdin": str|null}]
Placeholders in args: none; paths are relative to the scenario's fresh working directory.

Recorded per step: exit_code, stdout (raw bytes decoded utf-8, as emitted), stdout_normalized (CRLF->LF),
stderr, stderr_normalized.  Recorded per scenario: final_files {name: {size, sha256}} for every
file present after the last step (the workdir only contains setup + produced files).
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def norm(s):
    return s.replace("\r\n", "\n")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def setup(workdir, files, orig_dir):
    for name, spec in (files or {}).items():
        dst = os.path.join(workdir, name)
        os.makedirs(os.path.dirname(dst) or workdir, exist_ok=True)
        if "from_original" in spec:
            shutil.copyfile(os.path.join(orig_dir, spec["from_original"]), dst)
        elif "text" in spec:
            with open(dst, "w", newline="") as f:
                f.write(spec["text"])
        elif "hex" in spec:
            with open(dst, "wb") as f:
                f.write(bytes.fromhex(spec["hex"]))
        if "flip_byte" in spec:
            with open(dst, "r+b") as f:
                f.seek(spec["flip_byte"])
                b = f.read(1)
                f.seek(spec["flip_byte"])
                f.write(bytes([b[0] ^ 0xFF]))


def run_scenario(sc, launcher, env, orig_dir, timeout=120):
    wd = tempfile.mkdtemp(prefix="scn_")
    try:
        setup(wd, sc.get("files"), orig_dir)
        before = {n: sha(os.path.join(wd, n)) for n in os.listdir(wd) if os.path.isfile(os.path.join(wd, n))}
        steps = []
        for st in sc["steps"]:
            cp = subprocess.run(launcher + st["args"], cwd=wd, env=env, input=(st.get("stdin") or "").encode(),
                                capture_output=True, timeout=timeout)
            out, err = cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")
            steps.append({"args": st["args"], "stdin": st.get("stdin"), "exit_code": cp.returncode,
                          "stdout": out, "stdout_normalized": norm(out),
                          "stderr": err, "stderr_normalized": norm(err)})
        files = {}
        for dp, _, fns in os.walk(wd):
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, wd).replace(os.sep, "/")
                files[rel] = {"size": os.path.getsize(p), "sha256": sha(p),
                              "changed_or_new": before.get(rel) != sha(p)}
                raw = open(p, "rb").read()
                if len(raw) <= 4096:
                    try:
                        files[rel]["text"] = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        files[rel]["hex"] = raw.hex()
        return steps, dict(sorted(files.items()))
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["generate", "check"])
    ap.add_argument("--fixture", required=True, help="fixture dir")
    ap.add_argument("--launcher", help="candidate launcher command (shell-split); required for check")
    ap.add_argument("--original-dir", help="dir with shipped files for from_original setup (default fixture/original)")
    ap.add_argument("--only", help="comma-separated scenario ids")
    a = ap.parse_args()
    fx = os.path.abspath(a.fixture)
    cfg = json.load(open(os.path.join(fx, "harness", "config.json")))
    scenarios = json.load(open(os.path.join(fx, "harness", "scenarios.json")))
    if a.only:
        keep = set(a.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in keep]
    orig_dir = a.original_dir or os.path.join(fx, "original")
    env = dict(os.environ)
    env.update(cfg.get("env", {}))
    exp_path = os.path.join(fx, "expected", "scenarios.json")

    if a.mode == "generate":
        launcher = [x.replace("{ORIG}", orig_dir) for x in cfg["original_launcher"]]
        out = {"fixture": cfg["fixture"], "oracle": cfg["oracle"], "launcher": cfg["original_launcher"],
               "observable_on": "linux_via_" + cfg["oracle"]["runner"], "newline_policy": cfg.get("newline_policy"),
               "scenarios": []}
        for sc in scenarios:
            steps, files = run_scenario(sc, launcher, env, orig_dir)
            rec = {k: sc[k] for k in ("id", "feature", "description") if k in sc}
            rec["setup_files"] = sc.get("files", {})
            rec["steps"] = steps
            rec["final_files"] = files
            out["scenarios"].append(rec)
            print(f"[gen] {sc['id']}: exits={[s['exit_code'] for s in steps]} files={list(files)}")
        os.makedirs(os.path.dirname(exp_path), exist_ok=True)
        with open(exp_path, "w") as f:
            json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        print(f"wrote {exp_path} ({len(out['scenarios'])} scenarios)")
        return 0

    if not a.launcher:
        ap.error("check needs --launcher")
    import shlex
    launcher = shlex.split(a.launcher)
    exp = {s["id"]: s for s in json.load(open(exp_path))["scenarios"]}
    failures, passed = [], 0
    for sc in scenarios:
        e = exp[sc["id"]]
        steps, files = run_scenario(sc, launcher, env, orig_dir)
        bad = []
        for i, (g, w) in enumerate(zip(steps, e["steps"])):
            for k in ("exit_code", "stdout_normalized", "stderr_normalized"):
                if g[k] != w[k]:
                    bad.append(f"step{i} {k}: expected {w[k]!r} got {g[k]!r}")
        for name in sorted(set(files) | set(e["final_files"])):
            gf, wf = files.get(name), e["final_files"].get(name)
            if (gf or {}).get("sha256") != (wf or {}).get("sha256"):
                bad.append(f"file {name}: expected sha {(wf or {}).get('sha256')} got {(gf or {}).get('sha256')}")
        if bad:
            failures.append((sc["id"], bad))
        else:
            passed += 1
        print(f"[check] {sc['id']}: {'PASS' if not bad else 'FAIL'}")
    print(f"summary: {passed} passed, {len(failures)} failed, {len(scenarios)} total")
    for sid, bad in failures:
        for b in bad:
            print(f"  FAIL {sid}: {b}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
