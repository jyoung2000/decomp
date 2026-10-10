"""SQLite cross-index of one analysed module: functions <-> strings <-> xrefs (R1).

Built from rizin output already in hand (``aflj``, ``izzj``, ``axlj``) and stored in the module's work folder
(``<case>/re/<module>/xref_index.sqlite``), so "which functions use this string", "what does this function reference" and
the whole-program call graph are SQL queries instead of thousands of rizin round trips. Everything in the index is
binary-derived (untrusted) text; callers wrap search results accordingly.
"""
from __future__ import annotations

import bisect
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE functions (addr INTEGER PRIMARY KEY, name TEXT, size INTEGER, minbound INTEGER, maxbound INTEGER, nbbs INTEGER);
CREATE TABLE strings (addr INTEGER PRIMARY KEY, text TEXT, type TEXT, section TEXT, length INTEGER);
CREATE TABLE xrefs (src INTEGER, dst INTEGER, type TEXT, src_fn INTEGER, dst_fn INTEGER);
CREATE INDEX xrefs_dst ON xrefs(dst);
CREATE INDEX xrefs_src_fn ON xrefs(src_fn);
CREATE INDEX xrefs_dst_fn ON xrefs(dst_fn);
CREATE INDEX strings_text ON strings(text);
CREATE VIEW string_refs AS
  SELECT s.addr AS string_addr, s.text AS string, x.src AS ref_addr, f.addr AS fn_addr, f.name AS fn_name
  FROM strings s JOIN xrefs x ON x.dst = s.addr AND x.type != 'PTR' LEFT JOIN functions f ON f.addr = x.src_fn
  UNION
  SELECT s.addr, s.text, x2.src, f.addr, f.name
  FROM strings s JOIN xrefs p ON p.dst = s.addr AND p.type = 'PTR' JOIN xrefs x2 ON x2.dst = p.src AND x2.src_fn IS NOT NULL
  LEFT JOIN functions f ON f.addr = x2.src_fn;
CREATE VIEW call_edges AS
  SELECT DISTINCT x.src_fn AS caller, x.dst AS callee FROM xrefs x WHERE x.type = 'CALL' AND x.src_fn IS NOT NULL;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
MAX_TEXT = 1000


class _Owner:
    def __init__(self, functions: list[dict[str, Any]]):
        spans = []
        for f in functions:
            off = f.get("offset")
            if isinstance(off, int):
                lo = f.get("minbound", off)
                hi = f.get("maxbound", off + (f.get("size") or 0))
                spans.append((lo, hi, off))
        spans.sort()
        self.spans = spans
        self.lows = [s[0] for s in spans]
        self.starts = {s[2] for s in spans}

    def of(self, addr: int) -> int | None:
        i = bisect.bisect_right(self.lows, addr) - 1
        for j in range(i, max(-1, i - 32), -1):
            lo, hi, off = self.spans[j]
            if lo <= addr < hi:
                return off
        return None


def build(db_path: Path, *, functions: list[dict[str, Any]], strings: list[dict[str, Any]], xrefs: list[dict[str, Any]],
          meta: dict[str, Any] | None = None, pointers: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    """``pointers``: (slot, value) pairs of absolute pointers stored in data (from the relocation table). They are indexed
    as ``PTR`` xrefs so a string reached through a pointer table (``mov rcx, [slot]``) is still linked to its function."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(SCHEMA)
        own = _Owner(functions)
        con.executemany("INSERT OR IGNORE INTO functions VALUES (?,?,?,?,?,?)",
                        [(f["offset"], str(f.get("name") or ""), f.get("size"), f.get("minbound"), f.get("maxbound"), f.get("nbbs"))
                         for f in functions if isinstance(f.get("offset"), int)])
        con.executemany("INSERT OR IGNORE INTO strings VALUES (?,?,?,?,?)",
                        [(s["vaddr"], str(s.get("string") or "")[:MAX_TEXT], s.get("type"), s.get("section"), s.get("length"))
                         for s in strings if isinstance(s.get("vaddr"), int)])
        rows = []
        for x in xrefs:
            src, dst = x.get("from"), x.get("to")
            if not isinstance(src, int) or not isinstance(dst, int):
                continue
            rows.append((src, dst, str(x.get("type") or ""), own.of(src), dst if dst in own.starts else own.of(dst)))
        for slot, val in pointers or []:
            if own.of(slot) is None:   # pointers inside code are jump tables etc., not data tables
                rows.append((slot, val, "PTR", None, val if val in own.starts else own.of(val)))
        con.executemany("INSERT INTO xrefs VALUES (?,?,?,?,?)", rows)
        con.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in (meta or {}).items()])
        con.commit()
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("functions", "strings", "xrefs")}
        counts["string_refs"] = con.execute("SELECT COUNT(*) FROM string_refs").fetchone()[0]
        counts["call_edges"] = con.execute("SELECT COUNT(*) FROM call_edges").fetchone()[0]
    finally:
        con.close()
    tmp.replace(db_path)
    return counts


def search(db_path: Path, query: str, *, limit: int = 50) -> dict[str, Any]:
    """Strings containing ``query`` (case-insensitive) with the functions that reference them, and functions whose name
    contains it. ``query`` is bound as a parameter (never interpolated)."""
    limit = max(1, min(int(limit), 500))
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        srows = con.execute("SELECT addr, text FROM strings WHERE text LIKE ? ESCAPE '\\' ORDER BY addr LIMIT ?", (like, limit)).fetchall()
        strings = []
        for addr, text in srows:
            refs = con.execute("SELECT DISTINCT fn_addr, fn_name, ref_addr FROM string_refs WHERE string_addr=? LIMIT 50", (addr,)).fetchall()
            strings.append({"addr": f"0x{addr:x}", "string": text,
                            "referenced_by": [{"function": f"0x{a:x}" if a is not None else None, "name": n, "at": f"0x{r:x}"} for a, n, r in refs]})
        frows = con.execute("SELECT addr, name, size FROM functions WHERE name LIKE ? ESCAPE '\\' ORDER BY addr LIMIT ?", (like, limit)).fetchall()
        functions = [{"addr": f"0x{a:x}", "name": n, "size": s} for a, n, s in frows]
        return {"strings": strings, "functions": functions, "truncated": len(srows) >= limit or len(frows) >= limit}
    finally:
        con.close()


def function_refs(db_path: Path, fn_addr: int, *, limit: int = 200) -> dict[str, Any]:
    """Strings a function references, its callees and callers, from the index."""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        strings = con.execute("SELECT DISTINCT string_addr, string FROM string_refs WHERE fn_addr=? LIMIT ?", (fn_addr, limit)).fetchall()
        callees = con.execute("SELECT DISTINCT callee FROM call_edges WHERE caller=? LIMIT ?", (fn_addr, limit)).fetchall()
        callers = con.execute("SELECT DISTINCT caller FROM call_edges WHERE callee=? LIMIT ?", (fn_addr, limit)).fetchall()
        return {"strings": [{"addr": f"0x{a:x}", "string": t} for a, t in strings],
                "callees": [f"0x{c:x}" for (c,) in callees], "callers": [f"0x{c:x}" for (c,) in callers]}
    finally:
        con.close()
