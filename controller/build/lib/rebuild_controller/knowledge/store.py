"""Knowledge store: executable, versioned capabilities with proposal → isolated validation → promotion/quarantine/rollback.

Kinds: signature (byte-pattern → symbol/type), type_lib, parser (candidate plugin validated on golden+malformed samples),
recipe (deterministic action/replay recipe), rewrite (compiler idiom → source template), template, replay, fixture.
Models may propose once; promotion requires declared acceptance checks to pass in an isolated run. Trusted controller code
is never patched at runtime.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from ..cases import BlobStore
from ..config import Settings
from ..events import EventLog
from ..ids import new_id, now_iso, sha256_bytes, stable_json_hash
from ..store.db import Database, loads

KINDS = {"signature", "type_lib", "parser", "recipe", "rewrite", "template", "replay", "fixture"}
STATES = {"proposed", "validating", "promoted", "quarantined", "rolled_back"}

Validator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class KnowledgeStore:
    def __init__(self, db: Database, events: EventLog, settings: Settings):
        self.db = db
        self.events = events
        self.blobs = BlobStore(settings.blobs_dir)
        self._validators: dict[str, Validator] = {
            "signature": validate_signature, "rewrite": validate_rewrite, "replay": validate_replay, "recipe": validate_replay,
            "parser": validate_parser, "template": validate_template, "type_lib": validate_type_lib, "fixture": validate_fixture,
        }

    # -- proposals ------------------------------------------------------
    def propose(self, *, kind: str, name: str, body: dict[str, Any], constraints: dict[str, Any], author: str, source: str,
                confidence: float, evidence: list[str] | None = None, acceptance: dict[str, Any] | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown knowledge kind {kind}")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name):
            raise ValueError("invalid knowledge name")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError("confidence out of range")
        prev = self.db.query_one("SELECT knowledge_id, version FROM knowledge WHERE kind=? AND name=? ORDER BY version DESC LIMIT 1", (kind, name))
        version = (prev["version"] + 1) if prev else 1
        payload = {"body": body, "acceptance": acceptance or {}}
        sha = self.blobs.put_json(payload)
        kid = new_id("kn")
        ts = now_iso()
        self.db.insert("knowledge", {"knowledge_id": kid, "kind": kind, "name": name, "version": version, "state": "proposed", "constraints": constraints,
                                     "body_sha": sha, "author": author, "source": source, "confidence": confidence, "evidence": evidence or [],
                                     "regression": {}, "lineage": prev["knowledge_id"] if prev else None, "created_at": ts, "updated_at": ts})
        k = self.get(kid)
        self.events.emit("knowledge.updated", {"knowledge": k}, )
        return k

    def get(self, knowledge_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM knowledge WHERE knowledge_id=?", (knowledge_id,))
        if not r:
            raise KeyError(knowledge_id)
        for k in ("constraints", "evidence", "regression"):
            r[k] = loads(r[k], {} if k != "evidence" else [])
        return r

    def body(self, knowledge_id: str) -> dict[str, Any]:
        return self.blobs.get_json(self.get(knowledge_id)["body_sha"])

    def list(self, *, kind: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT knowledge_id FROM knowledge WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if state:
            sql += " AND state=?"; params.append(state)
        return [self.get(r["knowledge_id"]) for r in self.db.query(sql + " ORDER BY kind, name, version", tuple(params))]

    # -- validation / promotion ----------------------------------------
    def validate(self, knowledge_id: str) -> dict[str, Any]:
        k = self.get(knowledge_id)
        if k["state"] == "promoted":
            return k
        self._set_state(knowledge_id, "validating")
        payload = self.body(knowledge_id)
        validator = self._validators.get(k["kind"])
        try:
            result = validator(payload["body"], payload.get("acceptance", {})) if validator else {"ok": False, "error": "no validator for kind"}
        except Exception as e:  # a crashing proposal is quarantined with evidence
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
        result["validated_at"] = now_iso()
        state = "promoted" if result.get("ok") else "quarantined"
        if state == "promoted":
            # demote earlier promoted versions of the same name (kept for rollback)
            self.db.execute("UPDATE knowledge SET state='rolled_back', updated_at=? WHERE kind=? AND name=? AND state='promoted' AND knowledge_id<>?", (now_iso(), k["kind"], k["name"], knowledge_id))
        self.db.update("knowledge", "knowledge_id", knowledge_id, {"state": state, "regression": result, "updated_at": now_iso()})
        k = self.get(knowledge_id)
        self.events.emit("knowledge.updated", {"knowledge": k})
        return k

    def rollback(self, knowledge_id: str) -> dict[str, Any] | None:
        k = self.get(knowledge_id)
        self._set_state(knowledge_id, "rolled_back")
        if k["lineage"]:
            prev = self.get(k["lineage"])
            if prev["regression"].get("ok"):
                self._set_state(prev["knowledge_id"], "promoted")
                return self.get(prev["knowledge_id"])
        return None

    def _set_state(self, knowledge_id: str, state: str) -> None:
        assert state in STATES
        self.db.update("knowledge", "knowledge_id", knowledge_id, {"state": state, "updated_at": now_iso()})
        self.events.emit("knowledge.updated", {"knowledge": self.get(knowledge_id)})

    # -- reuse ----------------------------------------------------------
    def applicable(self, kind: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Promoted entries whose constraints match the context (arch/abi/version/engine). Non-matching entries are never reused."""
        out = []
        for k in self.list(kind=kind, state="promoted"):
            if _constraints_match(k["constraints"], context):
                out.append(k)
        return out

    def check_integrity(self) -> list[dict[str, Any]]:
        """Detect corrupted entries: missing blob or hash mismatch → quarantine."""
        bad = []
        for k in self.list():
            p = self.blobs.path_for(k["body_sha"])
            if not p.exists() or sha256_bytes(p.read_bytes()) != k["body_sha"]:
                self.db.update("knowledge", "knowledge_id", k["knowledge_id"], {"state": "quarantined", "regression": {"ok": False, "error": "integrity check failed"}, "updated_at": now_iso()})
                bad.append(k["knowledge_id"])
        return [self.get(b) for b in bad]


def _constraints_match(constraints: dict[str, Any], context: dict[str, Any]) -> bool:
    for key, want in constraints.items():
        have = context.get(key)
        if have is None:
            return False
        if isinstance(want, list):
            if have not in want:
                return False
        elif want != have:
            return False
    return True


# ---------------------------------------------------------------- validators (pure, deterministic)

def validate_signature(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    """body: {pattern: hex with ?? wildcards, symbol, arch}; acceptance: {positives: [hex], negatives: [hex]}"""
    pat = body.get("pattern", "")
    toks = pat.split()
    if not toks or any(not (t == "??" or re.fullmatch(r"[0-9a-fA-F]{2}", t)) for t in toks):
        return {"ok": False, "error": "invalid pattern"}

    def matches(hexstr: str) -> bool:
        b = bytes.fromhex(hexstr)
        n = len(toks)
        for i in range(0, len(b) - n + 1):
            if all(t == "??" or int(t, 16) == b[i + j] for j, t in enumerate(toks)):
                return True
        return False
    pos = acceptance.get("positives", []); neg = acceptance.get("negatives", [])
    if not pos:
        return {"ok": False, "error": "no positive samples declared"}
    fp = [n for n in neg if matches(n)]
    fn = [p for p in pos if not matches(p)]
    return {"ok": not fp and not fn, "positives": len(pos), "negatives": len(neg), "false_positives": len(fp), "false_negatives": len(fn)}


def validate_rewrite(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    """body: {match: regex, replace: str}; acceptance: {cases: [{input, expected}], must_not_change: [str]}"""
    try:
        rx = re.compile(body["match"], re.M)
    except (re.error, KeyError) as e:
        return {"ok": False, "error": f"bad regex: {e}"}
    fails = []
    for c in acceptance.get("cases", []):
        got = rx.sub(body.get("replace", ""), c["input"])
        if got != c["expected"]:
            fails.append({"input": c["input"][:200], "expected": c["expected"][:200], "got": got[:200]})
    for s in acceptance.get("must_not_change", []):
        if rx.sub(body.get("replace", ""), s) != s:
            fails.append({"unexpected_change": s[:200]})
    if not acceptance.get("cases"):
        return {"ok": False, "error": "no acceptance cases declared"}
    return {"ok": not fails, "cases": len(acceptance.get("cases", [])), "failures": fails}


def validate_replay(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    """body: {actions: [{type, selector|args, postcondition}]}; acceptance: {required_postconditions: int}"""
    actions = body.get("actions", [])
    if not actions:
        return {"ok": False, "error": "empty recipe"}
    allowed = {"click", "fill", "press", "goto", "wait", "wait_for", "offline", "online", "reload", "run", "key", "type", "screenshot", "wait_sw"}
    bad = [a for a in actions if a.get("type") not in allowed]
    if bad:
        return {"ok": False, "error": f"unsupported action types: {[a.get('type') for a in bad]}"}
    with_post = sum(1 for a in actions if a.get("postcondition"))
    need = int(acceptance.get("required_postconditions", 1))
    return {"ok": with_post >= need, "actions": len(actions), "postconditions": with_post}


def validate_parser(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    """body: {struct: [{name, type:u8|u16|u32|u64|i32|bytes:N|str:N}], magic?: hex}; acceptance: {golden: [{hex, expect: {...}}], malformed: [hex]}"""
    import struct as st
    fmt = {"u8": "B", "u16": "<H", "u32": "<I", "u64": "<Q", "i32": "<i", "i64": "<q"}

    def parse(hexstr: str) -> dict[str, Any]:
        b = bytes.fromhex(hexstr); off = 0; out = {}
        magic = body.get("magic")
        if magic:
            m = bytes.fromhex(magic)
            if b[:len(m)] != m:
                raise ValueError("bad magic")
            off = len(m)
        for f in body.get("struct", []):
            t = f["type"]
            if t in fmt:
                n = st.calcsize(fmt[t]); out[f["name"]] = st.unpack(fmt[t], b[off:off + n])[0]; off += n
            elif t.startswith("bytes:") or t.startswith("str:"):
                n = int(t.split(":")[1]); chunk = b[off:off + n]
                if len(chunk) < n:
                    raise ValueError("truncated")
                out[f["name"]] = chunk.hex() if t.startswith("bytes") else chunk.decode("utf-8", "replace"); off += n
            else:
                raise ValueError(f"unknown type {t}")
        return out
    golden = acceptance.get("golden", []); malformed = acceptance.get("malformed", [])
    if not golden:
        return {"ok": False, "error": "no golden samples"}
    fails = []
    for g in golden:
        try:
            got = parse(g["hex"])
            if any(got.get(k) != v for k, v in g.get("expect", {}).items()):
                fails.append({"golden": g["hex"][:64], "got": got})
        except Exception as e:
            fails.append({"golden": g["hex"][:64], "error": str(e)})
    accepted_malformed = []
    for m in malformed:
        try:
            parse(m); accepted_malformed.append(m[:64])
        except Exception:
            pass
    return {"ok": not fails and not accepted_malformed, "golden": len(golden), "malformed": len(malformed), "failures": fails, "malformed_accepted": accepted_malformed}


def validate_template(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    text = body.get("text", "")
    placeholders = set(re.findall(r"\{\{(\w+)\}\}", text))
    declared = set(body.get("placeholders", []))
    missing = placeholders - declared
    return {"ok": bool(text) and not missing, "placeholders": sorted(placeholders), "undeclared": sorted(missing)}


def validate_type_lib(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    types = body.get("types", {})
    bad = [n for n, t in types.items() if not isinstance(t, dict) or "size" not in t]
    return {"ok": bool(types) and not bad, "types": len(types), "invalid": bad}


def validate_fixture(body: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    return {"ok": "scenarios" in body and bool(body["scenarios"]), "scenarios": len(body.get("scenarios", []))}
