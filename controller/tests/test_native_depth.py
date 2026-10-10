"""R1 "no-AI native analysis depth": packer check + consented UPX unpack, post-analysis passes (pdata, relocation pointers,
import thunks, signature packs), demangling, the SQLite cross-index and the stage's decompile order.

Pure-Python parts run everywhere; parts that need the real rizin / the pinned UPX skip when they are not installed
(set REBUILD_STUDIO_TOOLS). Inputs are the committed benchmark binaries (fixtures/bench/*/bin).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from rebuild_controller.backends import demangle, packer, rizin_passes, xref_index
from rebuild_controller.backends.rizin_worker import RizinBackend, find_rizin
from rebuild_controller.config import Limits, Settings
from rebuild_controller.ids import sha256_file
from rebuild_controller.stages import decompile_order

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "fixtures" / "bench"
UPX_ROW = BENCH / "upx_c_msvc_x64" / "bin" / "benchc_upx.exe"
C64 = BENCH / "c_msvc_x64_o2" / "bin" / "benchc.exe"
C86 = BENCH / "c_msvc_x86_o2" / "bin" / "benchc32.exe"
CPP = BENCH / "cpp_msvc_x64_o2" / "bin" / "benchcpp.exe"
RUST = BENCH / "rust_msvc_x64" / "bin" / "benchrs.exe"

needs_rizin = pytest.mark.skipif(find_rizin(Settings()) is None, reason="rizin not installed on this host")
needs_upx = pytest.mark.skipif(packer.find_upx(Settings().tools_dir) is None, reason="pinned UPX not installed (Tools page: UPX)")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ packer report
def test_packer_report_flags_the_upx_row_and_nothing_else():
    rep = packer.packer_report(UPX_ROW)
    assert rep["packed"] and rep["packer"] == "UPX" and rep["confidence"] >= 0.9 and rep["unpacker"] == "upx -d"
    assert any(s["kind"] == "section_name" for s in rep["signatures"])
    assert any("no data in the file" in a for a in rep["anomalies"]) and any("writable and executable" in a for a in rep["anomalies"])
    for clean in (C64, C86, CPP, RUST, BENCH / "go_pe_x64" / "bin" / "benchgo.exe", BENCH / "go_elf_x64" / "bin" / "benchgo"):
        r = packer.packer_report(clean)
        assert not r["packed"], (clean.name, r["reasons"])


def test_packer_report_entropy_heuristic_without_a_signature(tmp_path):
    """A PE whose only code section is random data, written+executable, with no imports: packed (unknown family)."""
    import os
    src = bytearray(C64.read_bytes())
    import pefile
    pe = pefile.PE(data=bytes(src))
    text = pe.sections[0]
    off, size = text.PointerToRawData, text.SizeOfRawData
    src[off:off + size] = os.urandom(size)
    pe.close()
    pe = pefile.PE(data=bytes(src))
    pe.sections[0].Characteristics |= 0x80000000          # writable + executable
    p = tmp_path / "rand.exe"
    p.write_bytes(pe.write())
    rep = packer.packer_report(p)
    assert rep["packed"] and rep["packer"] == "unknown" and rep["unpacker"] is None
    assert any("high entropy" in a for a in rep["anomalies"])


def test_packer_report_never_raises_on_garbage(tmp_path):
    p = tmp_path / "junk.exe"
    p.write_bytes(b"MZ" + b"\x00" * 10)
    rep = packer.packer_report(p)
    assert rep["format"] == "pe" and not rep["packed"] and rep["anomalies"]
    assert packer.packer_report(tmp_path / "missing.exe")["error"]


def test_prepare_without_consent_returns_the_original(tmp_path):
    path, info = packer.prepare_for_analysis(UPX_ROW, tmp_path / "work", allow_unpack=False)
    assert path == UPX_ROW and info["report"]["packed"] and info["unpack"]["needs_consent"]
    assert not (tmp_path / "work").exists()


def test_find_upx_only_returns_the_pinned_binary(tmp_path):
    fake = tmp_path / "upx"
    fake.mkdir()
    (fake / "upx.exe").write_bytes(b"not upx")
    lock = json.loads((REPO / "docs" / "dependency-lock.json").read_text(encoding="utf-8"))
    assert lock["tools"]["upx"]["layout"]["entry_sha256"] == lock["fixture_build_tools"]["upx"]["layout"]["entry_sha256"]
    t = packer.find_upx(tmp_path)
    assert t is None or t.sha256 == lock["tools"]["upx"]["layout"]["entry_sha256"]


@needs_upx
def test_unpack_upx_into_work_folder_never_in_place(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / UPX_ROW.name
    shutil.copy2(UPX_ROW, src)
    before = _sha(src)
    upx = packer.find_upx(Settings().tools_dir)
    with pytest.raises(packer.UnpackError):
        packer.unpack_upx(src, src_dir, upx)            # refuses the folder holding the original
    res = packer.unpack_upx(src, tmp_path / "work", upx)
    assert res["ok"] and res["tool_pinned"] and not res["still_packed"]
    out = Path(res["unpacked_path"])
    assert out.parent == (tmp_path / "work").resolve() and out.is_file()
    assert _sha(src) == before == res["original_sha256"] and sorted(p.name for p in src_dir.iterdir()) == [UPX_ROW.name]
    assert res["unpacked_size"] > src.stat().st_size


# ------------------------------------------------------------------------------------------------ pass helpers
def test_funclet_prologue_detection():
    assert rizin_passes.is_funclet_prologue(bytes.fromhex("488954241055 4883ec20 488bea".replace(" ", "")))
    assert rizin_passes.is_funclet_prologue(bytes.fromhex("4889542410 53 55 4883ec28 488d6a28".replace(" ", "")))
    assert not rizin_passes.is_funclet_prologue(bytes.fromhex("48895c2408 57 4883ec20".replace(" ", "")))   # spills rbx: normal
    assert not rizin_passes.is_funclet_prologue(bytes.fromhex("4889542410 4883ec28 e8".replace(" ", "")))   # no rbp from rdx


def test_pdata_starts_skip_chained_and_funclets():
    cpp = rizin_passes.pdata_starts(CPP)
    assert cpp["entries"] > 0 and cpp["chained"] > 0 and cpp["starts"]
    rs = rizin_passes.pdata_starts(RUST)
    assert rs["funclets"] and rs["starts"] and not set(rs["funclets"]) & set(rs["starts"])
    assert rizin_passes.pdata_starts(C86)["starts"] == []        # x86 has no exception directory


def test_reloc_code_pointers_point_into_code():
    info = rizin_passes.reloc_code_pointers(C86)
    assert info["relocs"] > 0 and info["targets"]
    import pefile
    pe = pefile.PE(str(C86), fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    code = [(base + s.VirtualAddress, base + s.VirtualAddress + s.Misc_VirtualSize) for s in pe.sections if s.Characteristics & 0x20000000]
    assert all(any(a <= t < b for a, b in code) for t in info["targets"])


def test_sigpack_manifest_pins_every_pack():
    man = rizin_passes.sigpack_manifest()
    assert man.get("packs"), "no signature packs shipped"
    for p in man["packs"]:
        f = rizin_passes.SIGPACKS / p["file"]
        assert f.is_file() and _sha(f) == p["sha256"], p["file"]
        assert p["signatures"] > 0 and p["sources"] and all(len(s["sha256"]) == 64 for s in p["sources"])
        assert "bench" not in json.dumps(p["sources"]).lower()      # built from runtime libraries, never the corpus
    assert rizin_passes.packs_for("pe", "x86", 64) and rizin_passes.packs_for("pe", "x86", 32)


# ------------------------------------------------------------------------------------------------ demangling
@pytest.mark.parametrize("lang,full,want", [
    ("msvc", "public: void __cdecl std::bad_alloc::constructor(void) __ptr64", "std::bad_alloc::bad_alloc"),
    ("msvc", "public: virtual __cdecl Foo::destructor(void) __ptr64", "Foo::~Foo"),
    ("msvc", "void * __ptr64 __cdecl operator new(unsigned __int64)", "operator new"),
    ("msvc", "public: virtual char const * __ptr64 __cdecl std::exception::what(void)const __ptr64", "std::exception::what"),
    ("rust", "std[2a1f8e54e2930f4]::sys::io::error::windows::is_interrupted", "std::sys::io::error::windows::is_interrupted"),
    ("rust", "std::rt::lang_start_internal::h0123456789abcdef", "std::rt::lang_start_internal"),
    ("c++", "std::vector<int, std::allocator<int>>::push_back(int const&)", "std::vector<int,std::allocator<int>>::push_back"),
    ("msvc", "public: class std::basic_string<char,struct std::char_traits<char>,class std::allocator<char> > & __ptr64 __cdecl "
             "std::basic_string<char,struct std::char_traits<char>,class std::allocator<char> >::append(char const * __ptr64) __ptr64",
     "std::basic_string<char,std::char_traits<char>,std::allocator<char> >::append"),
    ("rust", "<std[abc123]::net::udp::UdpSocket>::bind", "std::net::udp::UdpSocket::bind"),
    ("rust", "<core::fmt::Arguments as core::fmt::Display>::fmt", "<core::fmt::Arguments as core::fmt::Display>::fmt"),
])
def test_qualified_name(lang, full, want):
    assert demangle.qualified_name(lang, full) == want


def test_guess_lang():
    assert demangle.guess_lang("??0bad_alloc@std@@QEAA@XZ") == "msvc"
    assert demangle.guess_lang("_RNvNtCs1_3std2rt10lang_start") == "rust"
    assert demangle.guess_lang("_ZN3std2rt19lang_start_internal17h0123456789abcdefE") == "rust"
    assert demangle.guess_lang("_ZNSt6vectorIiSaIiEE9push_backERKi") == "c++"
    assert demangle.guess_lang("main") is None
    assert demangle.pat_safe("operator new") == "operator_new"


# ------------------------------------------------------------------------------------------------ cross-index
def test_xref_index_build_and_search(tmp_path):
    funcs = [{"offset": 0x1000, "name": "main", "size": 0x40, "minbound": 0x1000, "maxbound": 0x1040, "nbbs": 3},
             {"offset": 0x1040, "name": "helper", "size": 0x10, "minbound": 0x1040, "maxbound": 0x1050, "nbbs": 1}]
    strings = [{"vaddr": 0x3000, "string": "usage: tool <file>", "type": "ascii", "section": ".rdata", "length": 18},
               {"vaddr": 0x3020, "string": "100%_done", "type": "ascii", "section": ".rdata", "length": 9}]
    xrefs = [{"from": 0x1010, "to": 0x3000, "type": "DATA"}, {"from": 0x1020, "to": 0x1040, "type": "CALL"},
             {"from": 0x1044, "to": 0x3020, "type": "DATA"}]
    db = tmp_path / "x.sqlite"
    counts = xref_index.build(db, functions=funcs, strings=strings, xrefs=xrefs, meta={"k": "v"})
    assert counts == {"functions": 2, "strings": 2, "xrefs": 3, "string_refs": 2, "call_edges": 1}
    r = xref_index.search(db, "USAGE")
    assert r["strings"][0]["referenced_by"][0]["name"] == "main" and not r["functions"]
    assert [s["string"] for s in xref_index.search(db, "%_")["strings"]] == ["100%_done"]    # LIKE wildcards are literal
    assert xref_index.search(db, "help")["functions"][0]["name"] == "helper"
    refs = xref_index.function_refs(db, 0x1000)
    assert refs["callees"] == ["0x1040"] and refs["strings"][0]["string"].startswith("usage")
    assert xref_index.function_refs(db, 0x1040)["callers"] == ["0x1000"]


def test_decompile_order_entry_first_then_largest():
    funcs = [{"offset": 1, "size": 10}, {"offset": 2, "size": 500}, {"offset": 3, "size": 5}, {"offset": 4, "size": 500}, {"name": "x"}]
    assert [f["offset"] for f in decompile_order(funcs, {3})] == [3, 2, 4, 1]


# ------------------------------------------------------------------------------------------------ real rizin
@pytest.fixture(scope="module")
def backend():
    b = RizinBackend(Settings(limits=Limits()))
    yield b
    b.close()


@needs_rizin
def test_passes_run_inside_analysis_and_name_import_thunks(backend):
    s = backend.session_for_path(C86)
    rep = s.analyze()
    passes = rep["passes"]
    assert set(passes) == {"sigpacks", "pdata", "relocptrs", "thunks", "auto_names_fixed"}
    assert not any("error" in v for v in passes.values() if isinstance(v, dict))
    assert [a["pack"] for a in passes["sigpacks"]["applied"]] == ["msvc-14.29.30133-crt"]   # no Rust markers in a C program
    assert passes["thunks"]["renamed"] > 10 and passes["relocptrs"]["added"] > 0
    names = {f["name"] for f in s.cached_functions()}
    assert "memset" in names and not any(n.startswith("sub.") and n.endswith("_memset") for n in names)


@needs_rizin
@needs_upx
def test_case_unpack_redirects_analysis_and_builds_cross_index(backend, cases, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(UPX_ROW, src / UPX_ROW.name)
    orig_sha = sha256_file(src / UPX_ROW.name)
    c = cases.create_case(name="packed", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust", output_type="cli")
    mid = cases.add_module(c["case_id"], UPX_ROW.name, orig_sha, (src / UPX_ROW.name).stat().st_size, "pe", "native_pe", "x86_64")
    rep = backend.op_packer_report(cases, case_id=c["case_id"], module_id=mid)
    assert rep.ok and rep.data["report"]["packed"] and rep.data["report"]["packer"] == "UPX"
    packed_funcs = backend.op_functions(cases, case_id=c["case_id"], module_id=mid)
    assert packed_funcs.ok and packed_funcs.data["count"] < 10                 # the stub only
    refused = backend.op_unpack(cases, case_id=c["case_id"], module_id=mid)
    assert not refused.ok and "confirm" in refused.error
    u = backend.op_unpack(cases, case_id=c["case_id"], module_id=mid, confirm=True)
    assert u.ok, u.error
    copy = u.data["unpacked"]
    work = backend.work_dir(cases, c["case_id"], mid)
    assert (work / copy["rel"]).is_file() and copy["original_sha256"] == orig_sha
    assert sha256_file(src / UPX_ROW.name) == orig_sha and [p.name for p in src.iterdir()] == [UPX_ROW.name]
    funcs = backend.op_functions(cases, case_id=c["case_id"], module_id=mid)
    assert funcs.ok and funcs.data["count"] > 50
    ev = cases.get_evidence(funcs.evidence_ids[0])
    assert ev["meta"]["inputs"]["module_sha256"] == copy["sha256"] != orig_sha
    idx = backend.op_xref_index(cases, case_id=c["case_id"], module_id=mid)
    assert idx.ok and idx.data["counts"]["string_refs"] > 0 and idx.data["counts"]["call_edges"] > 0
    hit = backend.op_search_index(cases, case_id=c["case_id"], module_id=mid, query="usage:")
    assert hit.ok and hit.data["untrusted"] and hit.data["strings"]
    assert any(r["function"] for s in hit.data["strings"] for r in s["referenced_by"])
    rel = backend.op_relocations(cases, case_id=c["case_id"], module_id=mid)
    assert rel.ok and rel.data["count"] > 0
    again = backend.op_packer_report(cases, case_id=c["case_id"], module_id=mid)
    assert again.ok and again.data["analysis_copy"]["sha256"] == copy["sha256"]


# ------------------------------------------------------------------------------------------------ PDB (verified by GUID + age)
def _msvc_available() -> bool:
    import sys
    sys.path.insert(0, str(BENCH))
    try:
        from build_bench import vswhere_install
        return vswhere_install() is not None
    except Exception:
        return False


@pytest.fixture(scope="module")
def built_c_row(tmp_path_factory):
    """Rebuild the C row with its PDB (MSVC, /Brepro: the exe is byte-identical to the committed one)."""
    if not _msvc_available():
        pytest.skip("MSVC Build Tools not installed")
    import sys
    sys.path.insert(0, str(BENCH))
    import build_bench
    work = tmp_path_factory.mktemp("pdbrow")
    res = build_bench.build_row("c_msvc_x64_o2", work)
    exe = Path(res["binary"])
    assert _sha(exe) == _sha(C64)
    return exe, exe.with_suffix(".pdb")


def test_pdb_identity_matches_codeview(built_c_row):
    from rebuild_controller.backends import pdb
    exe, p = built_c_row
    cv = pdb.codeview(exe)
    assert cv["pdb_name"] == "benchc.pdb" and len(cv["key"]) == 33
    assert pdb.pdb_identity(p) == {"guid": cv["guid"], "age": cv["age"]}
    assert pdb.find_sidecar(exe) == p
    assert pdb.codeview(UPX_ROW) is None or pdb.find_sidecar(UPX_ROW) is None
    assert pdb.symbol_server_url(cv).endswith(f"/benchc.pdb/{cv['key']}/benchc.pdb")


def test_pdb_download_keeps_only_a_verified_file(built_c_row, tmp_path):
    import httpx
    from rebuild_controller.backends import pdb
    exe, p = built_c_row
    cv = pdb.codeview(exe)
    good = p.read_bytes()
    bad = bytearray(good)
    info = pdb._msf_stream(good, 1)
    bad[bad.find(info[12:28])] ^= 0xFF                       # another build: GUID differs
    for body, ok in ((bytes(bad), False), (good, True)):
        seen = []

        def handler(req, body=body):
            seen.append(str(req.url))
            return httpx.Response(200, content=body)
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            res = pdb.download(cv, tmp_path / ("ok" if ok else "bad"), client=c)
        assert res["ok"] is ok and seen == [pdb.symbol_server_url(cv)]
        assert sorted(x.name for x in (tmp_path / ("ok" if ok else "bad")).iterdir()) == (["benchc.pdb"] if ok else [])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        assert not pdb.download(cv, tmp_path / "nf", client=c)["ok"]


@needs_rizin
def test_sidecar_pdb_is_loaded_and_names_functions(built_c_row):
    exe, _p = built_c_row
    b = RizinBackend(Settings(limits=Limits()), analysis={"command": "aaa", "analysis.timeout": 300, "passes": []})
    try:
        s = b.session_for_path(exe)
        rep = s.analyze()
        assert rep["pdb"]["source"] == "next to the program"
        names = [f["name"] for f in s.cached_functions()]
        assert sum(n.startswith("pdb.") for n in names) >= 20 and "pdb.benchc.bench_base64" in names
    finally:
        b.close()
    nb = RizinBackend(Settings(limits=Limits()), analysis={"command": "aaa", "analysis.timeout": 300, "passes": []}, use_pdb=False)
    try:
        s = nb.session_for_path(exe)
        assert s.analyze()["pdb"] is None and not any(f["name"].startswith("pdb.") for f in s.cached_functions())
    finally:
        nb.close()


# ------------------------------------------------------------------------------------------------ the pipeline stage
@needs_rizin
@needs_upx
@pytest.mark.parametrize("allow", [False, True])
def test_analyze_module_stage_checks_packer_and_unpacks_only_with_consent(settings, tmp_path, allow):
    from rebuild_controller.jobs import JobState
    from rebuild_controller.services import StudioServices
    settings.limits.max_stage_seconds = 600
    settings.limits.lease_timeout_seconds = 120
    st = StudioServices(settings)
    try:
        src = tmp_path / "src"
        src.mkdir()
        shutil.copy2(UPX_ROW, src / UPX_ROW.name)
        before = _sha(src / UPX_ROW.name)
        case = st.create_case(name="upx", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust",
                              output_type="exe", ai_policy={"mode": "no_ai"},
                              settings={"decompile_limit": 5, "decompile_seconds": 120, "unpack_packed": allow})
        cid = case["case_id"]
        mid = st.cases.add_module(cid, UPX_ROW.name, before, UPX_ROW.stat().st_size, "pe", "native_pe", "x86_64")
        job = st.jobs.create(cid, "analyze_module", "analyze", {"module_id": mid})
        for _ in range(50):
            if st.runner.run_pending() == 0:
                break
        j = st.jobs.get(job.job_id)
        assert j.state == JobState.COMPLETED, j.error
        rep = j.result
        assert rep["packer"]["packed"] and rep["packer"]["packer"] == "UPX"
        assert ("unpacked" in rep) is allow
        assert rep["function_count"] > (50 if allow else 0) and (rep["function_count"] < 10) is (not allow)
        for op in ("sections", "entrypoints", "symbols", "relocations", "xref_index"):
            assert op in rep, op
        assert rep["decompile_order"].startswith("entry points") and rep["decompiled"] >= 1
        assert rep["analysis_passes"] is not None
        assert _sha(src / UPX_ROW.name) == before and [p.name for p in src.iterdir()] == [UPX_ROW.name]
        logs = " ".join(str(e) for e in st.cases.list_evidence(cid, kind="native.packer_report"))
        assert "UPX" in logs
    finally:
        st.stop()


def test_sigpack_markers_select_the_runtime():
    man = {p["name"].split("-")[0]: p.get("markers") for p in rizin_passes.sigpack_manifest()["packs"]}
    go = BENCH / "go_pe_x64" / "bin" / "benchgo.exe"
    assert rizin_passes.markers_present(RUST, man["rust"]) and not rizin_passes.markers_present(C64, man["rust"])
    assert rizin_passes.markers_present(C64, man["msvc"]) and rizin_passes.markers_present(RUST, man["msvc"])
    assert not rizin_passes.markers_present(go, man["msvc"]) and not rizin_passes.markers_present(go, man["rust"])
    assert rizin_passes.markers_present(go, None)


def test_thunk_names_never_carry_rizin_command_syntax():
    for bad in ("?foo@@YAXXZ", "a;b", "x|y", "n@0x1", "a b", "1abc", "<lambda>"):
        assert not rizin_passes.SAFE_NAME.fullmatch(bad)
    assert rizin_passes.SAFE_NAME.fullmatch("__acrt_iob_func")
