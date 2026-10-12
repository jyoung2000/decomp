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
IDENT_CHAR = r"[A-Za-z0-9_]"
CTYPE_RE = re.compile(r"^(?:(?:struct|union|enum|unsigned|signed|const|long|short|volatile)\s+){0,4}[A-Za-z_][A-Za-z0-9_]{0,63}"
                      r"(?:\s*\*{1,3})?$")
PROTO_RE = re.compile(r"^[A-Za-z0-9_ *,()\[\].]{3,512}$")
PROTO_CHAR = r"[A-Za-z0-9_ *,()\[\].]"
DECL_CHAR = r"[A-Za-z0-9_ \t\r\n*;,{}\[\]():=\-]"
PROTO_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
DECL_CHARS_RE = re.compile(r"^[A-Za-z0-9_ \t\r\n*;,{}\[\]():=\-]+$")
DECL_NAMES_RE = re.compile(r"\b(struct|union|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
TYPEDEF_NAME_RE = re.compile(r"\}\s*([A-Za-z_][A-Za-z0-9_]*)\s*;")
MAX_COMMENT_CHARS = 1000
MAX_DECL_BYTES = 16 * 1024
MAX_TYPES = 200
MAX_ENTRIES = 20_000

# One valid example per kind of value, shown next to every refusal (app and MCP) so the fix is obvious.
EXAMPLES = {
    "name": "parse_args",
    "type": "uint32_t, char *, struct point *",
    "prototype": "int parse_args(int argc, char **argv)",
    "comment": "Opens the settings file and reads the [items] section",
    "declaration": "struct point { int x; int y; };",
}


class AnnotationError(ValueError):
    pass


def empty() -> dict[str, Any]:
    return {"version": 1, "functions": {}, "locals": {}, "globals": {}, "comments": {}, "types": []}


# ----------------------------------------------------------------------------------------------- validation
def check_ident(name: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
        shown = str(name)[:80]
        why = ("it is empty" if not str(name) else "it starts with a digit" if str(name)[:1].isdigit()
               else "it is longer than 128 characters" if len(str(name)) > 128
               else f"it contains {_bad_chars(str(name), IDENT_CHAR)}")
        raise AnnotationError(f"invalid name {shown!r}: {why}. Use letters, digits and _ (not starting with a digit), at most 128, "
                              f"e.g. {EXAMPLES['name']}")
    return name


def _bad_chars(text: str, allowed: str) -> str:
    bad = sorted({c for c in text if not re.fullmatch(allowed, c)})
    shown = " ".join("space" if c == " " else repr(c) for c in bad[:6])
    return f"characters that are not allowed ({shown})" if bad else "something that is not allowed"


def check_ctype(t: str) -> str:
    if not isinstance(t, str) or not CTYPE_RE.fullmatch(t.strip()):
        raise AnnotationError(f"invalid C type {str(t)[:80]!r}: one type name with up to 4 qualifiers (struct, unsigned, const, "
                              "...) and up to 3 '*', no array sizes or function pointers; e.g. 'int', 'uint32_t', 'char *', "
                              "'struct point *'")
    return " ".join(t.split())


def check_prototype(p: str) -> tuple[str, str]:
    """Return (normalised prototype, function name declared in it)."""
    if not isinstance(p, str) or not PROTO_RE.fullmatch(p.strip()) or p.count("(") != 1 or p.count(")") != 1 \
            or not p.strip().endswith(")"):
        t = str(p).strip()
        why = ("it is shorter than 3 characters" if len(t) < 3 else "it is longer than 512 characters" if len(t) > 512
               else f"it contains {_bad_chars(t, PROTO_CHAR)}" if not PROTO_RE.fullmatch(t)
               else "it needs exactly one '(' and one ')'" if t.count("(") != 1 or t.count(")") != 1
               else "it must end with ')' (leave out the ';' and the body)")
        raise AnnotationError(f"invalid prototype {str(p)[:80]!r}: {why}; expected e.g. '{EXAMPLES['prototype']}'")
    m = PROTO_NAME_RE.search(p)
    if not m:
        raise AnnotationError(f"prototype has no function name before '(', e.g. {EXAMPLES['prototype']}")
    return " ".join(p.split()), m.group(1)


def check_comment(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_COMMENT_CHARS:
        raise AnnotationError(f"comment must be a string of at most {MAX_COMMENT_CHARS} characters "
                              f"(this one has {len(text) if isinstance(text, str) else 0})")
    if any((ord(c) < 32 and c not in "\t") or ord(c) == 127 for c in text):
        raise AnnotationError("comment must be a single line without control characters")
    return text


def check_declaration(decl: str) -> tuple[str, list[dict[str, str]]]:
    """A C struct/union/enum/typedef declaration. No preprocessor ('#'), no comments ('/'), no quotes, bounded size."""
    if not isinstance(decl, str) or not decl.strip():
        raise AnnotationError(f"declaration must be non-empty C text, e.g. {EXAMPLES['declaration']}")
    if len(decl.encode("utf-8")) > MAX_DECL_BYTES:
        raise AnnotationError(f"declaration larger than {MAX_DECL_BYTES} bytes")
    if not DECL_CHARS_RE.fullmatch(decl):
        raise AnnotationError(f"declaration contains {_bad_chars(decl, DECL_CHAR)}: it may only contain "
                              "identifiers, digits, whitespace and * ; , { } [ ] ( ) : = - (no '#' preprocessor lines, comments "
                              f"or quotes), e.g. {EXAMPLES['declaration']}")
    if not re.match(r"^\s*(struct|union|enum|typedef)\b", decl):
        raise AnnotationError(f"declaration must start with struct, union, enum or typedef, e.g. {EXAMPLES['declaration']}")
    if decl.count("{") != decl.count("}"):
        raise AnnotationError(f"unbalanced braces ({decl.count('{')} '{{' and {decl.count('}')} '}}'), "
                              f"e.g. {EXAMPLES['declaration']}")
    names = [{"kind": k, "name": n} for k, n in DECL_NAMES_RE.findall(decl)]
    if decl.lstrip().startswith("typedef"):
        names += [{"kind": "typedef", "name": n} for n in TYPEDEF_NAME_RE.findall(decl)]
    if not names:
        raise AnnotationError("no named struct/union/enum (or typedef name) found in the declaration, "
                              f"e.g. {EXAMPLES['declaration']}")
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
        change = (body or {}).get("change")
        out.append({"evidence_id": r["evidence_id"], "revision": r["revision"], "change": change,
                    "who": who_label(change)})
    return out


def who_label(change: dict[str, Any] | None) -> str:
    """Who made a stored change, in the words the app shows: a person, a model (proposal) or a test (confirmation)."""
    author = (change or {}).get("author")
    if author == "model":
        return "model-proposed"
    if author == "test":
        return "confirmed by test"
    return "person"


def check_value(what: str, value: str) -> dict[str, Any]:
    """Validate one value with the same grammar the edit operations use (no rizin, nothing stored): the app's
    validate-as-you-type. Returns {ok, normalized | message, example}."""
    checks = {"name": lambda v: check_ident(v), "type": lambda v: check_ctype(v), "prototype": lambda v: check_prototype(v)[0],
              "comment": lambda v: check_comment(v), "declaration": lambda v: ", ".join(
                  f"{n['kind']} {n['name']}" for n in check_declaration(v)[1])}
    if what not in checks:
        raise AnnotationError(f"unknown value kind {what!r}; expected one of {', '.join(checks)}")
    try:
        return {"ok": True, "what": what, "normalized": checks[what](value), "example": EXAMPLES[what]}
    except AnnotationError as e:
        return {"ok": False, "what": what, "message": str(e), "example": EXAMPLES[what]}


def state_at(cases: Any, case_id: str, module_id: str, revision: int) -> tuple[dict[str, Any], str]:
    """The complete annotation state stored at ``revision`` (for a rollback). Revision 0 is the state before any annotation
    (undoing the first change). Raises AnnotationError when there is no such revision."""
    if int(revision) == 0:
        return empty(), ""
    for r in cases.list_evidence(case_id, kind=KIND, module_id=module_id):
        if int(r["revision"]) == int(revision):
            body = cases.blobs.get_json(r["blob_sha"]) if r.get("blob_sha") else {}
            st = body.get("state") if isinstance(body, dict) else None
            out = empty()
            if isinstance(st, dict):
                out.update({k: copy.deepcopy(st.get(k, out[k])) for k in out})
            return out, r["evidence_id"]
    raise AnnotationError(f"no annotation revision {revision} on this module (see the history)")


# ----------------------------------------------------------------------------------------------- provenance (R5)
# Every annotation entry may carry ``prov``: who proposed it and whether a person accepted it or a test confirmed it.
# ``origin`` model + ``status`` proposed = "model-proposed": shown labelled as such until accepted / confirmed, and it can be
# rejected (the entry is removed, or the entry it replaced is restored) or the whole module rolled back to an older revision.
PROV_STATUSES = ("proposed", "accepted", "confirmed")
REVIEW_DECISIONS = ("accept", "reject", "confirm")
_PROV_KEYS = ("model", "provider", "task", "call_id", "evidence_id", "confidence", "rationale")


def _clean_prov(p: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _PROV_KEYS:
        v = (p or {}).get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        else:
            out[k] = check_comment(str(v)[:300].replace("\n", " ").replace("\r", " "))
    return out


def entry_for(state: dict[str, Any], kind: str, key: str, local: str | None = None, *, create: bool = False) -> dict[str, Any] | None:
    """The annotation entry addressed by (kind, key[, local]). kind: function | global | local | comment."""
    if kind == "function":
        table = state["functions"]
    elif kind == "global":
        table = state["globals"]
    elif kind == "comment":
        table = state["comments"]
    elif kind == "local":
        if not local:
            raise AnnotationError("a local needs its original variable name")
        if create:
            return state["locals"].setdefault(key, {}).setdefault(local, {})
        return (state["locals"].get(key) or {}).get(local)
    else:
        raise AnnotationError(f"unknown annotation kind {kind!r}; expected function, global, local or comment")
    if create:
        return table.setdefault(key, {})
    return table.get(key)


def change_target(change: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """(kind, key, local) of the entry a stored change touched, or None."""
    a = change.get("action")
    if a == "rename":
        if change.get("kind") == "local":
            return "local", change["function"], change.get("original") or change.get("old")
        return change.get("kind"), change.get("addr"), None
    if a == "set_type":
        if change.get("kind") == "local":
            return "local", change["function"], change.get("variable")
        return "function", change.get("addr"), None
    if a == "comment":
        return "comment", change.get("addr"), None
    return None


def mark(state: dict[str, Any], before: dict[str, Any], change: dict[str, Any], *, author: str,
         provenance: dict[str, Any] | None = None) -> None:
    """Stamp the entry a change touched with its provenance. ``before`` is the state before the change (to restore on reject)."""
    tgt = change_target(change)
    if not tgt or not tgt[1]:
        return
    kind, key, local = tgt
    ent = entry_for(state, kind, key, local)
    if ent is None:                       # e.g. an emptied comment: nothing left to label
        return
    if author == "model":
        old = entry_for(before, kind, key, local)
        prev = (old or {}).get("prov") or {}
        p = {"origin": "model", "status": "proposed", "at": now_iso(), **_clean_prov(provenance)}
        if prev.get("origin") == "model" and prev.get("status") == "proposed":
            p["replaced"] = prev.get("replaced")          # keep what the FIRST model proposal replaced
        else:
            p["replaced"] = copy.deepcopy(old) if old else None      # restored exactly (with its own provenance) on reject
        if change.get("old") and kind in ("function", "global", "local") and p.get("replaced") is None:
            p["analysis_name"] = str(change["old"])[:200]
        ent["prov"] = p
    else:
        ent["prov"] = {"origin": "user", "status": "accepted", "at": now_iso()}


def _status_label(p: dict[str, Any] | None) -> str:
    p = p or {}
    if p.get("origin") != "model":
        return "user"
    return {"proposed": "model-proposed", "accepted": "accepted by you", "confirmed": "confirmed by a test"}.get(p.get("status"), "model-proposed")


def proposals(state: dict[str, Any], *, only_open: bool = False) -> list[dict[str, Any]]:
    """Every model-originated entry (open proposals first), in a shape the app and MCP show directly."""
    out: list[dict[str, Any]] = []

    def add(kind: str, key: str, ent: dict[str, Any], local: str | None = None) -> None:
        p = ent.get("prov") or {}
        if p.get("origin") != "model" or (only_open and p.get("status") != "proposed"):
            return
        value = ent.get("text") if kind == "comment" else (ent.get("name") or ent.get("prototype") or ent.get("type"))
        out.append({"kind": kind, "key": key, "local": local, "value": value, "prototype": ent.get("prototype"),
                    "type": ent.get("type"), "status": p.get("status"), "label": _status_label(p), "model": p.get("model"),
                    "task": p.get("task"), "at": p.get("at"), "replaced": p.get("replaced"), "analysis_name": p.get("analysis_name"),
                    "confirmed_by": p.get("confirmed_by"), "accepted_at": p.get("accepted_at"), "rationale": p.get("rationale")})
    for k, e in (state.get("functions") or {}).items():
        add("function", k, e)
    for k, e in (state.get("globals") or {}).items():
        add("global", k, e)
    for f, vars_ in (state.get("locals") or {}).items():
        for orig, e in vars_.items():
            add("local", f, e, orig)
    for k, e in (state.get("comments") or {}).items():
        add("comment", k, e)
    out.sort(key=lambda r: (r["status"] != "proposed", r["kind"], r["key"], r["local"] or ""))
    return out


def review(state: dict[str, Any], kind: str, key: str, decision: str, *, local: str | None = None,
           evidence: str | None = None) -> dict[str, Any]:
    """accept (a person) | confirm (a test; ``evidence`` names it) | reject (remove / restore what it replaced). Returns the change."""
    if decision not in REVIEW_DECISIONS:
        raise AnnotationError(f"unknown decision {decision!r}; expected one of {REVIEW_DECISIONS}")
    ent = entry_for(state, kind, key, local)
    p = (ent or {}).get("prov") or {}
    if ent is None or p.get("origin") != "model":
        raise AnnotationError(f"no model-proposed {kind} annotation at {key}" + (f" ({local})" if local else ""))
    change: dict[str, Any] = {"action": "review", "decision": decision, "kind": kind, "key": key, "local": local,
                              "value": ent.get("text") if kind == "comment" else ent.get("name") or ent.get("prototype") or ent.get("type"),
                              "model": p.get("model")}
    if decision == "accept":
        p.update(status="accepted", accepted_at=now_iso())
        ent["prov"] = p
    elif decision == "confirm":
        if not evidence or not isinstance(evidence, str):
            raise AnnotationError("confirm needs the test or evidence that confirms the annotation")
        p.update(status="confirmed", confirmed_by=check_comment(evidence[:300]), confirmed_at=now_iso())
        ent["prov"] = p
        change["evidence"] = p["confirmed_by"]
    else:
        replaced = p.get("replaced")
        tables = {"function": state["functions"], "global": state["globals"], "comment": state["comments"]}
        if kind == "local":
            vars_ = state["locals"].get(key) or {}
            if replaced:
                vars_[local] = copy.deepcopy(replaced)
            else:
                vars_.pop(local, None)
            if not vars_:
                state["locals"].pop(key, None)
        elif replaced:
            tables[kind][key] = copy.deepcopy(replaced)
        else:
            tables[kind].pop(key, None)
        change["restored"] = replaced
    return change


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
def _tagged(ent: dict[str, Any], text: str) -> str:
    p = ent.get("prov") or {}
    return f"[model-proposed] {text}" if p.get("origin") == "model" and p.get("status") == "proposed" else text


def overlay_disasm(ops: list[dict[str, Any]], state: dict[str, Any]) -> int:
    comments = state.get("comments") or {}
    n = 0
    for op in ops:
        off = op.get("offset")
        if isinstance(off, int):
            c = comments.get(f"0x{off:x}")
            if c:
                op["user_comment"] = c["text"]
                if (c.get("prov") or {}).get("origin") == "model":      # the label travels next to the text
                    op["comment_status"] = _status_label(c.get("prov"))
                n += 1
    return n


def comments_in(state: dict[str, Any], lo: int, hi: int) -> list[tuple[int, str]]:
    """Comments in [lo, hi) plus a note for every model-proposed name in that range (labelled until accepted)."""
    out = []
    for k, c in (state.get("comments") or {}).items():
        a = int(k, 16)
        if lo <= a < hi:
            out.append((a, _tagged(c, c["text"])))
    for k, e in (state.get("functions") or {}).items():
        a = int(k, 16)
        if lo <= a < hi and e.get("name") and ((e.get("prov") or {}).get("status") == "proposed"):
            out.append((a, f"[model-proposed] function name {e['name']} (not yet accepted)"))
    for k, vars_ in (state.get("locals") or {}).items():
        a = int(k, 16)
        if lo <= a < hi:
            for orig, e in vars_.items():
                if e.get("name") and (e.get("prov") or {}).get("status") == "proposed":
                    out.append((a, f"[model-proposed] local {orig} -> {e['name']} (not yet accepted)"))
    return sorted(out)


def overlay_decompiled(text: str, comments: list[tuple[int, str]]) -> str:
    if not comments:
        return text
    head = "".join(f"// [annotation @0x{a:x}] {t}\n" for a, t in comments)
    return head + text
