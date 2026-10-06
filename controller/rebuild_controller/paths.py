"""Path safety: final-path resolution, overlap checks, archive escape refusal.

Rules (enforced on the *resolved final path*, never on lexical prefixes alone):
- Source root, output root, case storage and app install dir must not overlap.
- Output destinations may never resolve inside the source root.
- Archive members may never resolve outside their extraction root (covers ../, absolute, drive-relative, links).
- Links inside the source tree are inventoried but never followed outside the root.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


class PathPolicyError(ValueError):
    pass


def resolve_final(p: Path | str) -> Path:
    """Resolve symlinks/junctions to the final path. On Windows this also normalises long-path prefixes."""
    path = Path(p)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as e:  # loops, permission
        raise PathPolicyError(f"cannot resolve {path}: {e}") from e
    if os.name == "nt":
        s = str(resolved)
        if s.startswith("\\\\?\\UNC\\"):
            resolved = Path("\\\\" + s[8:])
        elif s.startswith("\\\\?\\"):
            resolved = Path(s[4:])
    return resolved


def _norm_for_compare(p: Path) -> str:
    s = str(p)
    if os.name == "nt":
        s = s.lower()  # NTFS default case folding
    return s.rstrip("\\/") or s


def is_within(child: Path, parent: Path) -> bool:
    c, p = _norm_for_compare(child), _norm_for_compare(parent)
    if c == p:
        return True
    sep = "\\" if os.name == "nt" else "/"
    return c.startswith(p + sep)


def overlaps(a: Path, b: Path) -> bool:
    return is_within(a, b) or is_within(b, a)


@dataclass(frozen=True)
class RootSet:
    source_root: Path
    output_root: Path
    case_root: Path
    install_root: Path | None = None

    def validate(self) -> None:
        src = resolve_final(self.source_root)
        out = resolve_final(self.output_root)
        case = resolve_final(self.case_root)
        pairs = [("source", src, "output", out), ("source", src, "case storage", case), ("output", out, "case storage", case)]
        if self.install_root is not None:
            inst = resolve_final(self.install_root)
            pairs += [("install", inst, "source", src), ("install", inst, "output", out), ("install", inst, "case storage", case)]
        for an, a, bn, b in pairs:
            if overlaps(a, b):
                raise PathPolicyError(f"{an} directory {a} overlaps {bn} directory {b}; refusing")
        if not src.exists():
            raise PathPolicyError(f"source root does not exist: {src}")
        if not src.is_dir():
            raise PathPolicyError(f"source root is not a directory: {src}")
        _reject_protected(out)


_PROTECTED_POSIX = ("/", "/usr", "/bin", "/sbin", "/etc", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev")


def _windows_protected_roots() -> tuple[list[str], list[str]]:
    """Return (trees, exact): trees are protected with everything below; exact are protected only as themselves
    (a drive root or the user profile directory itself - subfolders of the profile are legitimate output locations)."""
    trees: list[str] = []
    exact: list[str] = []
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(var)
        if v:
            trees.append(_norm_for_compare(Path(v)))
    sysdrive = os.environ.get("SystemDrive", "C:")
    for d in {sysdrive.lower().rstrip("\\/"), "c:"}:
        trees += [d + "\\windows", d + "\\program files", d + "\\program files (x86)", d + "\\programdata"]
    home = os.environ.get("USERPROFILE")
    if home:
        exact.append(_norm_for_compare(Path(home)))
    users = os.environ.get("PUBLIC")
    if users:
        exact.append(_norm_for_compare(Path(users).parent))   # the Users directory itself
    return sorted(set(trees)), sorted(set(exact))


def _reject_protected(out: Path) -> None:
    s = _norm_for_compare(out)
    if os.name == "nt":
        trees, exact = _windows_protected_roots()
        drive = PureWindowsPath(str(out)).anchor.lower()
        is_drive_root = bool(drive) and s == _norm_for_compare(Path(drive)) or (len(s) == 2 and s[1] == ":")
        if is_drive_root or s in exact or any(s == p or s.startswith(p + "\\") for p in trees):
            raise PathPolicyError(f"output destination {out} is a protected system location")
    else:
        if s in _PROTECTED_POSIX or any(s.startswith(p + "/") for p in _PROTECTED_POSIX if p != "/"):
            raise PathPolicyError(f"output destination {out} is inside a protected system directory")


def safe_archive_target(extract_root: Path, member_name: str) -> Path:
    """Compute the on-disk path for an archive member, refusing escapes.

    Handles POSIX and Windows member names, absolute paths, drive letters, and `..` segments.
    The parent of the target is resolved *on disk* so a previously extracted symlink cannot redirect writes.
    """
    name = member_name.replace("\\", "/")
    if not name or name.startswith("/") or PureWindowsPath(member_name).drive or name.startswith("//"):
        raise PathPolicyError(f"archive member has absolute/drive path: {member_name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathPolicyError(f"archive member escapes extraction root: {member_name!r}")
    if os.name == "nt":
        for p in parts:
            if p.rstrip(". ") != p or any(ch in p for ch in '<>:"|?*'):
                raise PathPolicyError(f"archive member has a name Windows cannot represent safely: {member_name!r}")
    root = resolve_final(extract_root)
    target = root.joinpath(*parts)
    # Resolve the deepest existing ancestor and ensure it is still inside root (link-escape check).
    anc = target.parent
    while not anc.exists() and anc != anc.parent:
        anc = anc.parent
    if not is_within(resolve_final(anc), root):
        raise PathPolicyError(f"archive member parent resolves outside extraction root: {member_name!r}")
    return target


def classify_entry(path: Path) -> str:
    """Return one of: file|dir|symlink|junction|hardlink|reparse|other without following links."""
    try:
        st = os.lstat(path)
    except OSError:
        return "other"
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        if os.name == "nt" and getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            # Python reports junctions as symlinks on Windows (3.12+ exposes is_junction)
            if hasattr(path, "is_junction") and path.is_junction():
                return "junction"
        return "symlink"
    if os.name == "nt" and getattr(st, "st_file_attributes", 0) & 0x400:
        return "reparse"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "hardlink" if st.st_nlink > 1 else "file"
    return "other"


def assert_output_not_in_source(output: Path, source: Path) -> None:
    if is_within(resolve_final(output), resolve_final(source)):
        raise PathPolicyError(f"output {output} resolves inside protected source {source}")
