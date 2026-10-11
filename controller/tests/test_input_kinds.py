"""R7: more input kinds, each with a fixture we own.

* Mach-O: the committed bench rows go_macho_x64 / go_macho_arm64 (Go cross-compiled for darwin, never executed here) and go_macho_fat
  (assembled by fixtures/bench/tools/macho_fat.py, independent of the product's parser). Thin files go straight to rizin; a fat file is
  analysed slice by slice, each slice a module of its own.
* Android native libraries: bench row go_apk_arm64 (an APK built with build-tools 34 whose lib/arm64-v8a/libbenchgo.so is our Go
  program for android/arm64). Each lib/<abi>/*.so becomes its own native module next to the APK's dex/jadx path.
* Unity IL2CPP, GameMaker, Unreal stay detected-only; the support records say exactly what is missing.

Tool-free tests always run; the pipeline test needs rizin (REBUILD_STUDIO_TOOLS) and is skipped without it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from support.data_dirs import tool_settings  # noqa: E402  (R11: never the real data folder)
from rebuild_controller.backends import members
from rebuild_controller.backends import support as sp
from rebuild_controller.backends.detect import detect_path, sniff
from rebuild_controller.backends.inventory import inventory_root
from rebuild_controller.config import Limits

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "fixtures" / "bench"
FAT = BENCH / "go_macho_fat" / "bin" / "benchgo_universal"
THIN_X64 = BENCH / "go_macho_x64" / "bin" / "benchgo"
THIN_ARM64 = BENCH / "go_macho_arm64" / "bin" / "benchgo"
APK = BENCH / "go_apk_arm64" / "bin" / "benchapk.apk"
APK_LIB = "lib/arm64-v8a/libbenchgo.so"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _truth(row: str) -> dict:
    return json.loads((BENCH / row / "truth" / "truth.json").read_text(encoding="utf-8"))


def _fat_writer():
    spec = importlib.util.spec_from_file_location("macho_fat", BENCH / "tools" / "macho_fat.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macho_fat"] = mod
    spec.loader.exec_module(mod)
    return mod


def _elf_header(machine: int) -> bytes:
    """64-bit little-endian ELF header, ET_DYN (what a shared library has)."""
    return b"\x7fELF\x02\x01\x01" + b"\0" * 9 + struct.pack("<HHI", 3, machine, 1) + b"\0" * 40


def _apk(path: Path, libs: dict[str, bytes], *, deflate: bool = False) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00" + b"\0" * 60)
        z.writestr("classes.dex", b"dex\n035\0" + b"\0" * 100)
        for name, data in libs.items():
            z.writestr(name, data)
    return path


# ------------------------------------------------------------------------------------------------ fixtures we own
def test_bench_rows_for_the_new_kinds_exist_and_are_what_they_claim():
    man = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    for row in ("go_macho_x64", "go_macho_arm64", "go_macho_fat", "go_apk_arm64"):
        r = man["rows"][row]
        assert _sha((BENCH / r["binary"]).read_bytes()) == r["sha256"], row
    assert sniff(THIN_X64).arch == "x86_64" and sniff(THIN_ARM64).arch == "arm64"
    d = sniff(FAT)
    assert d.format == "macho" and d.flags["layout"] == "fat" and d.arch == "universal:arm64+x86_64"
    a = sniff(APK)
    assert (a.format, a.profile) == ("apk", "android") and a.flags["abis"] == ["arm64-v8a"] and a.flags["dex_files"] == 1
    t = _truth("go_apk_arm64")
    assert t["member"]["path"] == APK_LIB and t["machine"] == "aarch64" and t["counts"]["own_functions"] == 9


def test_fat_slices_extracted_by_the_product_equal_the_thin_rows():
    specs = members.fat_slices(FAT)
    assert [s["member"] for s in specs] == ["slice/x86_64", "slice/arm64"]
    got = {s["arch"]: members.read_member(FAT, s) for s in specs}
    assert _sha(got["x86_64"]) == _sha(THIN_X64.read_bytes()) and _sha(got["arm64"]) == _sha(THIN_ARM64.read_bytes())
    truth = {s["arch"]: s["sha256"] for s in _truth("go_macho_fat")["slices"]}
    assert truth == {k: _sha(v) for k, v in got.items()}
    # the fixture writer and the product parser agree on alignment as lipo lays it out (2^12 for x86_64, 2^14 for arm64)
    assert specs[0]["offset"] % 4096 == 0 and specs[1]["offset"] % 16384 == 0


def test_fat_writer_round_trip_and_refusals(tmp_path):
    mf = _fat_writer()
    x = b"\xcf\xfa\xed\xfe" + struct.pack("<iiIIII", 0x01000007, 3, 2, 0, 0, 0) + b"\0" * 4 + b"X" * 100
    a = b"\xcf\xfa\xed\xfe" + struct.pack("<iiIIII", 0x0100000C, 0, 2, 0, 0, 0) + b"\0" * 4 + b"A" * 50
    fat = tmp_path / "u"
    fat.write_bytes(mf.build_fat([x, a]))
    specs = members.fat_slices(fat)
    assert [members.read_member(fat, s) for s in specs] == [x, a]
    with pytest.raises(ValueError):
        mf.build_fat([x, x])                       # two slices of one architecture
    with pytest.raises(members.MemberError):
        members.fat_slices(THIN_X64)               # a thin file has no slices
    bad = dict(specs[0], offset=specs[0]["offset"] + 1)
    with pytest.raises(members.MemberError):
        members.read_member(fat, bad)              # a range that is not a Mach-O header is refused


def test_apk_native_libraries_are_listed_per_abi_with_guards(tmp_path):
    p = _apk(tmp_path / "two.apk", {"lib/arm64-v8a/libgame.so": _elf_header(0xB7) + b"a" * 64,
                                     "lib/x86_64/libgame.so": _elf_header(0x3E) + b"x" * 64,
                                     "lib/arm64-v8a/readme.txt": b"not a library",
                                     "assets/lib/arm64-v8a/libfake.so": b"not under lib/"})
    libs = members.apk_native_libs(p)
    assert [(m["abi"], m["name"]) for m in libs] == [("arm64-v8a", "libgame.so"), ("x86_64", "libgame.so")]
    det = {m["abi"]: members.describe(p, m)["detect"] for m in libs}
    assert det["arm64-v8a"]["profile"] == det["x86_64"]["profile"] == "native_elf"
    assert (det["arm64-v8a"]["arch"], det["x86_64"]["arch"]) == ("arm64", "x86_64")
    assert det["arm64-v8a"]["flags"]["android_abi"] == "arm64-v8a" and det["arm64-v8a"]["flags"]["type"] == "dyn"
    bomb = _apk(tmp_path / "bomb.apk", {"lib/arm64-v8a/libz.so": _elf_header(0xB7) + b"\0" * (4 << 20)}, deflate=True)
    rec = members.apk_native_libs(bomb)[0]
    assert "zip bomb" in rec["skip"]
    with pytest.raises(members.MemberError):
        members.read_member(bomb, rec)


def test_materialize_copies_once_into_the_work_folder_and_rechecks_the_hash(tmp_path):
    src = _apk(tmp_path / "a.apk", {"lib/arm64-v8a/liba.so": _elf_header(0xB7) + b"1" * 32})
    spec = members.apk_native_libs(src)[0]
    want = members.describe(src, spec)["sha256"]
    work = tmp_path / "work"
    out = members.materialize(src, spec, work, sha256=want)
    assert out.parent == work and out.name == "liba.so" and _sha(out.read_bytes()) == want
    _apk(src, {"lib/arm64-v8a/liba.so": _elf_header(0xB7) + b"2" * 32})      # the user's container changed afterwards
    out.unlink()
    with pytest.raises(members.MemberError, match="changed inside its container"):
        members.materialize(src, spec, work, sha256=want)


def test_inventory_lists_members_of_fat_and_apk_containers(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy2(FAT, root / FAT.name)
    shutil.copy2(APK, root / APK.name)
    inv = inventory_root(root, Limits())
    assert sorted(inv["member_modules"]) == sorted([f"{APK.name}!/{APK_LIB}", f"{FAT.name}!/slice/arm64", f"{FAT.name}!/slice/x86_64"])
    by = {f["path"]: f for f in inv["files"]}
    apk_m = by[APK.name]["members"][0]
    assert apk_m["detect"]["profile"] == "native_elf" and apk_m["sha256"] == _truth("go_apk_arm64")["member"]["sha256"]
    fat_m = {m["spec"]["arch"]: m for m in by[FAT.name]["members"]}
    assert fat_m["arm64"]["detect"]["flags"]["slice_of_fat"] is True and fat_m["x86_64"]["detect"]["arch"] == "x86_64"
    assert all(m["detect"]["support_status"] == "supported" for m in by[FAT.name]["members"])


# ------------------------------------------------------------------------------------------------ honest statuses
def test_support_statuses_after_r7():
    mac = sp.support_for("native_macho")
    assert mac["status"] == "supported" and mac["backend"] == "rizin"
    assert "slice by slice" in mac["statement"] and "cannot run here" in mac["statement"] and "Swift" in mac["statement"]
    andr = sp.support_for("android")
    assert andr["status"] == "partial" and any("lib/<abi>/*.so" in c for c in andr["can_recover"])
    assert not any("listed, not decompiled" in c for c in andr["cannot_recover"])
    for prof, missing in (("unity_il2cpp", "no Unity editor"), ("gamemaker", "no GameMaker runner"), ("unreal", "no engine exists")):
        rec = sp.support_for(prof)
        assert rec["status"] == "detected_only" and missing in rec["blocker"], prof
        assert "third-party" in rec["blocker"] or "own script" in rec["blocker"], prof
    from rebuild_controller import stages
    assert stages.PROFILE_STAGE["native_macho"] == "analyze_module" and "native_macho" not in stages.UNSUPPORTED_PROFILES
    for prof in ("unity_il2cpp", "gamemaker", "unreal"):
        assert prof in stages.UNSUPPORTED_PROFILES and prof not in stages.PROFILE_STAGE


def test_needs_table_offers_rizin_for_android_native_libraries():
    docs = (REPO / "docs" / "dependency-needs.json").read_bytes()
    assert docs == (REPO / "controller" / "rebuild_controller" / "data" / "dependency-needs.json").read_bytes()
    needs = json.loads(docs)["profiles"]
    assert needs["native_macho"]["required"] == ["rizin"] and "rizin" in needs["android"]["optional"]


def test_detect_path_on_the_universal_fixture_carries_the_supported_statement():
    d = detect_path(FAT)
    assert d["support_status"] == "supported" and "Rizin" in d["support"]["statement"]


# ------------------------------------------------------------------------------------------------ pipeline (needs rizin)
def _rizin_available() -> bool:
    from rebuild_controller.backends.rizin_worker import find_rizin
    from rebuild_controller.config import Settings
    t = find_rizin(tool_settings())
    return t is not None and bool(t.version)


needs_rizin = pytest.mark.skipif(not _rizin_available(), reason="rizin not installed on this host (set REBUILD_STUDIO_TOOLS)")


@needs_rizin
def test_pipeline_analyses_each_fat_slice_and_each_apk_library_with_rizin(settings, tmp_path):
    from rebuild_controller.jobs import JobState
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 900
    settings.limits.lease_timeout_seconds = 300
    st = StudioServices(settings)
    try:
        src = tmp_path / "src"
        src.mkdir()
        shutil.copy2(FAT, src / FAT.name)
        shutil.copy2(APK, src / APK.name)
        before = {p.name: _sha(p.read_bytes()) for p in src.iterdir()}
        case = st.create_case(name="r7", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe",
                              ai_policy={"mode": "no_ai"}, settings={"decompile_limit": 3, "decompile_seconds": 120})
        cid = case["case_id"]
        inv_job = st.jobs.create(cid, "inventory", "inventory", {})
        assert st.runner.run_pending(max_jobs=1) == 1
        res = st.jobs.get(inv_job.job_id).result
        assert res["containers"] == {APK.name: 1, FAT.name: 2}
        mods = {m["rel_path"]: m for m in st.cases.modules(cid)}
        assert {f"{FAT.name}!/slice/x86_64", f"{FAT.name}!/slice/arm64", f"{APK.name}!/{APK_LIB}"} <= set(mods)
        analyze = {j.inputs["module_id"]: j for j in st.jobs.list(cid) if j.stage == "analyze_module"}
        by_rel = {mods[r]["module_id"]: r for r in mods}
        assert sorted(by_rel[m] for m in analyze) == sorted([f"{FAT.name}!/slice/arm64", f"{FAT.name}!/slice/x86_64", f"{APK.name}!/{APK_LIB}"])
        assert not any(u["module"] == FAT.name for u in res["unsupported"])        # the fat container is analysed per slice
        assert any(j.stage == "recover_jvm" for j in st.jobs.list(cid))           # the APK's dex path is unchanged
        for _ in range(20):
            if st.runner.run_pending() == 0:
                break
        thin = {"x86_64": _truth("go_macho_x64"), "arm64": _truth("go_macho_arm64")}
        for mid, job in analyze.items():
            j = st.jobs.get(job.job_id)
            assert j.state == JobState.COMPLETED, (by_rel[mid], j.error)
            rep = j.result
            rel = by_rel[mid]
            truth = _truth("go_apk_arm64") if rel.startswith(APK.name) else thin[rel.rsplit("/", 1)[-1]]
            own = {int(f["start"], 16) for f in truth["functions"] if f["own"]}
            assert rep["function_count"] >= 0.95 * truth["counts"]["functions"], (rel, rep["function_count"])
            ev = [e for e in st.cases.list_evidence(cid, kind="native.functions") if e["module_id"] == mid]
            found = {f["offset"] for f in st.cases.evidence_body(ev[-1]["evidence_id"])["functions"]}
            assert own <= found, (rel, sorted(own - found))
            assert rep["decompiled"] >= 1 and "xref_index" in rep
            copy = st.cases.case_root(cid) / "re" / mid / "member"
            assert [_sha(p.read_bytes()) for p in copy.iterdir()] == [mods[rel]["sha256"]]   # one verified copy in the work folder
        assert {p.name: _sha(p.read_bytes()) for p in src.iterdir()} == before        # the user's files are untouched
    finally:
        st.stop()
