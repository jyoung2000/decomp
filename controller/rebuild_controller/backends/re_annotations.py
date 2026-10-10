"""Persistent reverse-engineering annotations (renames, types, comments, structs) for one case module.

Storage: every change stores the complete annotation state as a new ``re.annotations`` evidence revision (module-scoped).
The newest revision is the current state; older revisions are the history. Nothing is kept only in a rizin process, so
annotations survive controller restarts, idle-reaped or crashed rizin sessions and re-decompiles: ``apply_all`` replays
them after every (re-)analysis (wired through ``RizinSession.annotator``).

Every value is validated here against a strict grammar before it is stored, and validated again by the rizin session
before it is turned into a command, so stored annotations can never smuggle a rizin command, pipe or temporary seek.
Comments are never sent to rizin at all: they are overlaid onto disassembly / decompiler output in Python.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from ..ids import now_iso
from . import native

KIND = "re.annotations"
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
CTYPE_RE = re.compile(r"^(?:(?:struct|union|enum|unsigned|signed|const|long|short|volatile)\s+){0,4}[A-Za-z_][A-Za-z0-9_]{0,63}"
                      r"(?:\s*\*{1,3})?$")
PROTO_RE = re.compile(r"^[A-Za-z0-9_ *,()\[\].]{3,512}$")
PROTO_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
DECL_CHARS_RE = re.compile(r"^[A-Za-z0-9_ \t\r\n*;,{}\[\]():=\-]+$")
DECL_NAMES_RE = re.compile(r"\b(struct|union|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
TYPEDEF_NAME_RE = re.compile(r"\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;")
MAX_COMMENT_CHARS = 1000
MAX_DECL_BYTES = 16 * 1024
MAX_TYPES = 200
MAX_ENTRIES = 20_000


class AnnotationError(ValueError):
    pass


def empty() -> dict[str, Any]:
    return {"version": 1, "functions": {}, "locals": {}, "globals": {}, "comments": {}, "types": []}


# ----------------------------------------------------------------------------------------------- validation
def check_ident(name: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
        raise AnnotationError(f"invalid name {str(name)[:80]!r}: use letters, digits and _ (not starting with a digit), at most 128")
    return name


def check_ctype(t: str) -> str:
    if not isinstance(t, str) or not CTYPE_RE.fullmatch(t.strip()):
        raise AnnotationError(f"invalid C type {str(t)[:80]!r}: e.g. 'int', 'uint32_t', 'char *', 'struct point *'")
    return " ".join(t.split())


def check_prototype(p: str) -> tuple[str, str]:
    """Return (normalised prototype, function name declared in it)."""
    if not isinstance(p, str) or not PROTO_RE.fullmatch(p.strip()) or p.count("(") != 1 or p.count(")") != 1 \
            or not p.strip().endswith(")"):
        raise AnnotationError(f"invalid prototype {str(p)[:80]!r}: expected e.g. 'int parse_args(int argc, char **argv)'")
    m = PROTO_NAME_RE.search(p)
    if not m:
        raise AnnotationError("prototype has no function name before '('")
    return " ".join(p.split()), m.group(1)


def check_comment(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_COMMENT_CHARS:
        raise AnnotationError(f"comment must be a string of at most {MAX_COMMENT_CHARS} characters")
    if any((ord(c) < 32 and c not in "\t") or ord(c) == 127 for c in text):
        raise AnnotationError("comment must be a single line without control characters")
    return text


def check_declaration(decl: str) -> tuple[str, list[dict[str, str]]]:
    """A C struct/union/enum/typedef declaration. No preprocessor ('#'), no comments ('/'), no quotes, bounded size."""
    if not isinstance(decl, str) or not decl.strip():
        raise AnnotationError("declaration must be non-empty C text")
    if len(decl.encode("utf-8")) > MAX_DECL_BYTES:
        raise AnnotationError(f"declaration larger than {MAX_DECL_BYTES} bytes")
    if not DECL_CHARS_RE.fullmatch(decl):
        raise AnnotationError("declaration may only contain identifiers, digits, whitespace and * ; , { } [ ] ( ) : = - "
                              "(no '#' preprocessor lines, comments or quotes)")
    if not re.match(r"^\s*(struct|union|enum|typedef)\b", decl):
        raise AnnotationError("declaration must start with struct, union, enum or typedef")
    if decl.count("{") != decl.count("}"):
        raise AnnotationError("unbalanced braces")
    names = [{"kind": k, "name": n} for k, n in DECL_NAMES_RE.findall(decl)]
    if decl.lstrip().startswith("typedef"):
        names += [{"kind": "typedef", "name": n} for n in TYPEDEF_NAME_RE.findall(decl)]
    if not names:
        raise AnnotationError("no named struct/union/enum (or typedef name) found in the declaration")
    text = decl.strip()
    if not text.endswith(";"):
        text += ";"
    return text + "\n", names


# ----------------------------------------------------------------------------------------------- storage
def load(cases: Any, case_id: str, module_id: str) -> tuple[int, dict[str, Any], str | None]:
    """(revision, state, evidence_id) of the newest annotation revision; (0, empty, None) when there is none."""
    rows = cases.list_evidence(case_id, kind=KIND, module_id=module_id)
    if not rows:
        return 0, empty(), None
    row = rows[-1]
    body = cases.blobs.get_json(row["blob_sha"]) if row.get("blob_sha") else {}
    state = body.get("state") if isinstance(body, dict) else None
    out = empty()
    if isinstance(state, dict):
        out.update({k: state.get(k, out[k]) for k in out})
    return int(row["revision"]), out, row["evidence_id"]


def save(cases: Any, case_id: str, module_id: str, state: dict[str, Any], change: dict[str, Any], *,
         parent: str | None, author: str) -> dict[str, Any]:
    n = (len(state["functions"]) + sum(len(v) for v in state["locals"].values()) + len(state["globals"]) + len(state["comments"])
         + len(state["types"]))
    if n > MAX_ENTRIES:
        raise AnnotationError(f"more than {MAX_ENTRIES} annotations on one module")
    change = {**change, "at": now_iso(), "author": author}
    body = {"state": state, "change": change, "parent_evidence_id": parent,
            "label": "model-proposed" if author == "model" else "user"}
    inputs = {"op": "annotations", "parent": parent, "change": change}
    return native.store_evidence(cases, case_id, module_id, KIND, f"Annotations: {change.get('action')}", body, inputs,
                                 untrusted=True, producer=f"annotations:{author}")


def history(cases: Any, case_id: str, module_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = cases.list_evidence(case_id, kind=KIND, module_id=module_id)[-limit:]
    out = []
    for r in rows:
        body = cases.blobs.get_json(r["blob_sha"]) if r.get("blob_sha") else {}
        out.append({"evidence_id": r["evidence_id"], "revision": r["revision"], "change": (body or {}).get("change")})
    return out


# ----------------------------------------------------------------------------------------------- mutation helpers
def mutated(state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(state)


def local_key(state: dict[str, Any], fn: str, current_name: str) -> str:
    """The ORIGINAL variable name an annotation is keyed by (follows earlier renames back to the analysed name)."""
    for orig, ent in (state["locals"].get(fn) or {}).items():
        if ent.get("name") == current_name:
            return orig
    return current_name


def type_file(work_dir: Path, decl: str) -> Path:
    d = work_dir / "types"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (hashlib.sha256(decl.encode("utf-8")).hexdigest()[:24] + ".h")
    if not p.is_file() or p.read_text("utf-8") != decl:
        p.write_text(decl, "utf-8")
    return p


# ----------------------------------------------------------------------------------------------- replay
def apply_all(sess: Any, state: dict[str, Any], work_dir: Path, poll: Callable[[], None] | None = None) -> dict[str, Any]:
    """Replay every annotation onto a freshly analysed rizin session. Never raises for one bad entry (reports it)."""
    from .rizin_worker import RizinCrashed, RizinTimeout
    applied, skipped = 0, []

    def attempt(what: str, fn: Callable[[], None]) -> None:
        nonlocal applied
        try:
            fn()
            applied += 1
        except (RizinCrashed, RizinTimeout):
            raise
        except Exception as e:  # one stale annotation must not block the rest
            skipped.append({"what": what, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    for t in state.get("types") or []:
        attempt(f"type {t.get('names')}", lambda t=t: sess.load_types_file(type_file(work_dir, t["decl"]), poll))
    for addr_s, ent in (state.get("functions") or {}).items():
        a = int(addr_s, 16)
        if ent.get("name"):
            attempt(f"function name {addr_s}", lambda a=a, n=ent["name"]: sess.rename_function(a, n, poll))
        if ent.get("prototype"):
            attempt(f"prototype {addr_s}", lambda a=a, p=ent["prototype"]: sess.set_prototype(a, p, poll))
    for fn_s, vars_ in (state.get("locals") or {}).items():
        fa = int(fn_s, 16)
        try:
            data, _ = sess._run(f"afvlj @ 0x{fa:x}", poll=poll)
            present = {v.get("name") for group in (data or {}).values() if isinstance(group, list) for v in group if isinstance(v, dict)}
        except (RizinCrashed, RizinTimeout):
            raise
        except Exception:
            present = set()
        for orig, ent in vars_.items():
            cur = orig
            if ent.get("name") and ent["name"] != orig:
                if orig in present:
                    attempt(f"local {fn_s}:{orig}", lambda fa=fa, o=orig, n=ent["name"]: sess.rename_variable(fa, o, n, poll))
                    cur = ent["name"]
                elif ent["name"] in present:
                    cur = ent["name"]
                else:
                    skipped.append({"what": f"local {fn_s}:{orig}", "error": "variable no longer present after analysis"})
                    continue
            if ent.get("type"):
                attempt(f"local type {fn_s}:{cur}", lambda fa=fa, c=cur, t=ent["type"]: sess.retype_variable(fa, c, t, poll))
    for addr_s, ent in (state.get("globals") or {}).items():
        attempt(f"global {addr_s}", lambda a=int(addr_s, 16), n=ent["name"]: sess.set_flag(a, n, poll))
    return {"applied": applied, "skipped": skipped}


# ----------------------------------------------------------------------------------------------- overlays (comments)
def overlay_disasm(ops: list[dict[str, Any]], state: dict[str, Any]) -> int:
    comments = state.get("comments") or {}
    n = 0
    for op in ops:
        off = op.get("offset")
        if isinstance(off, int):
            c = comments.get(f"0x{off:x}")
            if c:
                op["user_comment"] = c["text"]
                n += 1
    return n


def comments_in(state: dict[str, Any], lo: int, hi: int) -> list[tuple[int, str]]:
    out = []
    for k, c in (state.get("comments") or {}).items():
        a = int(k, 16)
        if lo <= a < hi:
            out.append((a, c["text"]))
    return sorted(out)


def overlay_decompiled(text: str, comments: list[tuple[int, str]]) -> str:
    if not comments:
        return text
    head = "".join(f"// [annotation @0x{a:x}] {t}\n" for a, t in comments)
    return head + text
