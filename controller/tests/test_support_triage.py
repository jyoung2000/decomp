"""Detection precision + support statements + triage backend for Unity (Mono/IL2CPP), GameMaker, Android, JVM, Unreal and Mach-O.

Everything here uses tiny synthetic samples (headers/tables only) or the checked-in managed/apk samples; no external tool is needed.
None of these kinds except Mono Unity and JVM may ever claim code recovery, and the tests pin that.
"""
from __future__ import annotations

import hashlib
import re
import json
import shutil
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller.adapters.contract import Availability
from rebuild_controller.backends import android_info
from rebuild_controller.backends import support as sp
from rebuild_controller.backends.detect import detect_path, sniff, summarize_profile
from rebuild_controller.backends.ilspy import ILSpyBackend
from rebuild_controller.backends.inventory import inventory_root
from rebuild_controller.backends.triage import TriageBackend
from rebuild_controller.config import Limits

DATA = Path(__file__).parent / "data"
MINAPP = DATA / "jvm" / "minapp.apk"
JAVACLI = Path(__file__).resolve().parents[2] / "fixtures" / "javacli" / "original" / "javacli.jar"
UNITY_MONO = DATA / "managed" / "unity_mono"


# ---------------------------------------------------------------------------------------------------------------------
# synthetic builders (independent of the parsers under test)
# ---------------------------------------------------------------------------------------------------------------------
def macho_thin(cpu: int = 0x01000007, ftype: int = 2, *, little: bool = True, bits: int = 64, cmds: bytes = b"", ncmds: int = 0) -> bytes:
    e = "<" if little else ">"
    magic = {(True, 64): b"\xcf\xfa\xed\xfe", (True, 32): b"\xce\xfa\xed\xfe", (False, 64): b"\xfe\xed\xfa\xcf", (False, 32): b"\xfe\xed\xfa\xce"}[(little, bits)]
    hdr = magic + struct.pack(e + "iiIIII", cpu, 3, ftype, ncmds, len(cmds), 0x200085)
    if bits == 64:
        hdr += b"\0" * 4
    return hdr + cmds


def lc_dylib(name: str) -> bytes:
    raw = name.encode() + b"\0"
    size = (24 + len(raw) + 7) & ~7
    return struct.pack("<IIIIII", 0xC, size, 24, 2, 0x10000, 0x10000) + raw + b"\0" * (size - 24 - len(raw))


def lc_encryption(cryptid: int) -> bytes:
    return struct.pack("<IIIIII", 0x2C, 24, 0x4000, 0x1000, cryptid, 0)


def macho_fat(slices: list[bytes]) -> bytes:
    n = len(slices)
    off = 4096
    head = b"\xca\xfe\xba\xbe" + struct.pack(">I", n)
    body = b""
    for s in slices:
        cpu = struct.unpack("<i", s[4:8])[0]
        head += struct.pack(">iiIII", cpu, 3, off + len(body), len(s), 12)
        body += s + b"\0" * ((-len(s)) % 4096)
    return head + b"\0" * (off - len(head)) + body


def gamemaker_form(*, with_code: bool, strings: list[str], bytecode: int = 17) -> bytes:
    gen8 = bytes([0, bytecode, 0, 0]) + b"\0" * 60
    chunks = [("GEN8", gen8), ("SPRT", b"\0" * 16)]
    if with_code:
        chunks.append(("CODE", b"\x01" * 64))
    # STRG: count + absolute offsets + string objects; offsets depend on layout so build in two passes
    def build(strg_payload: bytes) -> bytes:
        body = b"".join(n.encode() + struct.pack("<I", len(d)) + d for n, d in chunks)
        body += b"STRG" + struct.pack("<I", len(strg_payload)) + strg_payload
        return b"FORM" + struct.pack("<I", len(body)) + body
    strg_start = 8 + sum(8 + len(d) for _, d in chunks) + 8
    offs, objs, cur = [], b"", strg_start + 4 + 4 * len(strings)
    for s in strings:
        offs.append(cur)
        obj = struct.pack("<I", len(s.encode())) + s.encode() + b"\0"
        objs += obj
        cur += len(obj)
    payload = struct.pack("<I", len(strings)) + struct.pack(f"<{len(offs)}I", *offs) + objs
    return build(payload)


def pak_file(version: int = 8, encrypted: bool = False, body: int = 600) -> bytes:
    footer = bytes([1 if encrypted else 0]) + b"\xe1\x12\x6f\x5a" + struct.pack("<IQQ", version, 100, 200) + b"\0" * 20 + b"\0" * 160
    return b"\x11" * body + footer


def il2cpp_metadata(names: list[str], version: int = 29) -> bytes:
    blob = b"".join(n.encode() + b"\0" for n in names)
    off = 256
    head = b"\xaf\x1b\xb1\xfa" + struct.pack("<i", version) + struct.pack("<IIIIII", 0, 0, 0, 0, off, len(blob))
    return head + b"\0" * (off - len(head)) + blob


def mz_stub() -> bytes:
    return b"MZ" + b"\0" * 62       # not a parseable PE: detection must still work from names


@pytest.fixture
def studio(cases):
    return SimpleNamespace(cases=cases)


@pytest.fixture
def case(cases, src_out):
    src, out = src_out
    return cases.create_case(name="triage", source_root=str(src), output_root=str(out), target_language="rust", output_type="exe")


# ---------------------------------------------------------------------------------------------------------------------
# Mach-O (thin + fat) and the 0xCAFEBABE collision with Java classes
# ---------------------------------------------------------------------------------------------------------------------
def test_macho_thin_detection_with_support_statement(tmp_path):
    p = tmp_path / "app"
    p.write_bytes(macho_thin(cmds=lc_dylib("/usr/lib/libSystem.B.dylib") + lc_encryption(0), ncmds=2))
    d = sniff(p)
    assert (d.format, d.profile, d.arch, d.bits) == ("macho", "native_macho", "x86_64", 64)
    assert d.flags["layout"] == "thin" and d.flags["encrypted"] is False and d.support_status == "detected_only"
    st = d.support["statement"]
    assert "What can be recovered:" in st and "What cannot:" in st and "What the rebuild will be:" in st and "Blocker:" in st
    info = sp.inspect_macho(p)
    assert info["slices"][0]["dylibs"] == ["/usr/lib/libSystem.B.dylib"] and info["architectures"] == ["x86_64"]


@pytest.mark.parametrize("little,bits", [(True, 32), (False, 64), (False, 32)])
def test_macho_all_thin_magics(tmp_path, little, bits):
    p = tmp_path / f"m{int(little)}{bits}"
    p.write_bytes(macho_thin(cpu=7 if bits == 32 else 0x01000007, little=little, bits=bits))
    d = sniff(p)
    assert d.profile == "native_macho" and d.bits == bits and d.flags["layout"] == "thin"


def test_macho_fat_universal_lists_every_slice(tmp_path):
    p = tmp_path / "universal"
    p.write_bytes(macho_fat([macho_thin(0x01000007), macho_thin(0x0100000C, ftype=2)]))
    d = sniff(p)
    assert d.profile == "native_macho" and d.flags["layout"] == "fat" and d.flags["slice_count"] == 2
    assert d.flags["architectures"] == ["arm64", "x86_64"] and d.arch == "universal:arm64+x86_64"
    info = sp.inspect_macho(p)
    assert [s["arch"] for s in info["slices"]] == ["x86_64", "arm64"] and all(s["offset"] % 4096 == 0 for s in info["slices"])


def test_macho_encrypted_slice_is_reported_as_blocker(tmp_path):
    p = tmp_path / "store_app"
    p.write_bytes(macho_thin(0x0100000C, cmds=lc_encryption(1), ncmds=1))
    info = sp.inspect_macho(p)
    assert info["encrypted"] is True and "encrypted" in info["blocker"] and sniff(p).flags["encrypted"] is True


def test_java_class_is_not_mistaken_for_fat_macho(tmp_path):
    cls = tmp_path / "A.class"
    cls.write_bytes(b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 61) + b"\0" * 40)      # minor 0, major 61 (Java 17)
    d = sniff(cls)
    assert (d.format, d.profile) == ("java_class", "jvm") and d.flags["java_version"] == 17
    assert sp.macho_kind(cls.read_bytes()) is None and sp.is_java_class(cls.read_bytes())


def test_macho_truncated_header_is_low_confidence_not_a_crash(tmp_path):
    p = tmp_path / "trunc"
    p.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 8)
    d = sniff(p)
    assert d.profile == "native_macho" and d.confidence < 1 and "parse_error" in d.flags


# ---------------------------------------------------------------------------------------------------------------------
# GameMaker
# ---------------------------------------------------------------------------------------------------------------------
def test_gamemaker_detection_chunks_and_strings(tmp_path):
    p = tmp_path / "data.win"
    p.write_bytes(gamemaker_form(with_code=True, strings=["gml_Object_obj_player_Create_0", "spr_player", "Hello"]))
    d = sniff(p)
    assert (d.format, d.profile) == ("gamemaker_data", "gamemaker") and d.flags["bytecode_version"] == 17 and d.support_status == "detected_only"
    info = sp.inspect_gamemaker(p)
    assert [c["name"] for c in info["chunks"]] == ["GEN8", "SPRT", "CODE", "STRG"]
    assert info["build_kind"] == "vm_bytecode" and info["bytecode_version"] == 17
    assert info["strings"] == ["gml_Object_obj_player_Create_0", "spr_player", "Hello"] and info["strings_truncated"] is False
    assert "no GameMaker decompiler" in info["blocker"]


def test_gamemaker_without_code_chunk_is_flagged_likely_yyc_and_strings_are_capped(tmp_path):
    p = tmp_path / "game.unx"
    p.write_bytes(gamemaker_form(with_code=False, strings=[f"s{i}" for i in range(50)]))
    assert sniff(p).profile == "gamemaker"                  # Linux data file name
    info = sp.inspect_gamemaker(p, max_strings=10)
    assert info["build_kind"] == "likely_yyc_native_or_no_code" and info["has_code_chunk"] is False
    assert len(info["strings"]) == 10 and info["strings_truncated"] is True and info["string_count"] == 50


def test_gamemaker_malformed_chunk_table_is_disclosed(tmp_path):
    good = gamemaker_form(with_code=False, strings=["a"])
    p = tmp_path / "data.win"
    p.write_bytes(good[:40] + b"\xff" * (len(good) - 40))
    info = sp.inspect_gamemaker(p)
    assert info["chunks"][0]["name"] == "GEN8" and "chunk_note" in info


# ---------------------------------------------------------------------------------------------------------------------
# Unreal
# ---------------------------------------------------------------------------------------------------------------------
def test_unreal_pak_footer_detection(tmp_path):
    p = tmp_path / "game-WindowsNoEditor.pak"
    p.write_bytes(pak_file(version=8))
    d = sniff(p)
    assert (d.format, d.profile) == ("unreal_pak", "unreal") and d.flags["pak_version"] == 8 and d.flags["encrypted_index"] is False
    assert d.support_status == "detected_only"
    info = sp.inspect_unreal(p)
    assert info["footer"]["index_offset"] == 100 and info["footer"]["index_size"] == 200 and "extractor" in info["blocker"]


def test_unreal_encrypted_pak_and_iostore_and_non_pak(tmp_path):
    enc = tmp_path / "e.pak"
    enc.write_bytes(pak_file(version=11, encrypted=True))
    assert sp.inspect_unreal(enc)["footer"]["encrypted_index"] is True and "AES key" in sp.inspect_unreal(enc)["blocker"]
    utoc = tmp_path / "global.utoc"
    utoc.write_bytes(sp.UTOC_MAGIC + b"\0" * 100)
    assert sniff(utoc).format == "unreal_iostore" and sniff(utoc).profile == "unreal"
    fake = tmp_path / "fake.pak"
    fake.write_bytes(b"not a pak " * 100)
    assert sniff(fake).profile == "unknown"                   # .pak by name alone is not enough


def test_unreal_installation_with_version_marker(tmp_path):
    root = tmp_path / "Game"
    (root / "Game" / "Content" / "Paks").mkdir(parents=True)
    (root / "Engine" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Game" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Game" / "Content" / "Paks" / "Game-WindowsNoEditor.pak").write_bytes(pak_file(version=8))
    (root / "Game" / "Binaries" / "Win64" / "Game-Win64-Shipping.exe").write_bytes(mz_stub() + b"\0" * 5000 + b"++UE4+Release-4.27\0" + b"\0" * 100)
    (root / "Engine" / "Binaries" / "Win64" / "CrashReportClient.exe").write_bytes(mz_stub())
    inv = inventory_root(root, Limits())
    prof = inv["profile"]
    assert prof["primary"] == "unreal" and prof["support_status"] == "detected_only"
    assert prof["engine_version"]["engine"] == "UE4" and prof["engine_version"]["version"] == "4.27"
    kinds = {e["kind"] for e in prof["evidence"]}
    assert {"pak", "shipping_exe", "engine_binaries"} <= kinds
    assert "Blueprint" in prof["support_statement"] and "None is produced from code" in prof["support_statement"]
    assert any(m.endswith(".pak") for m in inv["modules"])    # the pak is a module, so the plan reports it as unsupported


def test_unreal_build_version_json_wins(tmp_path):
    root = tmp_path / "G"
    (root / "Engine" / "Build").mkdir(parents=True)
    (root / "Engine" / "Build" / "Build.version").write_text(json.dumps({"MajorVersion": 5, "MinorVersion": 3, "PatchVersion": 2, "BranchName": "++UE5+Release-5.3"}))
    (root / "Content" / "Paks").mkdir(parents=True)
    (root / "Content" / "Paks" / "p.pak").write_bytes(pak_file(version=11))
    prof = inventory_root(root, Limits())["profile"]
    assert prof["engine_version"] == {"engine": "UE5", "version": "5.3.2", "branch": "++UE5+Release-5.3", "source": "Engine/Build/Build.version"}


# ---------------------------------------------------------------------------------------------------------------------
# Unity: Mono must route to ILSpy, IL2CPP must be detected-only
# ---------------------------------------------------------------------------------------------------------------------
def make_unity_mono(root: Path) -> None:
    (root / "Game_Data" / "Managed").mkdir(parents=True)
    (root / "Game.exe").write_bytes(mz_stub())
    (root / "UnityPlayer.dll").write_bytes(mz_stub())
    shutil.copy2(UNITY_MONO / "Assembly-CSharp.dll", root / "Game_Data" / "Managed" / "Assembly-CSharp.dll")
    shutil.copy2(UNITY_MONO / "UnityEngine.dll", root / "Game_Data" / "Managed" / "UnityEngine.dll")


def test_unity_mono_is_detected_and_routed_to_ilspy(tmp_path):
    from rebuild_controller import stages
    make_unity_mono(tmp_path)
    prof = inventory_root(tmp_path, Limits())["profile"]
    assert prof["primary"] == "unity_mono" and prof["support_status"] == "supported"
    assert prof["support"]["backend"] == "ilspy" and any(e["kind"] == "managed_assembly" for e in prof["evidence"])
    assert "ILSpy" in prof["support_statement"] and "IL2CPP" not in prof["support_statement"]
    # routing: the profile has a recovery stage and the ILSpy backend serves it
    assert stages.PROFILE_STAGE["unity_mono"] == "recover_managed"
    assert "unity_mono" in ILSpyBackend().probe().profiles
    assert sniff(tmp_path / "Game_Data" / "Managed" / "Assembly-CSharp.dll").profile == "unity_mono"
    assert sniff(tmp_path / "UnityPlayer.dll").flags["engine_binary"] == "unity"


def test_unity_il2cpp_needs_metadata_and_is_detected_only(tmp_path):
    (tmp_path / "Game_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
    (tmp_path / "GameAssembly.dll").write_bytes(mz_stub())
    (tmp_path / "UnityPlayer.dll").write_bytes(mz_stub())
    (tmp_path / "Game.exe").write_bytes(mz_stub())
    (tmp_path / "Game_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(il2cpp_metadata(["Player", "Update", "health"]))
    inv = inventory_root(tmp_path, Limits())
    prof = inv["profile"]
    assert prof["primary"] == "unity_il2cpp" and prof["support_status"] == "detected_only" and prof["support"]["backend"] == "triage"
    kinds = {e["kind"] for e in prof["evidence"]}
    assert {"il2cpp_metadata", "game_assembly", "unity_player"} <= kinds
    assert "no IL to decompile" in prof["support_statement"] and "Il2CppDumper" in json.dumps(prof["support"]["tools_candidate"])
    assert "Game_Data/il2cpp_data/Metadata/global-metadata.dat" in inv["modules"]
    meta = next(f for f in inv["files"] if f["path"].endswith("global-metadata.dat"))["detect"]
    assert meta["profile"] == "unity_il2cpp" and meta["flags"]["metadata_version"] == 29 and meta["support_status"] == "detected_only"


def test_unity_gameassembly_without_metadata_lowers_confidence(tmp_path):
    (tmp_path / "GameAssembly.dll").write_bytes(mz_stub())
    prof = inventory_root(tmp_path, Limits())["profile"]
    assert prof["primary"] == "unity_il2cpp" and prof["confidence"] < 1 and any("global-metadata.dat not found" in r for r in prof["reasons"])


def test_il2cpp_metadata_identifiers_are_listed_and_capped(tmp_path):
    p = tmp_path / "global-metadata.dat"
    p.write_bytes(il2cpp_metadata([f"Name{i}" for i in range(30)], version=24))
    info = sp.inspect_il2cpp_metadata(p, max_names=5)
    assert info["metadata_version"] == 24 and info["identifiers"] == [f"Name{i}" for i in range(5)] and info["identifiers_truncated"] is True
    assert "native code" in info["blocker"]
    bad = tmp_path / "bad" / "global-metadata.dat"
    bad.parent.mkdir()
    bad.write_bytes(b"\x00" * 300)                           # protected/encrypted metadata: header sanity fails
    d = sniff(bad)
    assert d.flags["metadata_header_invalid"] is True and d.confidence < 1
    with pytest.raises(ValueError, match="encrypted or obfuscated"):
        sp.inspect_il2cpp_metadata(bad)


def test_gamemaker_installation_profile(tmp_path):
    (tmp_path / "data.win").write_bytes(gamemaker_form(with_code=True, strings=["x"]))
    (tmp_path / "Runner.exe").write_bytes(mz_stub())
    prof = inventory_root(tmp_path, Limits())["profile"]
    assert prof["primary"] == "gamemaker" and prof["support_status"] == "detected_only"
    assert any(e["path"] == "data.win" for e in prof["evidence"]) and "UndertaleModTool" in json.dumps(prof["support"]["tools_candidate"])


# ---------------------------------------------------------------------------------------------------------------------
# Android / JVM detection
# ---------------------------------------------------------------------------------------------------------------------
def test_apk_detection_with_manifest_and_dex_evidence(tmp_path):
    d = sniff(MINAPP)
    assert (d.format, d.profile) == ("apk", "android") and d.flags["dex_files"] == 1 and d.support_status == "partial"
    assert d.evidence[0] == "AndroidManifest.xml" and "classes.dex" in d.evidence
    st = d.support["statement"]
    assert "code recovery needs jadx" in st.lower() and "manifest" in st and "native libraries" in st
    prof = inventory_root(_copy_into(tmp_path / "root", MINAPP), Limits())["profile"]
    assert prof["primary"] == "android" and prof["support_status"] == "partial"


def _copy_into(root: Path, src: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, root / src.name)
    return root


def test_aab_and_framework_hints(tmp_path):
    aab = tmp_path / "app.aab"
    with zipfile.ZipFile(aab, "w") as z:
        z.writestr("BundleConfig.pb", b"\x0a\x00")
        z.writestr("base/manifest/AndroidManifest.xml", b"\x0a\x00")
        z.writestr("base/dex/classes.dex", b"dex\n035\0" + b"\0" * 100)
    d = sniff(aab)
    assert (d.format, d.profile) == ("aab", "android") and d.flags["aab"] and d.flags["dex_files"] == 1
    flutter = tmp_path / "flutter.apk"
    with zipfile.ZipFile(flutter, "w") as z:
        z.writestr("AndroidManifest.xml", b"x")
        z.writestr("classes.dex", b"dex\n035\0" + b"\0" * 100)
        z.writestr("lib/arm64-v8a/libflutter.so", b"\x7fELF")
        z.writestr("lib/arm64-v8a/libapp.so", b"\x7fELF")
    fd = sniff(flutter)
    assert fd.flags["frameworks"] == ["flutter"] and fd.flags["abis"] == ["arm64-v8a"]
    assert "flutter" in fd.support["statement"] and "not Java" in fd.support["statement"]
    assert "protobuf" not in fd.support["statement"]
    ainfo = android_info.inspect_android_package(aab)
    assert ainfo["format"] == "aab" and ainfo["manifest"] is None and any("protobuf" in w for w in ainfo["warnings"])


def test_jar_class_and_jvm_installation_detection(tmp_path):
    d = sniff(JAVACLI)
    assert (d.format, d.profile) == ("jar", "jvm") and d.flags["main_class"] == "dev.rebuild.ledger.Main" and d.flags["class_count"] == 5
    assert d.support_status == "supported" and "CFR" in d.support["statement"]
    assert d.evidence[0] == "META-INF/MANIFEST.MF"
    inv = inventory_root(_copy_into(tmp_path / "app", JAVACLI), Limits())
    assert inv["profile"]["primary"] == "jvm" and "javacli.jar" in inv["modules"]
    notjar = tmp_path / "x.jar"
    notjar.write_bytes(b"PK\x03\x04garbage")
    nd = sniff(notjar)
    assert nd.profile == "jvm" and nd.confidence < 1 and "zip_error" in nd.flags
    plainzip = tmp_path / "data.zip"
    with zipfile.ZipFile(plainzip, "w") as z:
        z.writestr("readme.txt", "hi")
    assert sniff(plainzip).profile == "unknown"


def test_detect_path_carries_statement_for_every_tracked_kind(tmp_path):
    samples = {
        "m": macho_thin(), "data.win": gamemaker_form(with_code=False, strings=["a"]), "a.pak": pak_file(),
        "global-metadata.dat": il2cpp_metadata(["x"]),
    }
    for name, blob in samples.items():
        (tmp_path / name).write_bytes(blob)
    for name in samples:
        r = detect_path(tmp_path / name)
        assert r["support"]["statement"] and r["support_status"] in ("detected_only", "partial", "supported"), name
        assert r["support"]["blocker"] and r["support"]["next_action"], name         # unsupported kinds always say why and what next
    for p in (MINAPP, JAVACLI):
        assert detect_path(p)["support"]["statement"]


def test_every_status_is_valid_and_non_supported_kinds_name_a_blocker():
    for prof, rec in sp.SUPPORT.items():
        assert rec["status"] in sp.STATUS_LABEL, prof
        if rec["status"] != "supported":
            assert rec["blocker"] and rec["can_recover"] and rec["cannot_recover"], prof
        if rec["status"] in ("detected_only", "unsupported"):
            assert "None is produced from code" in rec["rebuild"], prof
            assert sp.FUTURE_TOOLS.get(prof), f"{prof}: candidate tools must be listed"
    for tools in sp.FUTURE_TOOLS.values():
        assert all(t["license"] and t["url"].startswith("https://") for t in tools)


# ---------------------------------------------------------------------------------------------------------------------
# triage backend
# ---------------------------------------------------------------------------------------------------------------------
def test_triage_probe_smoke_and_registry():
    b = TriageBackend()
    info = b.probe()
    assert info.experimental and info.availability == Availability.INSTALLED and set(info.profiles) == {"unity_il2cpp", "gamemaker", "unreal", "native_macho"}
    assert info.resources["recovers_code"] is False and {o.name for o in info.operations} == {"describe", "inspect"}
    assert b.smoke().availability == Availability.USABLE


def test_triage_describe_and_inspect_record_evidence_and_never_claim_recovery(tmp_path, studio, case, cases):
    b = TriageBackend()
    p = tmp_path / "global-metadata.dat"
    p.write_bytes(il2cpp_metadata(["Player", "Enemy"]))
    r = b.inspect(p, studio=studio, case_id=case["case_id"])
    assert r.ok and r.data["recovered"] is False and r.data["equivalence_claimed"] is False
    assert r.data["inspect"]["identifiers"] == ["Player", "Enemy"] and r.data["support_status"] == "detected_only"
    ev = cases.get_evidence(r.evidence_ids[0])
    body = cases.evidence_body(ev["evidence_id"])
    assert ev["kind"] == "triage.inspect" and body["module"]["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    d = b.describe(p, studio=studio, case_id=case["case_id"])
    assert d.ok and d.data["support"]["status"] == "detected_only" and d.data["recovered"] is False
    assert cases.get_evidence(d.evidence_ids[0])["kind"] == "triage.support"
    cap = b.inspect(p, max_names=1)             # unknown kwargs are ignored; bounded lists report truncation
    assert cap.ok


def test_triage_inspect_reports_parse_failure_instead_of_crashing(tmp_path):
    p = tmp_path / "global-metadata.dat"
    p.write_bytes(b"\0" * 400)
    r = TriageBackend().inspect(p)
    assert not r.ok and "encrypted or obfuscated" in r.error and r.data["recovered"] is False
    assert not TriageBackend().inspect(tmp_path / "missing").ok


def test_triage_inspect_each_kind(tmp_path):
    b = TriageBackend()
    (tmp_path / "m").write_bytes(macho_fat([macho_thin(0x01000007), macho_thin(0x0100000C)]))
    (tmp_path / "data.win").write_bytes(gamemaker_form(with_code=True, strings=["s"]))
    (tmp_path / "p.pak").write_bytes(pak_file())
    assert b.inspect(tmp_path / "m").data["inspect"]["architectures"] == ["arm64", "x86_64"]
    assert b.inspect(tmp_path / "data.win").data["inspect"]["has_code_chunk"] is True
    assert b.inspect(tmp_path / "p.pak").data["inspect"]["footer"]["pak_version"] == 8


def test_inspect_path_rejects_unknown_kinds(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world")
    with pytest.raises(ValueError, match="no inspector"):
        sp.inspect_path(p)


# ---------------------------------------------------------------------------------------------------------------------
# docs/SUPPORT_MATRIX.md must stay consistent with the code and the tests
# ---------------------------------------------------------------------------------------------------------------------
MATRIX = Path(__file__).resolve().parents[2] / "docs" / "SUPPORT_MATRIX.md"


def _matrix_rows() -> dict[str, list[str]]:
    text = MATRIX.read_text(encoding="utf-8").replace("\r\n", "\n")
    section = text.split("## Input kinds", 1)[1].split("### Java/Android", 1)[0]
    rows = {}
    for ln in section.splitlines():
        if ln.startswith("| ") and not ln.startswith("| Input kind") and not ln.startswith("|---"):
            cells = [c.strip() for c in ln.strip().strip("|").split(" | ")]
            rows[cells[0]] = cells
    return rows


def test_support_matrix_statuses_match_the_code():
    rows = _matrix_rows()
    want = {"Unity, Mono scripting backend": "unity_mono", "Unity, IL2CPP": "unity_il2cpp", "GameMaker": "gamemaker", "Unreal Engine": "unreal",
            "Mach-O (thin, fat/universal)": "native_macho", "Java / JVM (.jar, .class)": "jvm", "Android (.apk / .aab / .dex)": "android"}
    for kind, prof in want.items():
        status_cell = rows[kind][5]
        code_status = sp.SUPPORT[prof]["status"].replace("_", "-")
        assert status_cell.startswith(code_status), (kind, status_cell, code_status)


def test_support_matrix_cites_only_tests_that_exist():
    cell_text = " ".join(c[6] for c in _matrix_rows().values())
    cur_file = None
    missing = []
    for m in re.finditer(r"`(tests/[a-z_]+\.py)(::[a-zA-Z0-9_]+)?`|`(::[a-zA-Z0-9_]+)`", cell_text):
        if m.group(1):
            cur_file = Path(__file__).resolve().parents[1] / m.group(1)
            name = (m.group(2) or "")[2:]
        else:
            name = m.group(3)[2:]
        assert cur_file is not None and cur_file.is_file(), m.group(0)
        if name and not re.search(rf"^def {name}\(", cur_file.read_text(encoding="utf-8"), re.M):
            missing.append(f"{cur_file.name}::{name}")
    assert not missing, missing
