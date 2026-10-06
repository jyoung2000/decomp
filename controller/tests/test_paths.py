import os
import pytest
from pathlib import Path

from rebuild_controller.paths import PathPolicyError, RootSet, safe_archive_target, classify_entry, is_within


def test_rootset_rejects_overlap(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    with pytest.raises(PathPolicyError):
        RootSet(src, src / "out", tmp_path / "case").validate()
    with pytest.raises(PathPolicyError):
        RootSet(src, tmp_path / "out", tmp_path / "out" / "case").validate()
    RootSet(src, tmp_path / "out", tmp_path / "case").validate()


def test_rootset_rejects_symlinked_output_into_source(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    link = tmp_path / "outlink"
    os.symlink(src / "nested", link)
    with pytest.raises(PathPolicyError):
        RootSet(src, link, tmp_path / "case").validate()


def test_rootset_rejects_protected(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    with pytest.raises(PathPolicyError):
        RootSet(src, Path("/usr/share/x"), tmp_path / "case").validate()


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/etc/passwd", "C:\\win\\x", "..\\x", "//server/share"])
def test_archive_escapes_refused(tmp_path, name):
    with pytest.raises(PathPolicyError):
        safe_archive_target(tmp_path, name)


def test_archive_symlink_parent_escape_refused(tmp_path):
    root = tmp_path / "x"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "lnk")
    with pytest.raises(PathPolicyError):
        safe_archive_target(root, "lnk/evil.txt")
    assert safe_archive_target(root, "ok/./file.txt") == root.resolve() / "ok" / "file.txt"


def test_classify_entry(tmp_path):
    f = tmp_path / "f"; f.write_text("x")
    os.symlink(f, tmp_path / "l")
    os.link(f, tmp_path / "h")
    assert classify_entry(tmp_path / "l") == "symlink"
    assert classify_entry(tmp_path / "h") == "hardlink"
    assert classify_entry(tmp_path) == "dir"
    assert classify_entry(tmp_path / "missing") == "other"


def test_is_within_no_prefix_confusion(tmp_path):
    assert not is_within(Path("/a/bc"), Path("/a/b"))
    assert is_within(Path("/a/b/c"), Path("/a/b"))
