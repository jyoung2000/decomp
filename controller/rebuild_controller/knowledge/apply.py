"""Apply promoted knowledge without model calls: signature scans and rewrite rules. Reuse is recorded as evidence."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .store import KnowledgeStore


def _compile(pattern: str) -> list[int | None]:
    return [None if t == "??" else int(t, 16) for t in pattern.split()]


def scan_signatures(store: KnowledgeStore, data: bytes, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Find promoted signatures applicable to `context` (arch/abi/...) in `data`. Returns matches with offsets."""
    out = []
    for k in store.applicable("signature", context):
        body = store.body(k["knowledge_id"])["body"]
        toks = _compile(body["pattern"])
        n = len(toks)
        first = toks[0]
        hits = []
        i = 0
        while True:
            if first is None:
                j = i
            else:
                j = data.find(bytes([first]), i)
                if j < 0:
                    break
            if j + n > len(data):
                break
            if all(t is None or data[j + q] == t for q, t in enumerate(toks)):
                hits.append(j)
                if len(hits) >= 1000:
                    break
            i = j + 1
            if i >= len(data):
                break
        if hits:
            out.append({"knowledge_id": k["knowledge_id"], "name": k["name"], "version": k["version"], "symbol": body.get("symbol"), "offsets": hits[:50], "count": len(hits)})
    return out


def apply_rewrites(store: KnowledgeStore, text: str, context: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    applied = []
    for k in store.applicable("rewrite", context):
        body = store.body(k["knowledge_id"])["body"]
        new, n = re.subn(body["match"], body.get("replace", ""), text, flags=re.M)
        if n:
            applied.append({"knowledge_id": k["knowledge_id"], "name": k["name"], "version": k["version"], "replacements": n})
            text = new
    return text, applied


def reuse_on_module(studio, case_id: str, module_id: str, *, ai_guard=None) -> dict[str, Any]:
    """Demonstration entry point: reuse promoted knowledge on a module with the AI adapter replaced by a raising guard."""
    mod = studio.cases.get_module(module_id)
    case = studio.cases.get_case(case_id)
    data = (Path(case["source_root"]) / mod["rel_path"]).read_bytes()
    context = {"arch": mod.get("arch"), "format": mod["format"], "profile": mod["profile"]}
    before = studio.db.query_one("SELECT COUNT(*) AS n FROM ai_calls")["n"]
    matches = scan_signatures(studio.knowledge, data, context)
    after = studio.db.query_one("SELECT COUNT(*) AS n FROM ai_calls")["n"]
    ev = studio.cases.add_evidence(case_id, "knowledge_reuse", f"Signature reuse on {mod['rel_path']}", body={"context": context, "matches": matches, "ai_calls_during": after - before},
                                   module_id=module_id, inputs={"module_sha": mod["sha256"], "knowledge": [m["knowledge_id"] for m in matches]}, producer="knowledge")
    return {"evidence_id": ev["evidence_id"], "matches": matches, "ai_calls_during": after - before, "context": context,
            "applicable_entries": len(studio.knowledge.applicable("signature", context))}
