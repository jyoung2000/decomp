import os
import subprocess
from pathlib import Path

import pytest

from rebuild_controller.backends.detect import sniff, summarize_profile, embedded_pck_offset
from rebuild_controller.backends.inventory import inventory_root, build_dependency_graph
from rebuild_controller.config import Limits


def _mk_tree(tmp_path: Path) -> Path:
    root = tmp_path / "app"; root.mkdir()
    (root / "readme.txt").write_text("hello")
    (root / "data.bin").write_bytes(bytes(range(256)) * 4)
    (root / "sub").mkdir(); (root / "sub" / "script.js").write_text("const __webpack_require__ = 1;\n//# sourceMappingURL=x.map")
    (root / "game.pck").write_bytes(b"GDPC" + (2).to_bytes(4, "little") + (4).to_bytes(4, "little") + (3).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\0" * 100)
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "secret.txt").write_text("s")
    os.symlink(outside, root / "escape_dir")
    os.symlink(outside / "secret.txt", root / "escape_file")
    os.link(root / "readme.txt", root / "readme_hard.txt")
    return root


def test_inventory_records_links_without_following(tmp_path):
    root = _mk_tree(tmp_path)
    inv = inventory_root(root, Limits())
    paths = {f["path"] for f in inv["files"]}
    assert "escape_dir/secret.txt" not in paths and "escape_file" not in paths
    kinds = {(l["path"], l["kind"]) for l in inv["links"]}
    assert ("escape_dir", "symlink") in kinds and ("escape_file", "symlink") in kinds
    assert all(l["followed"] is False for l in inv["links"])
    hard = [f for f in inv["files"] if f["path"] in ("readme.txt", "readme_hard.txt")]
    assert all(f["kind"] == "hardlink" for f in hard) and any(f.get("hardlink_duplicate") for f in hard)
    assert "game.pck" in inv["modules"]
    assert inv["profile"]["primary"] == "godot"
    assert any("links" in s for s in inv["unknown_scope"])


def test_inventory_truncation_is_visible(tmp_path):
    root = tmp_path / "many"; root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.txt").write_text("x")
    inv = inventory_root(root, Limits(max_inventory_files=10))
    assert inv["truncated"] and inv["file_count"] == 10 and inv["truncation_limit"] == 10
    assert any("truncated" in s for s in inv["unknown_scope"])


def test_inventory_inaccessible_disclosed(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    f = root / "locked.bin"; f.write_bytes(b"MZ" + b"\0" * 100)
    if os.name == "nt":
        # Windows has no chmod 000: deny read to the current user via an ACL (removed in finally so tmp cleanup works).
        who = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(f), "/deny", f"{who}:(R)"], check=True, capture_output=True)
        try:
            inv = inventory_root(root, Limits())
        finally:
            subprocess.run(["icacls", str(f), "/remove:d", who], capture_output=True)
    else:
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        f.chmod(0)
        inv = inventory_root(root, Limits())
    assert any("unreadable" in s["reason"] for s in inv["skipped"])


def test_detect_pe_and_dependency_graph(tmp_path):
    exe = tmp_path / "app" / "a.exe"; exe.parent.mkdir()
    # The committed pecli fixture is a real mingw-built x64 console PE: no cross-compiler needed on any host.
    import shutil
    shutil.copy2(Path(__file__).resolve().parents[2] / "fixtures" / "pecli" / "original" / "pecli.exe", exe)
    d = sniff(exe)
    assert d.format == "pe" and d.profile == "native_pe" and d.arch == "x86_64" and d.bits == 64
    assert d.flags["subsystem"] == "console" and "kernel32.dll" in d.flags["imports"]
    inv = inventory_root(exe.parent, Limits())
    assert inv["profile"]["primary"] == "native_pe"
    g = build_dependency_graph(inv)
    assert "kernel32.dll" in g["external"]["a.exe"]


def test_detect_malformed_pe_low_confidence(tmp_path):
    p = tmp_path / "bad.exe"; p.write_bytes(b"MZ" + b"\xff" * 300)
    d = sniff(p)
    assert d.format == "pe" and d.confidence < 1 and "parse_error" in d.flags


def test_detect_elf_and_js_and_profiles(tmp_path):
    elf = tmp_path / "x"; elf.write_bytes(b"\x7fELF\x02\x01\x01" + b"\0" * 9 + (2).to_bytes(2, "little") + (0x3E).to_bytes(2, "little") + b"\0" * 40)
    d = sniff(elf); assert d.format == "elf" and d.arch == "x86_64" and d.flags["type"] == "exec"
    js = tmp_path / "b.js"; js.write_text("webpackChunk=1")
    assert sniff(js).flags["webpack"]
    assert summarize_profile([("GameAssembly.dll", sniff(elf)), ("x", sniff(js))])["primary"] == "unity_il2cpp"


def test_embedded_pck_offset(tmp_path):
    p = tmp_path / "game.exe"
    pck = b"GDPC" + b"\0" * 60
    p.write_bytes(b"MZ" + b"\0" * 50 + pck + len(pck).to_bytes(8, "little") + b"GDPC")
    assert embedded_pck_offset(p) == 52
    q = tmp_path / "plain.exe"; q.write_bytes(b"MZ" + b"\0" * 50)
    assert embedded_pck_offset(q) is None
