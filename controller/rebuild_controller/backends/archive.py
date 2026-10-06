"""Bounded, escape-safe archive listing/extraction (zip, tar, asar) plus helpers shared by the recovery backends.

Design rules (all enforced here, never left to callers):
- Every member path goes through ``paths.safe_archive_target``; escapes (``..``, absolute, drive, link redirection) are
  *refused and reported*, never written.
- Symlink/hardlink members are never materialised. Links whose target resolves outside the archive root are refused;
  links that stay inside are recorded as skipped. Nothing is ever written through an existing symlink.
- ``max_entries`` and ``max_expansion_bytes`` (``config.Limits.max_archive_entries`` / ``max_archive_expansion_bytes``)
  bound listing and extraction. Hitting a bound sets ``truncated`` with a reason; it is never silent.
- Malformed archives raise ``ArchiveError`` (backends turn that into a failed OperationResult, not a crash).
- asar is parsed natively (no node needed): ``[u32=4][u32 header_size][u32 payload][u32 json_len][json]`` then the data
  region starts at ``8 + header_size``; file offsets in the JSON are relative to that region.

The bottom of the module holds small helpers shared by ilspy.py / gdre.py / jsweb.py (tool discovery, bounded subprocess
fallback, evidence recording, a ToolProbe subclass that carries ``next_action``).
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import stat
import struct
import subprocess
import tarfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator

from ..adapters.contract import Availability, BackendInfo, ToolProbe
from ..config import Limits, Settings
from ..paths import PathPolicyError, is_within, resolve_final, safe_archive_target

CHUNK = 1 << 16
MAX_ASAR_HEADER_BYTES = 64 * 1024 * 1024
MAX_LINK_TARGET_BYTES = 4096
REPORT_LIST_CAP = 200          # cap for skipped/refused/error lists carried in reports


class ArchiveError(Exception):
    """Malformed or unreadable archive."""


class ArchiveLimitError(ArchiveError):
    """A bound was hit in a mode that cannot produce a partial result."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 200_000
    max_expansion_bytes: int = 4 * 1024 * 1024 * 1024

    @classmethod
    def coerce(cls, v: "ArchiveLimits | Limits | None") -> "ArchiveLimits":
        if v is None:
            d = Limits()
            return cls(d.max_archive_entries, d.max_archive_expansion_bytes)
        if isinstance(v, ArchiveLimits):
            return v
        return cls(int(v.max_archive_entries), int(v.max_archive_expansion_bytes))


@dataclass
class ArchiveMember:
    name: str
    size: int
    kind: str                      # file | dir | symlink | other
    link_target: str | None = None
    unpacked: bool = False         # asar: payload lives in <archive>.unpacked
    offset: int | None = None      # asar: offset inside the data region
    executable: bool = False
    integrity: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "size": self.size, "kind": self.kind}
        if self.link_target is not None:
            d["link_target"] = self.link_target
        if self.unpacked:
            d["unpacked"] = True
        return d


@dataclass
class ArchiveListing:
    format: str
    path: str
    members: list[ArchiveMember] = field(default_factory=list)
    entries_seen: int = 0              # entries actually inspected
    declared_entries: int | None = None  # entries the container says it holds (None when unknown, e.g. tar)
    truncated: bool = False
    truncation_reason: str | None = None
    total_size: int = 0                # sum of listed file sizes
    errors: list[str] = field(default_factory=list)

    def to_dict(self, max_members: int | None = None) -> dict[str, Any]:
        ms = self.members if max_members is None else self.members[:max_members]
        return {"format": self.format, "path": self.path, "entries_seen": self.entries_seen,
                "declared_entries": self.declared_entries, "truncated": self.truncated,
                "truncation_reason": self.truncation_reason, "total_size": self.total_size,
                "errors": self.errors[:REPORT_LIST_CAP], "members": [m.to_dict() for m in ms],
                "members_omitted": max(0, len(self.members) - len(ms))}


@dataclass
class ExtractionReport:
    format: str
    archive: str
    out_dir: str
    files_extracted: int = 0
    dirs_created: int = 0
    bytes_written: int = 0
    entries_seen: int = 0
    declared_entries: int | None = None
    skipped: list[dict[str, str]] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    skipped_total: int = 0
    refused_total: int = 0
    truncated: bool = False
    truncation_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    extracted_names: list[str] = field(default_factory=list)   # bounded; names of files actually written

    def skip(self, name: str, reason: str) -> None:
        self.skipped_total += 1
        if len(self.skipped) < REPORT_LIST_CAP:
            self.skipped.append({"name": name, "reason": reason})

    def refuse(self, name: str, reason: str) -> None:
        self.refused_total += 1
        if len(self.refused) < REPORT_LIST_CAP:
            self.refused.append({"name": name, "reason": reason})

    def error(self, msg: str) -> None:
        if len(self.errors) < REPORT_LIST_CAP:
            self.errors.append(msg)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "extracted_names"}
        d["names_recorded"] = len(self.extracted_names)
        return d


# ---------------------------------------------------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------------------------------------------------
def detect_format(path: Path | str) -> str | None:
    """Return 'zip' | 'tar' | 'asar' | None by content (extension is only a tie-breaker for tar)."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(560)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if len(head) >= 16 and struct.unpack_from("<I", head, 0)[0] == 4 and p.suffix.lower() == ".asar":
        return "asar"
    if len(head) >= 16 and struct.unpack_from("<I", head, 0)[0] == 4 and head[16:18] == b'{"' and size > 16:
        return "asar"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    if head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00":
        try:
            with tarfile.open(p, "r|*") as tf:
                tf.next()
            return "tar"
        except (tarfile.TarError, OSError, EOFError, zlib.error):
            return None
    return None


# ---------------------------------------------------------------------------------------------------------------------
# link policy
# ---------------------------------------------------------------------------------------------------------------------
def link_escapes(member_name: str, target: str, *, hardlink: bool = False) -> bool:
    """True when a symlink/hardlink member would resolve outside the archive root."""
    t = target.replace("\\", "/")
    if not t or t.startswith("/") or PureWindowsPath(target).drive:
        return True
    base = "" if hardlink else posixpath.dirname(member_name.replace("\\", "/"))
    norm = posixpath.normpath(posixpath.join(base, t))
    return norm == ".." or norm.startswith("../") or norm.startswith("/")


# ---------------------------------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------------------------------
class _Sink:
    """Shared write path: escape check, no-follow create, running byte budget."""

    def __init__(self, out_dir: Path, limits: ArchiveLimits, report: ExtractionReport):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.root = resolve_final(self.out_dir)
        self.limits = limits
        self.report = report
        self.budget_hit = False

    def target(self, name: str) -> Path | None:
        try:
            return safe_archive_target(self.root, name)
        except PathPolicyError as e:
            self.report.refuse(name, str(e))
            return None

    def mkdir(self, name: str) -> bool:
        t = self.target(name)
        if t is None:
            return False
        try:
            t.mkdir(parents=True, exist_ok=True)
        except (OSError, FileExistsError, NotADirectoryError) as e:
            self.report.error(f"{name}: cannot create directory: {e}")
            return False
        if not is_within(resolve_final(t), self.root):
            self.report.refuse(name, "directory resolves outside extraction root after creation")
            return False
        self.report.dirs_created += 1
        return True

    def would_exceed(self, declared: int) -> bool:
        return self.report.bytes_written + max(0, declared) > self.limits.max_expansion_bytes

    def write(self, name: str, chunks: Iterator[bytes], declared: int) -> bool:
        t = self.target(name)
        if t is None:
            return False
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, FileExistsError, NotADirectoryError) as e:
            self.report.error(f"{name}: cannot create parent directory: {e}")
            return False
        if not is_within(resolve_final(t.parent), self.root):
            self.report.refuse(name, "parent resolves outside extraction root")
            return False
        if t.is_symlink():
            self.report.refuse(name, "destination is an existing symlink")
            return False
        if t.is_dir():
            self.report.error(f"{name}: a directory already exists at the destination")
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        written = 0
        try:
            fd = os.open(t, flags, 0o644)
        except OSError as e:
            self.report.error(f"{name}: cannot create file: {e}")
            return False
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in chunks:
                    if self.report.bytes_written + written + len(chunk) > self.limits.max_expansion_bytes:
                        raise _BudgetExceeded()
                    out.write(chunk)
                    written += len(chunk)
        except _BudgetExceeded:
            self.budget_hit = True
            _unlink_quiet(t)
            return False
        except (OSError, zipfile.BadZipFile, zlib.error, EOFError, tarfile.TarError, ArchiveError) as e:
            _unlink_quiet(t)
            self.report.error(f"{name}: {type(e).__name__}: {e}")
            return False
        self.report.bytes_written += written
        self.report.files_extracted += 1
        if len(self.report.extracted_names) < 100_000:
            self.report.extracted_names.append(name)
        return True


class _BudgetExceeded(Exception):
    pass


SafeWriter = _Sink  # public name for backends that write recovered files (escape-checked, no-follow, byte-budgeted)


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def read_chunks(f, n: int | None = None) -> Iterator[bytes]:
    left = n
    while True:
        want = CHUNK if left is None else min(CHUNK, left)
        if want <= 0:
            return
        b = f.read(want)
        if not b:
            return
        if left is not None:
            left -= len(b)
        yield b


# ---------------------------------------------------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------------------------------------------------
def _zip_eocd(f, size: int) -> tuple[int, int, int]:
    """Return (declared_entries, central_dir_offset, central_dir_size), zip64-aware."""
    tail_len = min(size, 65557)
    f.seek(size - tail_len)
    tail = f.read(tail_len)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) - i < 22:
        raise ArchiveError("not a valid zip: end-of-central-directory record not found")
    _sig, _d, _cd, _n_disk, n_total, cd_size, cd_off, _clen = struct.unpack_from("<4sHHHHIIH", tail, i)
    if n_total == 0xFFFF or cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc = tail.rfind(b"PK\x06\x07", 0, i)
        if loc >= 0 and len(tail) - loc >= 20:
            z64_off = struct.unpack_from("<Q", tail, loc + 8)[0]
            if z64_off + 56 <= size:
                f.seek(z64_off)
                rec = f.read(56)
                if rec[:4] == b"PK\x06\x06":
                    n_total, cd_size, cd_off = struct.unpack_from("<QQQ", rec, 32)
    if cd_off + cd_size > size:
        raise ArchiveError("malformed zip: central directory extends past end of file")
    return int(n_total), int(cd_off), int(cd_size)


def _zip_member_from_info(zi: zipfile.ZipInfo) -> tuple[str, str, str | None]:
    mode = (zi.external_attr >> 16) & 0o170000
    if zi.is_dir():
        return "dir", zi.filename, None
    if mode == stat.S_IFLNK:
        return "symlink", zi.filename, ""
    if mode not in (0, stat.S_IFREG):
        return "other", zi.filename, None
    return "file", zi.filename, None


def _zip_partial_listing(f, size: int, cd_off: int, limit: int) -> list[ArchiveMember]:
    """Parse the first ``limit`` central-directory records by hand (zipfile would load all of them)."""
    out: list[ArchiveMember] = []
    f.seek(cd_off)
    while len(out) < limit:
        hdr = f.read(46)
        if len(hdr) < 46 or hdr[:4] != b"PK\x01\x02":
            break
        (_s, _vm, _vn, flags, _method, _mt, _md, _crc, _cs, usize, nlen, elen, clen, _disk, _ia, ea,
         _lo) = struct.unpack("<4s6H3I5H2I", hdr)
        name_raw = f.read(nlen)
        f.seek(elen + clen, os.SEEK_CUR)
        name = name_raw.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        mode = (ea >> 16) & 0o170000
        kind = "dir" if name.endswith("/") else "symlink" if mode == stat.S_IFLNK else "file"
        out.append(ArchiveMember(name=name, size=int(usize), kind=kind))
    return out


def _list_zip(path: Path, lim: ArchiveLimits) -> ArchiveListing:
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        declared, cd_off, _cd_size = _zip_eocd(f, size)
        res = ArchiveListing("zip", str(path), declared_entries=declared)
        if declared > lim.max_entries:
            res.members = _zip_partial_listing(f, size, cd_off, lim.max_entries)
            res.entries_seen = len(res.members)
            res.truncated = True
            res.truncation_reason = f"archive declares {declared} entries; listing limited to {lim.max_entries}"
            res.total_size = sum(m.size for m in res.members if m.kind == "file")
            return res
    try:
        with zipfile.ZipFile(path) as zf:
            for zi in zf.infolist():
                kind, name, _ = _zip_member_from_info(zi)
                res.members.append(ArchiveMember(name=name, size=zi.file_size, kind=kind))
    except (zipfile.BadZipFile, NotImplementedError, OSError, zlib.error, EOFError, struct.error) as e:
        raise ArchiveError(f"malformed zip: {e}") from e
    res.entries_seen = len(res.members)
    res.total_size = sum(m.size for m in res.members if m.kind == "file")
    return res


def _extract_zip(path: Path, out_dir: Path, lim: ArchiveLimits, only: Callable[[str], bool] | None) -> ExtractionReport:
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        declared, _cd_off, _cd_size = _zip_eocd(f, size)
    rep = ExtractionReport("zip", str(path), str(out_dir), declared_entries=declared)
    if declared > lim.max_entries:
        rep.truncated = True
        rep.truncation_reason = (f"archive declares {declared} entries, over the {lim.max_entries} limit; "
                                 "extraction refused (no partial extract of oversized zip directories)")
        rep.entries_seen = 0
        raise ArchiveLimitError(rep.truncation_reason)
    sink = _Sink(out_dir, lim, rep)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, NotImplementedError, OSError, struct.error) as e:
        raise ArchiveError(f"malformed zip: {e}") from e
    with zf:
        for zi in zf.infolist():
            rep.entries_seen += 1
            kind, name, _ = _zip_member_from_info(zi)
            if only is not None and not only(name):
                rep.skip(name, "filtered out")
                continue
            if kind == "dir":
                sink.mkdir(name)
                continue
            if kind == "symlink":
                try:
                    with zf.open(zi) as src:
                        tgt = src.read(MAX_LINK_TARGET_BYTES + 1).decode("utf-8", "replace")
                except Exception as e:  # noqa: BLE001 - any read failure is reported, not fatal
                    rep.error(f"{name}: unreadable symlink member: {e}")
                    continue
                _record_link(rep, name, tgt, hardlink=False)
                continue
            if kind == "other":
                rep.skip(name, "special file member")
                continue
            if zi.flag_bits & 0x1:
                rep.skip(name, "encrypted member (no password support)")
                continue
            if sink.would_exceed(zi.file_size):
                rep.truncated = True
                rep.truncation_reason = (f"expansion limit {lim.max_expansion_bytes} bytes reached before member "
                                         f"{name!r} ({zi.file_size} bytes)")
                break
            try:
                src = zf.open(zi)
            except (NotImplementedError, RuntimeError, zipfile.BadZipFile, ValueError) as e:
                rep.skip(name, f"cannot open member: {e}")
                continue
            with src:
                sink.write(name, read_chunks(src), zi.file_size)
            if sink.budget_hit:
                rep.truncated = True
                rep.truncation_reason = f"expansion limit {lim.max_expansion_bytes} bytes reached while writing {name!r}"
                break
    return rep


# ---------------------------------------------------------------------------------------------------------------------
# tar (streaming)
# ---------------------------------------------------------------------------------------------------------------------
def _tar_kind(ti: tarfile.TarInfo) -> str:
    if ti.isdir():
        return "dir"
    if ti.issym() or ti.islnk():
        return "symlink"
    if ti.isfile():
        return "file"
    return "other"


def _list_tar(path: Path, lim: ArchiveLimits) -> ArchiveListing:
    res = ArchiveListing("tar", str(path))
    try:
        with tarfile.open(path, "r|*") as tf:
            for ti in tf:
                if res.entries_seen >= lim.max_entries:
                    res.truncated = True
                    res.truncation_reason = f"more than {lim.max_entries} entries; listing stopped"
                    break
                kind = _tar_kind(ti)
                res.entries_seen += 1
                res.members.append(ArchiveMember(ti.name, ti.size if kind == "file" else 0, kind,
                                                 ti.linkname if kind == "symlink" else None))
                if kind == "file":
                    res.total_size += ti.size
                if res.total_size > lim.max_expansion_bytes:
                    res.truncated = True
                    res.truncation_reason = (f"declared expansion exceeds {lim.max_expansion_bytes} bytes; "
                                             "listing stopped")
                    break
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        if not res.members:
            raise ArchiveError(f"malformed tar: {e}") from e
        res.errors.append(f"tar stream error after {res.entries_seen} entries: {e}")
    return res


def _extract_tar(path: Path, out_dir: Path, lim: ArchiveLimits, only: Callable[[str], bool] | None) -> ExtractionReport:
    rep = ExtractionReport("tar", str(path), str(out_dir))
    sink = _Sink(out_dir, lim, rep)
    try:
        with tarfile.open(path, "r|*") as tf:
            for ti in tf:
                if rep.entries_seen >= lim.max_entries:
                    rep.truncated = True
                    rep.truncation_reason = f"more than {lim.max_entries} entries; extraction stopped"
                    break
                rep.entries_seen += 1
                name = ti.name
                if only is not None and not only(name):
                    rep.skip(name, "filtered out")
                    continue
                if ti.isdir():
                    sink.mkdir(name)
                elif ti.issym() or ti.islnk():
                    _record_link(rep, name, ti.linkname, hardlink=ti.islnk())
                elif ti.isfile():
                    if sink.would_exceed(ti.size):
                        rep.truncated = True
                        rep.truncation_reason = (f"expansion limit {lim.max_expansion_bytes} bytes reached before "
                                                 f"member {name!r} ({ti.size} bytes)")
                        break
                    src = tf.extractfile(ti)
                    if src is None:
                        rep.skip(name, "unreadable member")
                        continue
                    sink.write(name, read_chunks(src, ti.size), ti.size)
                    if sink.budget_hit:
                        rep.truncated = True
                        rep.truncation_reason = f"expansion limit reached while writing {name!r}"
                        break
                else:
                    rep.skip(name, "special file member (device/fifo)")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        if rep.entries_seen == 0:
            raise ArchiveError(f"malformed tar: {e}") from e
        rep.error(f"tar stream error after {rep.entries_seen} entries: {e}")
        rep.truncated = True
        rep.truncation_reason = f"archive ended unexpectedly: {e}"
    return rep


def _record_link(rep: ExtractionReport, name: str, target: str, *, hardlink: bool) -> None:
    if link_escapes(name, target, hardlink=hardlink):
        rep.refuse(name, f"{'hardlink' if hardlink else 'symlink'} target {target!r} resolves outside the archive root")
    else:
        rep.skip(name, f"{'hardlink' if hardlink else 'symlink'} to {target!r} recorded, not created")


# ---------------------------------------------------------------------------------------------------------------------
# asar (native)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class AsarArchive:
    path: Path
    file_size: int
    data_start: int
    header: dict[str, Any]
    members: list[ArchiveMember]
    declared_entries: int
    truncated: bool
    truncation_reason: str | None
    errors: list[str]


def read_asar_header(path: Path | str, lim: ArchiveLimits | Limits | None = None) -> AsarArchive:
    """Parse an asar header and flatten its file tree (iteratively; hostile nesting cannot recurse)."""
    lim = ArchiveLimits.coerce(lim)
    p = Path(path)
    try:
        with open(p, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            raw = f.read(16)
            if len(raw) < 16:
                raise ArchiveError("malformed asar: file shorter than the 16-byte header prefix")
            p0, header_size, payload, jlen = struct.unpack("<IIII", raw)
            if p0 != 4:
                raise ArchiveError("not an asar archive: size-pickle prefix is not 4")
            if header_size < 8 or 8 + header_size > file_size:
                raise ArchiveError("malformed asar: header size points past end of file")
            if payload + 4 != header_size or jlen + 4 > payload:
                raise ArchiveError("malformed asar: inconsistent header pickle sizes")
            if jlen > MAX_ASAR_HEADER_BYTES:
                raise ArchiveError(f"asar header JSON is {jlen} bytes, over the {MAX_ASAR_HEADER_BYTES} byte bound")
            js = f.read(jlen)
            if len(js) != jlen:
                raise ArchiveError("malformed asar: header JSON truncated")
    except OSError as e:
        raise ArchiveError(f"cannot read asar: {e}") from e
    try:
        header = json.loads(js.decode("utf-8"))
    except (ValueError, RecursionError, UnicodeDecodeError) as e:
        raise ArchiveError(f"malformed asar: header JSON invalid: {e}") from e
    if not isinstance(header, dict) or not isinstance(header.get("files"), dict):
        raise ArchiveError("malformed asar: header has no 'files' tree")
    data_start = 8 + header_size
    members: list[ArchiveMember] = []
    errors: list[str] = []
    truncated, reason = False, None
    declared = 0
    # Depth-first pre-order (parents before children, siblings sorted) using an explicit stack.
    stack: list[tuple[str, Any]] = [(k, header["files"][k]) for k in sorted(header["files"], reverse=True)]
    while stack:
        name, node = stack.pop()
        declared += 1
        if len(members) >= lim.max_entries:
            truncated = True
            reason = f"asar holds more than {lim.max_entries} entries; listing limited"
            break
        if not isinstance(node, dict):
            errors.append(f"{name}: node is not an object")
            continue
        if isinstance(node.get("files"), dict):
            members.append(ArchiveMember(name, 0, "dir"))
            kids = node["files"]
            stack.extend((f"{name}/{k}", kids[k]) for k in sorted(kids, reverse=True))
            continue
        if isinstance(node.get("link"), str):
            members.append(ArchiveMember(name, 0, "symlink", link_target=node["link"]))
            continue
        size = node.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"{name}: invalid size {size!r}")
            continue
        unpacked = bool(node.get("unpacked"))
        off: int | None = None
        if not unpacked:
            o = node.get("offset")
            try:
                off = int(o) if isinstance(o, (str, int)) and not isinstance(o, bool) else None
            except ValueError:
                off = None
            if off is None or off < 0:
                errors.append(f"{name}: invalid offset {o!r}")
                continue
        integ = node.get("integrity") if isinstance(node.get("integrity"), dict) else None
        members.append(ArchiveMember(name, size, "file", unpacked=unpacked, offset=off,
                                     executable=bool(node.get("executable")), integrity=integ))
    return AsarArchive(p, file_size, data_start, header, members, declared, truncated, reason, errors)


def _list_asar(path: Path, lim: ArchiveLimits) -> ArchiveListing:
    a = read_asar_header(path, lim)
    res = ArchiveListing("asar", str(path), members=a.members, entries_seen=len(a.members), declared_entries=None if a.truncated else a.declared_entries,
                         truncated=a.truncated, truncation_reason=a.truncation_reason, errors=list(a.errors))
    res.total_size = sum(m.size for m in a.members if m.kind == "file")
    for m in a.members:
        if m.kind == "file" and not m.unpacked and m.offset is not None and a.data_start + m.offset + m.size > a.file_size:
            res.errors.append(f"{m.name}: payload range lies beyond end of archive (truncated or corrupt file)")
    return res


def _extract_asar(path: Path, out_dir: Path, lim: ArchiveLimits, only: Callable[[str], bool] | None) -> ExtractionReport:
    a = read_asar_header(path, lim)
    rep = ExtractionReport("asar", str(path), str(out_dir), declared_entries=None if a.truncated else a.declared_entries)
    rep.errors.extend(a.errors[:REPORT_LIST_CAP])
    if a.truncated:
        rep.truncated = True
        rep.truncation_reason = a.truncation_reason
    sink = _Sink(out_dir, lim, rep)
    unpacked_root = path.with_name(path.name + ".unpacked")
    with open(path, "rb") as f:
        for m in a.members:
            rep.entries_seen += 1
            if only is not None and not only(m.name):
                rep.skip(m.name, "filtered out")
                continue
            if m.kind == "dir":
                sink.mkdir(m.name)
                continue
            if m.kind == "symlink":
                _record_link(rep, m.name, m.link_target or "", hardlink=False)
                continue
            if sink.would_exceed(m.size):
                rep.truncated = True
                rep.truncation_reason = (f"expansion limit {lim.max_expansion_bytes} bytes reached before member "
                                         f"{m.name!r} ({m.size} bytes)")
                break
            if m.unpacked:
                if not unpacked_root.is_dir():
                    rep.skip(m.name, f"unpacked payload not found: {unpacked_root.name}/ is missing next to the archive")
                    continue
                try:
                    src_path = safe_archive_target(unpacked_root, m.name)
                except PathPolicyError as e:
                    rep.refuse(m.name, f"unpacked sibling path rejected: {e}")
                    continue
                if not src_path.is_file() or src_path.is_symlink():
                    rep.skip(m.name, f"unpacked payload not found next to archive ({unpacked_root.name}/...)")
                    continue
                with open(src_path, "rb") as sf:
                    sink.write(m.name, read_chunks(sf, m.size), m.size)
            else:
                start = a.data_start + (m.offset or 0)
                if start + m.size > a.file_size:
                    rep.skip(m.name, "payload range lies beyond end of archive (truncated or corrupt)")
                    continue
                f.seek(start)
                sink.write(m.name, read_chunks(f, m.size), m.size)
            if sink.budget_hit:
                rep.truncated = True
                rep.truncation_reason = f"expansion limit reached while writing {m.name!r}"
                break
    return rep


class AsarReader:
    """Open an asar once and read packed members (bounded) without extracting."""

    def __init__(self, path: Path | str, lim: ArchiveLimits | Limits | None = None):
        self.archive = read_asar_header(path, lim)
        self.members = self.archive.members
        self._by_name = {m.name: m for m in self.members if m.kind == "file"}
        self._f = open(self.archive.path, "rb")

    def __enter__(self) -> "AsarReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._f.close()

    def has(self, name: str) -> bool:
        return name in self._by_name

    def size(self, name: str) -> int | None:
        m = self._by_name.get(name)
        return m.size if m else None

    def read(self, name: str, max_bytes: int = 1 << 20, offset: int = 0) -> bytes | None:
        """Read up to ``max_bytes`` of a packed member; None when absent, unpacked or out of range."""
        m = self._by_name.get(name)
        if m is None or m.unpacked or m.offset is None:
            return None
        a = self.archive
        if a.data_start + m.offset + m.size > a.file_size or offset >= m.size:
            return None
        self._f.seek(a.data_start + m.offset + offset)
        return self._f.read(min(max_bytes, m.size - offset))


def asar_read_file(path: Path | str, member: str, *, max_bytes: int = 1 << 20,
                   lim: ArchiveLimits | Limits | None = None) -> bytes | None:
    """Read one packed member (bounded) without extracting; None when absent/unpacked/over max_bytes/out of range."""
    with AsarReader(path, lim) as r:
        sz = r.size(member)
        if sz is None or sz > max_bytes:
            return None
        return r.read(member, max_bytes)


def verify_asar_integrity(path: Path | str, lim: ArchiveLimits | Limits | None = None,
                          max_files: int = 5000) -> dict[str, Any]:
    """Compare per-file sha256 integrity records (when the asar carries them) with the payload bytes."""
    a = read_asar_header(path, lim)
    checked = mismatched = 0
    bad: list[str] = []
    with open(a.path, "rb") as f:
        for m in a.members:
            if checked >= max_files:
                break
            if m.kind != "file" or m.unpacked or not m.integrity or m.offset is None:
                continue
            if str(m.integrity.get("algorithm", "")).upper() != "SHA256" or not m.integrity.get("hash"):
                continue
            start = a.data_start + m.offset
            if start + m.size > a.file_size:
                continue
            h = hashlib.sha256()
            f.seek(start)
            for c in read_chunks(f, m.size):
                h.update(c)
            checked += 1
            if h.hexdigest() != m.integrity["hash"]:
                mismatched += 1
                if len(bad) < 20:
                    bad.append(m.name)
    return {"checked": checked, "mismatched": mismatched, "mismatched_names": bad}


# ---------------------------------------------------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------------------------------------------------
def list_archive(path: Path | str, *, limits: ArchiveLimits | Limits | None = None, format: str | None = None) -> ArchiveListing:
    p = Path(path)
    lim = ArchiveLimits.coerce(limits)
    fmt = format or detect_format(p)
    if fmt is None:
        raise ArchiveError(f"{p.name}: unrecognised or malformed archive (not zip/tar/asar)")
    try:
        if fmt == "zip":
            return _list_zip(p, lim)
        if fmt == "tar":
            return _list_tar(p, lim)
        if fmt == "asar":
            return _list_asar(p, lim)
    except (struct.error, zlib.error, EOFError) as e:
        raise ArchiveError(f"malformed {fmt}: {e}") from e
    raise ArchiveError(f"unsupported archive format {fmt}")


def extract_archive(path: Path | str, out_dir: Path | str, *, limits: ArchiveLimits | Limits | None = None,
                    format: str | None = None, only: Callable[[str], bool] | None = None) -> ExtractionReport:
    """Extract with bounds and escape refusal. ``only`` filters member names (True = extract)."""
    p, out = Path(path), Path(out_dir)
    lim = ArchiveLimits.coerce(limits)
    fmt = format or detect_format(p)
    if fmt is None:
        raise ArchiveError(f"{p.name}: unrecognised or malformed archive (not zip/tar/asar)")
    if is_within(resolve_final(p), resolve_final(out)):
        raise ArchiveError("refusing to extract into a directory that contains the archive")
    try:
        if fmt == "zip":
            return _extract_zip(p, out, lim, only)
        if fmt == "tar":
            return _extract_tar(p, out, lim, only)
        if fmt == "asar":
            return _extract_asar(p, out, lim, only)
    except (struct.error, zlib.error, EOFError) as e:
        raise ArchiveError(f"malformed {fmt}: {e}") from e
    raise ArchiveError(f"unsupported archive format {fmt}")


# =====================================================================================================================
# Shared backend helpers (used by ilspy.py, gdre.py, jsweb.py)
# =====================================================================================================================
@dataclass
class RecoveryToolProbe(ToolProbe):
    """ToolProbe plus the operator guidance the base contract lacks. Serialises via ToolProbe.to_dict()."""
    next_action: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _exe_names(base: str) -> list[str]:
    return [base, base + ".exe", base + ".cmd", base + ".bat"] if os.name == "nt" else [base]


def _is_exec(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def discover_executable(settings: Settings, names: list[str], *, subdirs: list[str],
                        extra_dirs: list[Path] | None = None, bin_subpaths: tuple[str, ...] = ("", "bin", "node_modules/.bin")) -> Path | None:
    """Tool discovery order: settings.tools_dir/<subdir>[/bin|node_modules/.bin], PATH, Windows per-user tools, extra dirs."""
    cand_names = [n2 for n in names for n2 in _exe_names(n)]
    tools = Path(settings.tools_dir)
    for sub in subdirs:
        for bp in bin_subpaths:
            base = tools / sub / bp if bp else tools / sub
            for n in cand_names:
                if _is_exec(base / n):
                    return base / n
    for n in cand_names:
        w = shutil.which(n)
        if w:
            return Path(w)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for sub in subdirs:
                for bp in bin_subpaths:
                    base = Path(local) / "RebuildStudio" / "tools" / sub / bp if bp else Path(local) / "RebuildStudio" / "tools" / sub
                    for n in cand_names:
                        if _is_exec(base / n):
                            return base / n
    for d in extra_dirs or []:
        for n in cand_names:
            if _is_exec(Path(d) / n):
                return Path(d) / n
    return None


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in read_chunks(f):
            h.update(c)
    return h.hexdigest()


@dataclass
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool
    duration_s: float
    command: list[str]

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def run_bounded(ctx: Any, command: list[str], *, limits: Limits, env: dict[str, str] | None = None,
                cwd: str | os.PathLike | None = None, timeout: float | None = None) -> ProcResult:
    """Run through ``ctx.run`` (job-tied, cancellable) when a StageContext is supplied; otherwise a local bounded run
    with the same output cap and process-group kill. Timeouts raise ``StageError`` in both paths."""
    from ..jobs.runner import StageError, kill_tree
    if ctx is not None and hasattr(ctx, "run"):
        r = ctx.run(command, cwd=cwd, env=env, timeout=timeout)
        return ProcResult(r.returncode, r.stdout, r.stderr, r.truncated, r.timed_out, r.duration_s, list(command))
    timeout = timeout or limits.max_stage_seconds
    cap = limits.max_subprocess_output_bytes
    kwargs: dict[str, Any] = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    start = time.time()
    proc = subprocess.Popen(command, **kwargs)
    chunks: list[list[bytes]] = [[], []]
    sizes = [0, 0]
    trunc = [False]

    def pump(stream, idx):
        try:
            while True:
                b = stream.read(CHUNK)
                if not b:
                    break
                room = cap - sizes[idx]
                if len(b) > room:
                    b = b[:max(0, room)]
                    trunc[0] = True
                if b:
                    chunks[idx].append(b)
                    sizes[idx] += len(b)
        except Exception:  # noqa: BLE001
            pass

    ts = [threading.Thread(target=pump, args=(proc.stdout, 0), daemon=True),
          threading.Thread(target=pump, args=(proc.stderr, 1), daemon=True)]
    for t in ts:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    for t in ts:
        t.join(timeout=5)
    res = ProcResult(proc.returncode if proc.returncode is not None else -1, b"".join(chunks[0]), b"".join(chunks[1]),
                     trunc[0], timed_out, time.time() - start, list(command))
    if timed_out:
        raise StageError(f"timeout after {timeout:.0f}s running {command[0]}", retry=False)
    return res


def resolve_studio(ctx: Any = None, studio: Any = None) -> Any:
    if studio is not None:
        return studio
    services = getattr(ctx, "services", None) or {}
    return services.get("studio")


def record_evidence(studio: Any, case_id: str | None, kind: str, title: str, body: Any, *, module_id: str | None,
                    inputs: dict[str, Any], producer: str, meta: dict[str, Any] | None = None) -> str | None:
    """Store evidence through ``studio.cases.add_evidence``; ``inputs`` must carry tool version + module sha256."""
    if studio is None or not case_id:
        return None
    ev = studio.cases.add_evidence(case_id, kind, title, body=body, module_id=module_id, meta=meta or {},
                                   inputs=inputs, producer=producer)
    return ev["evidence_id"]


def cap_list(items: list[Any], n: int) -> dict[str, Any]:
    return {"items": items[:n], "total": len(items), "truncated": len(items) > n}


def probe_error(name: str, detail: str, next_action: str, **kw: Any) -> RecoveryToolProbe:
    return RecoveryToolProbe(name=name, availability=Availability.MISSING, detail=detail, next_action=next_action, **kw)


def failed_probe_info(backend_id: str, title: str, tool_name: str, exc: BaseException, next_action: str) -> BackendInfo:
    """probe() must never raise: an unexpected error becomes a MISSING tool with the error and a next_action."""
    t = probe_error(tool_name, f"probe failed: {type(exc).__name__}: {exc}", next_action)
    return BackendInfo(backend_id=backend_id, title=title, formats=[], platforms=[], profiles=[], operations=[], tools=[t],
                       resources={"next_action": next_action})


__all__ = [
    "ArchiveError", "ArchiveLimitError", "ArchiveLimits", "ArchiveMember", "ArchiveListing", "ExtractionReport",
    "AsarArchive", "AsarReader", "SafeWriter", "read_chunks", "detect_format", "list_archive", "extract_archive", "read_asar_header", "asar_read_file",
    "verify_asar_integrity", "link_escapes", "RecoveryToolProbe", "discover_executable", "run_bounded", "ProcResult",
    "resolve_studio", "record_evidence", "cap_list", "probe_error", "failed_probe_info", "sha256_of",
]
