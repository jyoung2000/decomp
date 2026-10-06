#!/usr/bin/env python3
"""Generate expected/resources.json for godotgame.

Everything is measured, nothing typed by hand:
  * `gdre`    : real GDRE tools 2.7.0 run (--list-files, --recover) against original/game.pck;
                the recovered tree is diffed byte-for-byte against src/ (minus build tooling).
  * `static`  : facts parsed from src/ (ground truth) with simple regexes: nodes, resources, functions,
                input actions, autoloads, wav header, sha256 of every shipped script/scene/asset.
  * `runtime` : the Godot engine binary is not available, so gameplay behaviours cannot be executed here;
                they are listed with observable_on = "godot_engine_only" and no expected values.

usage: gen_expected.py [--gdre PATH]   (run from anywhere)
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, wave

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.dirname(HERE)
SRC = os.path.join(FX, "src")
PCK = os.path.join(FX, "original", "game.pck")
GDRE = "/opt/rebuild-tools/gdre/gdre_tools.x86_64"
TOOLING = {"pack_pck.py", "make_wav.py"}
if "--gdre" in sys.argv:
    GDRE = sys.argv[sys.argv.index("--gdre") + 1]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def game_files():
    out = []
    for dp, _, fns in os.walk(SRC):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            if rel in TOOLING or "__pycache__" in rel:
                continue
            out.append(rel)
    return sorted(out)


def run_gdre(args):
    env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="gdre_home_"))
    cp = subprocess.run([GDRE, "--headless"] + args, capture_output=True, text=True, env=env, timeout=300)
    shutil.rmtree(env["HOME"], ignore_errors=True)
    return cp.returncode, cp.stdout + cp.stderr


def gdre_section():
    rc, out = run_gdre([f"--list-files={PCK}"])
    listed = sorted(l.strip() for l in out.splitlines() if l.strip().startswith("res://"))
    outdir = tempfile.mkdtemp(prefix="gdre_rec_")
    shutil.rmtree(outdir)
    rc2, out2 = run_gdre([f"--recover={PCK}", f"--output={outdir}"])
    recovered = {}
    for dp, _, fns in os.walk(outdir):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), outdir).replace(os.sep, "/")
            if rel != "gdre_export.log":
                recovered[rel] = sha(os.path.join(dp, fn))
    src_hashes = {r: sha(os.path.join(SRC, r)) for r in game_files()}
    diff = {
        "missing_from_recovery": sorted(set(src_hashes) - set(recovered)),
        "unexpected_in_recovery": sorted(set(recovered) - set(src_hashes)),
        "content_mismatch": sorted(r for r in src_hashes if r in recovered and recovered[r] != src_hashes[r]),
    }
    shutil.rmtree(outdir, ignore_errors=True)
    m = re.search(r"Detected Engine Version: (\S+)", out2)
    ver = re.search(r"Verified (\d+) files, (no errors detected|[^\n]*)", out2)
    ext = re.search(r"Extracted (\d+) files, (no errors detected|[^\n]*)", out2)
    return {
        "tool": "GDRE tools 2.7.0 (gdre_tools.x86_64 --headless)",
        "list_files_exit": rc, "listed": listed,
        "recover_exit": rc2,
        "detected_engine_version": m.group(1) if m else None,
        "md5_verified_files": int(ver.group(1)) if ver else None,
        "md5_verify_result": ver.group(2).strip() if ver else None,
        "extracted_files": int(ext.group(1)) if ext else None,
        "extract_result": ext.group(2).strip() if ext else None,
        "warnings": sorted(set(re.findall(r"^(?:WARNING|ERROR): (.*)$", out2, re.M))),
        "recovered_sha256": dict(sorted(recovered.items())),
        "diff_vs_src": diff,
        "recovered_identical_to_src": not any(diff.values()),
    }


def parse_tscn(rel):
    t = open(os.path.join(SRC, rel)).read()
    return {
        "format": int(re.search(r"format=(\d+)", t).group(1)),
        "uid": (re.search(r'^\[gd_scene[^\]]*uid="([^"]+)"', t, re.M) or [None, None])[1],
        "ext_resources": [dict(re.findall(r'(\w+)="([^"]*)"', m)) for m in re.findall(r"^\[ext_resource ([^\]]*)\]", t, re.M)],
        "sub_resources": [dict(re.findall(r'(\w+)="([^"]*)"', m)) for m in re.findall(r"^\[sub_resource ([^\]]*)\]", t, re.M)],
        "nodes": [dict(re.findall(r'(\w+)="([^"]*)"', m)) for m in re.findall(r"^\[node ([^\]]*)\]", t, re.M)],
    }


def parse_gd(rel):
    t = open(os.path.join(SRC, rel)).read()
    return {
        "extends": (re.search(r"^extends (\S+)", t, re.M) or [None, None])[1],
        "signals": re.findall(r"^signal (\w+)", t, re.M),
        "consts": re.findall(r"^const (\w+)", t, re.M),
        "exports": re.findall(r"^@export var (\w+)", t, re.M),
        "functions": re.findall(r"^func (\w+)", t, re.M),
        "input_actions_used": sorted(set(re.findall(r'"((?:move|collect)\w*)"', t))),
        "user_paths": sorted(set(re.findall(r'"(user://[^"]+)"', t))),
        "line_count": len(t.splitlines()),
    }


def static_section():
    files = game_files()
    pg = open(os.path.join(SRC, "project.godot")).read()
    inp = re.search(r"^\[input\]\n(.*?)(?=^\[|\Z)", pg, re.M | re.S).group(1)
    w = wave.open(os.path.join(SRC, "audio", "beep.wav"))
    return {
        "files": {r: {"size": os.path.getsize(os.path.join(SRC, r)), "sha256": sha(os.path.join(SRC, r))} for r in files},
        "scripts_sha256": {r: sha(os.path.join(SRC, r)) for r in files if r.endswith(".gd")},
        "project": {
            "name": re.search(r'config/name="([^"]*)"', pg).group(1),
            "main_scene": re.search(r'run/main_scene="([^"]*)"', pg).group(1),
            "autoloads": dict(re.findall(r'^(\w+)="\*?(res://[^"]+)"', re.search(r"^\[autoload\]\n(.*?)(?=^\[|\Z)", pg, re.M | re.S).group(1), re.M)),
            "input_actions": sorted(re.findall(r"^(\w+)=\{", inp, re.M)),
            "viewport": [int(re.search(r"viewport_width=(\d+)", pg).group(1)), int(re.search(r"viewport_height=(\d+)", pg).group(1))],
        },
        "scenes": {r: parse_tscn(r) for r in files if r.endswith(".tscn")},
        "scripts": {r: parse_gd(r) for r in files if r.endswith(".gd")},
        "audio": {"audio/beep.wav": {"channels": w.getnchannels(), "sample_width_bytes": w.getsampwidth(),
                                      "rate": w.getframerate(), "frames": w.getnframes()}},
    }


FEATURES = [
    ("godotgame.pck_recover", "Recover all 7 entries from game.pck with byte-identical content", "gdre", "linux_via_gdre"),
    ("godotgame.engine_version_detect", "Detect engine version 4.3.0 / pack format 2 from the PCK header", "gdre", "linux_via_gdre"),
    ("godotgame.scripts_recover", "Recover the 3 GDScript sources (sha256 listed)", "static", "linux_via_gdre"),
    ("godotgame.scenes_recover", "Recover 2 text scenes with node trees and resource links", "static", "linux_via_gdre"),
    ("godotgame.audio_asset", "Recover beep.wav (16-bit mono 22050 Hz)", "static", "linux_via_gdre"),
    ("godotgame.project_settings", "Recover project.godot: main scene, autoload, input map", "static", "linux_via_gdre"),
    ("godotgame.player_movement", "Player moves with move_* actions at speed 200", "runtime", "godot_engine_only"),
    ("godotgame.save_system", "SaveSystem writes/reads user://save.json (score, player_x, player_y)", "runtime", "godot_engine_only"),
    ("godotgame.audio_player", "AudioPlayer node plays beep.wav on collect", "runtime", "godot_engine_only"),
    ("godotgame.score_label", "ScoreLabel shows 'Score: N' and updates on collect", "runtime", "godot_engine_only"),
]


def main():
    out = {
        "fixture": "godotgame",
        "pck": {"path": "original/game.pck", "size": os.path.getsize(PCK), "sha256": sha(PCK)},
        "gdre": gdre_section(),
        "static": static_section(),
        "runtime": {
            "executable_here": False,
            "reason": "no Godot engine binary is installed or redistributed; GDRE tools embed an editor-only 4.8-dev build that cannot run the project's game loop headlessly in this harness",
            "note": "runtime features carry no expected values; they must be exercised on a machine with Godot 4.3 or via a Windows/Linux engine in CI",
        },
        "features": [{"id": i, "description": d, "evidence": e, "observable_on": o} for i, d, e, o in FEATURES],
    }
    p = os.path.join(FX, "expected", "resources.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")
    g = out["gdre"]
    print(f"gdre: version={g['detected_engine_version']} listed={len(g['listed'])} verified={g['md5_verified_files']} "
          f"extracted={g['extracted_files']} identical_to_src={g['recovered_identical_to_src']}")
    print("wrote", p)
    return 0 if g["recovered_identical_to_src"] and g["recover_exit"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
