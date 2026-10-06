"""Archive listing/extraction: bounds, escape refusal, malformed input. asar is cross-checked against the real @electron/asar CLI."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from rebuild_controller.backends.archive import (ArchiveError, ArchiveLimitError, ArchiveLimits, AsarReader, asar_read_file,
                                                 detect_format, discover_executable, extract_archive, link_escapes, list_archive,
                                                 read_asar_header, verify_asar_integrity)
from rebuild_controller.backends.jsweb import build_asar
from rebuild_controller.config import Limits, Settings

DATA = Path(__file__).parent / "data" / "managed" / "js"
REAL_ASAR = DATA / "electron_app" / "resources" / "app.asar"
LIM = ArchiveLimits(max_entries=1000, max_expansion_bytes=10 * 1024 * 1024)


def outside_snapshot(parent: Path, keep: str) -> set[str]:
    return {p.name for p in parent.iterdir() if p.name != keep}


# ---------------------------------------------------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------------------------------------------------
def make_zip(path: Path, members: dict[str, bytes], *, deflate: bool = True, extra: list[tuple[zipfile.ZipInfo, bytes]] | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
        for n, d in members.items():
            z.writestr(n, d)
        for zi, d in extra or []:
            z.writestr(zi, d)
    return path


def make_tar(path: Path, members: list[tuple[str, bytes | None, dict]], mode: str = "w") -> Path:
    with tarfile.open(path, mode) as t:
        for name, data, kw in members:
            ti = tarfile.TarInfo(name)
            for k, v in kw.items():
                setattr(ti, k, v)
            if data is None:
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    return path


def asar_with_tree(path: Path, tree: dict, data: bytes = b"") -> Path:
    js = json.dumps(tree, separators=(",", ":")).encode()
    padded = js + b"\0" * ((-len(js)) % 4)
    payload = 4 + len(padded)
    path.write_bytes(struct.pack("<IIII", 4, payload + 4, payload, len(js)) + padded + data)
    return path


# ---------------------------------------------------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("deflate", [True, False])
def test_zip_roundtrip_listing_and_extract(tmp_path, deflate):
    members = {"a.txt": b"alpha", "dir/b.bin": bytes(range(256)) * 10, "dir/sub/c.txt": b"c" * 5000}
    z = make_zip(tmp_path / "t.zip", members, deflate=deflate)
    assert detect_format(z) == "zip"
    lst = list_archive(z, limits=LIM)
    assert lst.format == "zip" and not lst.truncated and lst.declared_entries == 3
    assert {m.name: m.size for m in lst.members} == {k: len(v) for k, v in members.items()}
    out = tmp_path / "out"
    rep = extract_archive(z, out, limits=LIM)
    assert rep.files_extracted == 3 and rep.refused_total == 0 and not rep.truncated and not rep.errors
    for k, v in members.items():
        assert (out / k).read_bytes() == v


def test_zip_slip_members_refused_and_nothing_escapes(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    members = {"ok.txt": b"fine", "../evil.txt": b"x", "/abs.txt": b"x", "..\\winslip.txt": b"x", "C:\\drive.txt": b"x",
               "a/../../deep.txt": b"x", "good/dir/file.txt": b"ok"}
    z = make_zip(work / "slip.zip", members)
    out = work / "out"
    before = outside_snapshot(work, "out")
    rep = extract_archive(z, out, limits=LIM)
    assert rep.refused_total == 5, rep.refused
    assert {r["name"] for r in rep.refused} == {"../evil.txt", "/abs.txt", "..\\winslip.txt", "C:\\drive.txt", "a/../../deep.txt"}
    assert (out / "ok.txt").read_bytes() == b"fine" and (out / "good/dir/file.txt").read_bytes() == b"ok"
    assert outside_snapshot(work, "out") == before            # nothing created next to the extraction root
    assert not (tmp_path / "evil.txt").exists() and not Path("/abs.txt").exists()
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == ["good/dir/file.txt", "ok.txt"]


def test_zip_symlink_members_never_materialised(tmp_path):
    esc = zipfile.ZipInfo("link_out")
    esc.create_system = 3
    esc.external_attr = (0o120777 << 16)
    inside = zipfile.ZipInfo("sub/link_in")
    inside.create_system = 3
    inside.external_attr = (0o120777 << 16)
    z = make_zip(tmp_path / "l.zip", {"sub/real.txt": b"r"}, extra=[(esc, b"/etc/passwd"), (inside, b"real.txt")])
    out = tmp_path / "out"
    rep = extract_archive(z, out, limits=LIM)
    assert [r["name"] for r in rep.refused] == ["link_out"]
    assert [s["name"] for s in rep.skipped] == ["sub/link_in"]
    assert not any(p.is_symlink() for p in out.rglob("*"))
    assert (out / "sub/real.txt").exists()


def test_zip_oversized_expansion_stops_before_exceeding_limit(tmp_path):
    z = make_zip(tmp_path / "big.zip", {"1.bin": b"a" * 1000, "2.bin": b"b" * 1000, "3.bin": b"c" * 1000})
    out = tmp_path / "out"
    rep = extract_archive(z, out, limits=ArchiveLimits(max_entries=100, max_expansion_bytes=2500))
    assert rep.truncated and "expansion limit" in rep.truncation_reason
    assert rep.bytes_written == 2000 and rep.bytes_written <= 2500
    assert sorted(p.name for p in out.iterdir()) == ["1.bin", "2.bin"]


def test_zip_bomb_high_ratio_member_is_not_written(tmp_path):
    z = make_zip(tmp_path / "bomb.zip", {"zeros.bin": b"\0" * (8 * 1024 * 1024)})
    assert z.stat().st_size < 20_000                          # tiny on disk, huge declared
    out = tmp_path / "out"
    rep = extract_archive(z, out, limits=ArchiveLimits(max_entries=100, max_expansion_bytes=1024 * 1024))
    assert rep.truncated and rep.bytes_written == 0 and rep.files_extracted == 0
    assert not (out / "zeros.bin").exists()


def test_zip_truncated_listing_flag_and_refused_extraction_for_huge_directory(tmp_path):
    z = make_zip(tmp_path / "many.zip", {f"f{i:03d}.txt": b"x" for i in range(50)})
    lst = list_archive(z, limits=ArchiveLimits(max_entries=10, max_expansion_bytes=10**9))
    assert lst.truncated and len(lst.members) == 10 and lst.declared_entries == 50
    assert lst.members[0].name == "f000.txt" and "50 entries" in lst.truncation_reason
    with pytest.raises(ArchiveLimitError):
        extract_archive(z, tmp_path / "out", limits=ArchiveLimits(max_entries=10, max_expansion_bytes=10**9))
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


@pytest.mark.parametrize("blob", [b"", b"not an archive at all" * 50, b"PK\x03\x04" + b"\0" * 10])
def test_malformed_archives_raise_archive_error(tmp_path, blob):
    p = tmp_path / "bad.bin"
    p.write_bytes(blob)
    with pytest.raises(ArchiveError):
        list_archive(p, limits=LIM)
    with pytest.raises(ArchiveError):
        extract_archive(p, tmp_path / "out", limits=LIM)


def test_truncated_zip_is_malformed(tmp_path):
    z = make_zip(tmp_path / "t.zip", {"a.txt": b"hello" * 100})
    data = z.read_bytes()
    z.write_bytes(data[: len(data) - 30])   # cut through the end-of-central-directory record
    with pytest.raises(ArchiveError):
        list_archive(z, limits=LIM)


def test_zip_crc_corruption_is_reported_and_partial_file_removed(tmp_path):
    z = make_zip(tmp_path / "c.zip", {"a.txt": b"payload" * 50, "b.txt": b"ok"}, deflate=False)
    data = bytearray(z.read_bytes())
    i = data.index(b"payload")
    data[i] ^= 0xFF
    z.write_bytes(bytes(data))
    out = tmp_path / "out"
    rep = extract_archive(z, out, limits=LIM)
    assert any("a.txt" in e for e in rep.errors)
    assert not (out / "a.txt").exists() and (out / "b.txt").read_bytes() == b"ok"


def test_extract_refuses_when_output_contains_archive(tmp_path):
    z = make_zip(tmp_path / "t.zip", {"a": b"1"})
    with pytest.raises(ArchiveError):
        extract_archive(z, tmp_path, limits=LIM)


# ---------------------------------------------------------------------------------------------------------------------
# tar
# ---------------------------------------------------------------------------------------------------------------------
def test_tar_roundtrip_gz_and_listing(tmp_path):
    t = make_tar(tmp_path / "t.tar.gz", [("d", None, {"type": tarfile.DIRTYPE}), ("d/x.txt", b"xx", {}), ("y.txt", b"yy", {})], mode="w:gz")
    assert detect_format(t) == "tar"
    lst = list_archive(t, limits=LIM)
    assert [(m.name, m.kind) for m in lst.members] == [("d", "dir"), ("d/x.txt", "file"), ("y.txt", "file")]
    out = tmp_path / "out"
    rep = extract_archive(t, out, limits=LIM)
    assert rep.files_extracted == 2 and (out / "d/x.txt").read_bytes() == b"xx"


def test_tar_slip_and_link_escapes_refused(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    members = [
        ("ok.txt", b"ok", {}),
        ("../evil.txt", b"x", {}),
        ("/abs_evil.txt", b"x", {}),
        ("sym_out", None, {"type": tarfile.SYMTYPE, "linkname": "/etc/passwd"}),
        ("sym_up", None, {"type": tarfile.SYMTYPE, "linkname": "../../outside"}),
        ("sub/sym_ok", None, {"type": tarfile.SYMTYPE, "linkname": "ok_target"}),
        ("hard_out", None, {"type": tarfile.LNKTYPE, "linkname": "/etc/shadow"}),
        ("hard_up", None, {"type": tarfile.LNKTYPE, "linkname": "../secret"}),
        ("dev", None, {"type": tarfile.CHRTYPE}),
    ]
    t = make_tar(work / "t.tar", members)
    out = work / "out"
    before = outside_snapshot(work, "out")
    rep = extract_archive(t, out, limits=LIM)
    assert {r["name"] for r in rep.refused} == {"../evil.txt", "/abs_evil.txt", "sym_out", "sym_up", "hard_out", "hard_up"}
    assert {s["name"] for s in rep.skipped} == {"sub/sym_ok", "dev"}
    assert not any(p.is_symlink() for p in out.rglob("*"))
    assert outside_snapshot(work, "out") == before
    assert [p.name for p in out.rglob("*") if p.is_file()] == ["ok.txt"]


def test_tar_entry_limit_truncates_listing_and_extraction(tmp_path):
    t = make_tar(tmp_path / "many.tar", [(f"f{i}.txt", b"x", {}) for i in range(50)])
    lim = ArchiveLimits(max_entries=10, max_expansion_bytes=10**9)
    lst = list_archive(t, limits=lim)
    assert lst.truncated and len(lst.members) == 10 and "more than 10 entries" in lst.truncation_reason
    rep = extract_archive(t, tmp_path / "out", limits=lim)
    assert rep.truncated and rep.entries_seen == 10 and rep.files_extracted == 10


def test_tar_expansion_limit(tmp_path):
    t = make_tar(tmp_path / "big.tar.gz", [("a", b"a" * 4000, {}), ("b", b"b" * 4000, {}), ("c", b"c" * 4000, {})], mode="w:gz")
    rep = extract_archive(t, tmp_path / "out", limits=ArchiveLimits(max_entries=100, max_expansion_bytes=9000))
    assert rep.truncated and rep.bytes_written == 8000 and rep.files_extracted == 2


def test_tar_truncated_stream_reports_error_but_keeps_what_it_read(tmp_path):
    t = make_tar(tmp_path / "t.tar.gz", [("a.txt", b"a" * 100, {}), ("b.txt", os.urandom(200_000), {})], mode="w:gz")
    data = t.read_bytes()
    t.write_bytes(data[: len(data) // 2])
    rep = extract_archive(t, tmp_path / "out", limits=LIM)
    assert rep.truncated and rep.errors
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"a" * 100


def test_tar_garbage_is_malformed(tmp_path):
    p = tmp_path / "g.tar.gz"
    p.write_bytes(gzip.compress(b"this is not a tar stream" * 100))
    with pytest.raises(ArchiveError):
        list_archive(p, limits=LIM, format="tar")


# ---------------------------------------------------------------------------------------------------------------------
# link policy
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,target,hard,expect", [
    ("a/l", "b", False, False), ("a/l", "../b", False, False), ("a/l", "../../b", False, True), ("l", "..", False, True),
    ("l", "/etc/passwd", False, True), ("l", "C:\\x", False, True), ("a/h", "a/x", True, False), ("a/h", "../x", True, True),
    ("l", "", False, True),
])
def test_link_escapes(name, target, hard, expect):
    assert link_escapes(name, target, hardlink=hard) is expect


def test_existing_symlink_destination_and_parent_redirect_refused(tmp_path):
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "precious.txt"
    victim.write_text("keep me")
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(victim, out / "a.txt")                   # destination file is a symlink
    os.symlink(victim_dir, out / "sub")                 # parent directory is a symlink
    z = make_zip(tmp_path / "t.zip", {"a.txt": b"overwritten", "sub/precious.txt": b"overwritten", "fine.txt": b"ok"})
    rep = extract_archive(z, out, limits=LIM)
    assert victim.read_text() == "keep me"
    assert {r["name"] for r in rep.refused} == {"a.txt", "sub/precious.txt"}
    assert (out / "fine.txt").read_bytes() == b"ok"


# ---------------------------------------------------------------------------------------------------------------------
# asar (native) vs the real asar CLI
# ---------------------------------------------------------------------------------------------------------------------
def real_asar_cli() -> Path | None:
    return discover_executable(Settings(), ["asar"], subdirs=["asar"])


def test_asar_listing_matches_real_cli():
    cli = real_asar_cli()
    if cli is None:
        pytest.skip("@electron/asar CLI not installed")
    r = subprocess.run([str(cli), "list", str(REAL_ASAR)], capture_output=True, text=True, check=True, timeout=60)
    cli_paths = {ln.strip().lstrip("/") for ln in r.stdout.splitlines() if ln.strip()}
    lst = list_archive(REAL_ASAR, limits=LIM)
    native = {m.name for m in lst.members}
    assert native == cli_paths and lst.format == "asar" and not lst.truncated and lst.declared_entries == len(cli_paths)
    assert sum(1 for m in lst.members if m.unpacked) == 1


def test_asar_extraction_is_byte_identical_to_real_cli(tmp_path):
    cli = real_asar_cli()
    if cli is None:
        pytest.skip("@electron/asar CLI not installed")
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    # work on a copy so the .unpacked sibling lookup is also exercised
    shutil.copytree(REAL_ASAR.parent, tmp_path / "res")
    rep = extract_archive(tmp_path / "res" / "app.asar", ours, limits=LIM)
    subprocess.run([str(cli), "extract", str(tmp_path / "res" / "app.asar"), str(theirs)], check=True, timeout=60)
    assert rep.files_extracted == 12 and not rep.errors and not rep.refused
    d = subprocess.run(["diff", "-r", str(ours), str(theirs)], capture_output=True, text=True)
    assert d.returncode == 0, d.stdout


def test_asar_unpacked_payload_missing_is_reported_not_invented(tmp_path):
    shutil.copy(REAL_ASAR, tmp_path / "app.asar")     # deliberately without app.asar.unpacked
    rep = extract_archive(tmp_path / "app.asar", tmp_path / "out", limits=LIM)
    assert any(s["name"] == "native/addon.node" and "unpacked payload not found" in s["reason"] for s in rep.skipped)
    assert not (tmp_path / "out/native/addon.node").exists()


def test_asar_read_helpers_and_integrity(tmp_path):
    assert json.loads(asar_read_file(REAL_ASAR, "package.json"))["name"] == "tiny-electron"
    assert asar_read_file(REAL_ASAR, "native/addon.node") is None          # unpacked
    assert asar_read_file(REAL_ASAR, "main.js", max_bytes=5) is None        # over the bound
    with AsarReader(REAL_ASAR) as r:
        assert r.read("main.js", 5) == b"const" and r.read("missing") is None
    v = verify_asar_integrity(REAL_ASAR)
    assert v["checked"] >= 10 and v["mismatched"] == 0
    # tamper with a packed byte of a synthetic asar that carries integrity records
    p = tmp_path / "i.asar"
    p.write_bytes(build_asar({"a.txt": b"AAAA" * 10, "b.txt": b"BBBB" * 10}, with_integrity=True))
    assert verify_asar_integrity(p) == {"checked": 2, "mismatched": 0, "mismatched_names": []}
    raw = bytearray(p.read_bytes())
    raw[-3] ^= 0xFF
    p.write_bytes(bytes(raw))
    v = verify_asar_integrity(p)
    assert v["mismatched"] == 1 and v["mismatched_names"] == ["b.txt"]


def test_asar_slip_member_names_refused(tmp_path):
    tree = {"files": {"..": {"files": {"evil.txt": {"size": 4, "offset": "0"}}},
                      "ok.txt": {"size": 4, "offset": "0"},
                      "a": {"files": {"..": {"files": {"..": {"files": {"deep.txt": {"size": 4, "offset": "0"}}}}}}}}}
    work = tmp_path / "w"
    work.mkdir()
    a = asar_with_tree(work / "s.asar", tree, b"DATA")
    out = work / "out"
    before = outside_snapshot(work, "out")
    rep = extract_archive(a, out, limits=LIM)
    assert (out / "ok.txt").read_bytes() == b"DATA"
    assert rep.refused_total >= 2 and any("escapes" in r["reason"] for r in rep.refused)
    assert outside_snapshot(work, "out") == before and not (tmp_path / "evil.txt").exists()
    assert [p.name for p in out.rglob("*") if p.is_file()] == ["ok.txt"]


def test_asar_symlink_member_escape_refused(tmp_path):
    tree = {"files": {"l": {"link": "../../etc/passwd"}, "m": {"link": "ok.txt"}, "ok.txt": {"size": 2, "offset": "0"}}}
    a = asar_with_tree(tmp_path / "l.asar", tree, b"hi")
    rep = extract_archive(a, tmp_path / "out", limits=LIM)
    assert [r["name"] for r in rep.refused] == ["l"] and [s["name"] for s in rep.skipped] == ["m"]
    assert not any(p.is_symlink() for p in (tmp_path / "out").rglob("*"))


def test_asar_payload_out_of_range_is_skipped_and_listed_as_error(tmp_path):
    tree = {"files": {"ok.txt": {"size": 2, "offset": "0"}, "far.txt": {"size": 100, "offset": "50"}}}
    a = asar_with_tree(tmp_path / "r.asar", tree, b"hi")
    lst = list_archive(a, limits=LIM)
    assert any("far.txt" in e for e in lst.errors)
    rep = extract_archive(a, tmp_path / "out", limits=LIM)
    assert (tmp_path / "out/ok.txt").read_bytes() == b"hi"
    assert any(s["name"] == "far.txt" and "beyond end" in s["reason"] for s in rep.skipped)


def test_asar_entry_limit_truncated_flag(tmp_path):
    a = tmp_path / "many.asar"
    a.write_bytes(build_asar({f"d{i % 3}/f{i:02d}.txt": b"x" for i in range(30)}))
    lst = list_archive(a, limits=ArchiveLimits(max_entries=10, max_expansion_bytes=10**9))
    assert lst.truncated and len(lst.members) == 10 and "more than 10 entries" in lst.truncation_reason
    rep = extract_archive(a, tmp_path / "out", limits=ArchiveLimits(max_entries=10, max_expansion_bytes=10**9))
    assert rep.truncated and rep.entries_seen == 10


def test_asar_expansion_limit(tmp_path):
    a = tmp_path / "e.asar"
    a.write_bytes(build_asar({"1": b"a" * 1000, "2": b"b" * 1000, "3": b"c" * 1000}))
    rep = extract_archive(a, tmp_path / "out", limits=ArchiveLimits(max_entries=100, max_expansion_bytes=2500))
    assert rep.truncated and rep.bytes_written == 2000


def test_asar_malformed_variants(tmp_path):
    good = REAL_ASAR.read_bytes()
    cases = {
        "empty": b"",
        "short": good[:10],
        "bad_prefix": struct.pack("<I", 7) + good[4:],
        "header_past_eof": good[:4] + struct.pack("<I", 10**9) + good[8:],
        "inconsistent_sizes": good[:8] + struct.pack("<I", 5) + good[12:],
        "json_truncated": good[:60],
    }
    for name, blob in cases.items():
        p = tmp_path / f"{name}.asar"
        p.write_bytes(blob)
        with pytest.raises(ArchiveError):
            list_archive(p, limits=LIM, format="asar")
        with pytest.raises(ArchiveError):
            extract_archive(p, tmp_path / f"o_{name}", limits=LIM, format="asar")


def test_asar_bad_json_and_hostile_nesting(tmp_path):
    def raw_asar(js: bytes) -> bytes:
        padded = js + b"\0" * ((-len(js)) % 4)
        payload = 4 + len(padded)
        return struct.pack("<IIII", 4, payload + 4, payload, len(js)) + padded
    p = tmp_path / "junk.asar"
    p.write_bytes(raw_asar(b"{not json"))
    with pytest.raises(ArchiveError):
        read_asar_header(p)
    p.write_bytes(raw_asar(b'{"nofiles": 1}'))
    with pytest.raises(ArchiveError):
        read_asar_header(p)
    # moderately deep trees are walked iteratively (no recursion in our code) and bounded by max_entries
    moderate = b'{"files":{"d":' * 500 + b'{"size":1,"offset":"0"}' + b"}}" * 500
    p.write_bytes(raw_asar(moderate))
    a = read_asar_header(p, ArchiveLimits(max_entries=100, max_expansion_bytes=10**6))
    assert a.truncated and len(a.members) == 100
    # absurd nesting trips the JSON parser's recursion guard; it must surface as ArchiveError, never RecursionError
    p.write_bytes(raw_asar(b'{"files":' * 400_000 + b"{}" + b"}" * 400_000))
    with pytest.raises(ArchiveError):
        read_asar_header(p)


def test_asar_invalid_node_fields_reported(tmp_path):
    tree = {"files": {"neg.txt": {"size": -5, "offset": "0"}, "str.txt": {"size": "x", "offset": "0"}, "noff.txt": {"size": 1, "offset": "zz"},
                      "ok.txt": {"size": 2, "offset": "0"}, "notobj": 5}}
    a = asar_with_tree(tmp_path / "f.asar", tree, b"hi")
    lst = list_archive(a, limits=LIM)
    assert [m.name for m in lst.members] == ["ok.txt"] and len(lst.errors) == 4


def test_limits_coerce_from_config_limits():
    lim = ArchiveLimits.coerce(Limits(max_archive_entries=7, max_archive_expansion_bytes=99))
    assert (lim.max_entries, lim.max_expansion_bytes) == (7, 99)
    assert ArchiveLimits.coerce(None).max_entries == Limits().max_archive_entries


def test_detect_format_by_content(tmp_path):
    z = make_zip(tmp_path / "noext", {"a": b"1"})
    assert detect_format(z) == "zip"
    assert detect_format(REAL_ASAR) == "asar"
    renamed = tmp_path / "renamed.bin"
    shutil.copy(REAL_ASAR, renamed)
    assert detect_format(renamed) == "asar"       # JSON header start is the second signal when the extension lies
    assert detect_format(tmp_path / "missing") is None
    (tmp_path / "t.txt").write_text("hello")
    assert detect_format(tmp_path / "t.txt") is None
