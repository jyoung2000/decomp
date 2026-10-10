"""PDB use for native analysis (R1): find the program database that belongs to a PE, verify it, and hand it to rizin.

* ``codeview(path)`` reads the PE debug directory's CodeView ``RSDS`` record: PDB file name, GUID and age.
* ``pdb_identity(pdb)`` reads the PDB info stream (MSF 7.00 stream 1): GUID and age (the DBI stream's age when present).
* A PDB is used only when its GUID **and** age equal the PE's record ("hash-verified": the GUID/age pair is the identity
  the linker writes into both files; a PDB from another build never matches).
* ``find_sidecar`` looks next to the module (``<RSDS name>`` or ``<stem>.pdb``); ``download`` fetches from a Microsoft-style
  symbol server (``<server>/<name>/<GUIDAGE>/<name>``) ONLY when the case opted in (setting ``symbol_server``), into the
  case work folder, with a size cap, and keeps the file only if it verifies.
"""
from __future__ import annotations

import struct
import uuid
from pathlib import Path
from typing import Any

MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
DEFAULT_SERVER = "https://msdl.microsoft.com/download/symbols"
MAX_PDB_BYTES = 512 * 1024 * 1024


class PdbError(ValueError):
    pass


def codeview(path: Path | str) -> dict[str, Any] | None:
    """{"pdb_name", "guid", "age", "key"} from the PE's RSDS record, or None (no debug directory / not a PE)."""
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
    except Exception:
        return None
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]])
        for d in getattr(pe, "DIRECTORY_ENTRY_DEBUG", []) or []:
            if d.struct.Type != 2:     # IMAGE_DEBUG_TYPE_CODEVIEW
                continue
            raw = pe.__data__[d.struct.PointerToRawData: d.struct.PointerToRawData + d.struct.SizeOfData]
            if raw[:4] != b"RSDS" or len(raw) < 25:
                continue
            g = uuid.UUID(bytes_le=bytes(raw[4:20]))
            age = struct.unpack_from("<I", raw, 20)[0]
            name = bytes(raw[24:]).split(b"\0", 1)[0].decode("utf-8", "replace")
            return {"pdb_name": name, "guid": str(g).upper(), "age": age, "key": g.hex.upper() + f"{age:X}"}
    finally:
        pe.close()
    return None


def _msf_stream(data: bytes, idx: int) -> bytes:
    if not data.startswith(MSF_MAGIC):
        raise PdbError("not an MSF 7.00 PDB")
    bs, _fpm, _nb, dir_bytes, _unk, bmap = struct.unpack_from("<6I", data, 32)
    if bs not in (512, 1024, 2048, 4096):
        raise PdbError("bad block size")
    ndb = -(-dir_bytes // bs)
    dblocks = struct.unpack_from(f"<{ndb}I", data, bmap * bs)
    directory = b"".join(data[i * bs:(i + 1) * bs] for i in dblocks)[:dir_bytes]
    n = struct.unpack_from("<I", directory, 0)[0]
    sizes = struct.unpack_from(f"<{n}I", directory, 4)
    pos = 4 + 4 * n
    for i, size in enumerate(sizes):
        nb = 0 if size == 0xFFFFFFFF else -(-size // bs)
        blocks = struct.unpack_from(f"<{nb}I", directory, pos)
        pos += 4 * nb
        if i == idx:
            if size == 0xFFFFFFFF:
                return b""
            return b"".join(data[b * bs:(b + 1) * bs] for b in blocks)[:size]
    raise PdbError(f"stream {idx} missing")


def pdb_identity(path: Path | str) -> dict[str, Any]:
    data = Path(path).read_bytes()
    info = _msf_stream(data, 1)
    if len(info) < 28:
        raise PdbError("PDB info stream too short")
    _ver, _sig, age = struct.unpack_from("<III", info, 0)
    g = uuid.UUID(bytes_le=bytes(info[12:28]))
    try:                                     # the DBI stream carries the age the linker matched against the PE
        dbi = _msf_stream(data, 3)
        if len(dbi) >= 12:
            age = struct.unpack_from("<I", dbi, 8)[0]
    except PdbError:
        pass
    return {"guid": str(g).upper(), "age": age}


def matches(pe_cv: dict[str, Any], pdb_path: Path | str) -> bool:
    try:
        ident = pdb_identity(pdb_path)
    except (OSError, PdbError, struct.error):
        return False
    return ident["guid"] == pe_cv["guid"] and ident["age"] == pe_cv["age"]


def find_sidecar(module_path: Path | str, cv: dict[str, Any] | None = None) -> Path | None:
    p = Path(module_path)
    cv = cv or codeview(p)
    if not cv:
        return None
    names = [Path(cv["pdb_name"].replace("\\", "/")).name, p.with_suffix(".pdb").name]
    for n in dict.fromkeys(names):
        c = p.parent / n
        if c.is_file() and matches(cv, c):
            return c
    return None


def symbol_server_url(cv: dict[str, Any], server: str = DEFAULT_SERVER) -> str:
    name = Path(cv["pdb_name"].replace("\\", "/")).name
    if not name or "/" in name or ".." in name:
        raise PdbError("unusable PDB name in the CodeView record")
    return f"{server.rstrip('/')}/{name}/{cv['key']}/{name}"


def download(cv: dict[str, Any], dest_dir: Path | str, *, server: str = DEFAULT_SERVER, timeout: float = 120.0,
             client: Any = None) -> dict[str, Any]:
    """Opt-in symbol-server fetch into ``dest_dir`` (case work folder). Keeps the file only if GUID and age verify."""
    import httpx
    url = symbol_server_url(cv, server)
    if not url.startswith("https://"):
        raise PdbError("symbol server must use https")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(cv["pdb_name"].replace("\\", "/")).name
    part = dest_dir / (name + ".part")
    own = client is None
    c = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                return {"ok": False, "url": url, "reason": f"symbol server answered {r.status_code}"}
            total = 0
            with open(part, "wb") as f:
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_PDB_BYTES:
                        raise PdbError("PDB larger than the size cap")
                    f.write(chunk)
    except (httpx.HTTPError, OSError, PdbError) as e:
        part.unlink(missing_ok=True)
        return {"ok": False, "url": url, "reason": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            c.close()
    if not matches(cv, part):
        part.unlink(missing_ok=True)
        return {"ok": False, "url": url, "reason": "downloaded file is not the PDB of this build (GUID/age mismatch)"}
    final = dest_dir / name
    part.replace(final)
    return {"ok": True, "url": url, "path": str(final), "size": final.stat().st_size, "guid": cv["guid"], "age": cv["age"]}
