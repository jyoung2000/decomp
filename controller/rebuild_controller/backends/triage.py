"""Triage backend for input kinds that are detected but NOT recoverable: Unity IL2CPP, GameMaker, Unreal, Mach-O.

It is deliberately honest: it never decompiles anything. ``describe`` returns the detection, the plain-language support statement and the
candidate future tools; ``inspect`` returns what the built-in bounded parsers can still give (Mach-O slice table and flags, GameMaker
chunk table and strings, Unreal pak footer, IL2CPP metadata identifier names). Both record evidence with the parser version and the
module sha256 so the plan can show exactly what was (not) done. All lists are capped and report ``truncated``.
"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path
from typing import Any

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..ids import sha256_file
from . import support as sp
from .archive import RecoveryToolProbe, record_evidence, resolve_studio

BACKEND_ID = "triage"
PARSER_VERSION = "1"
PROFILES = ["unity_il2cpp", "gamemaker", "unreal", "native_macho"]


def _smoke_macho() -> bytes:
    # thin little-endian x86_64 executable header, zero load commands
    return b"\xcf\xfa\xed\xfe" + struct.pack("<iiIIII", 0x01000007, 3, 2, 0, 0, 0x200085) + b"\0" * 4


class TriageBackend(BackendAdapter):
    backend_id = BACKEND_ID

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def probe(self) -> BackendInfo:
        tool = RecoveryToolProbe("builtin-parsers", Availability.INSTALLED, version=PARSER_VERSION, license="project-internal",
                                 detail="pure-Python bounded header parsers; no external tool and no code recovery", integration="library")
        return BackendInfo(
            backend_id=BACKEND_ID, title="Triage for detected-only kinds (IL2CPP, GameMaker, Unreal, Mach-O)",
            formats=["il2cpp_metadata", "gamemaker_data", "unreal_pak", "unreal_iostore", "macho"],
            platforms=["windows", "linux", "macos"], profiles=list(PROFILES),
            operations=[
                Operation("describe", "Detection + plain-language support statement + candidate future tools", {"path": "path"}, {"support": "dict"}),
                Operation("inspect", "What the built-in parsers can still give: headers, chunk tables, identifier names, pak footer",
                          {"module_path": "path"}, {"inspect": "dict"}),
            ],
            tools=[tool], resources={"ram_mb": 50, "recovers_code": False,
                                     "note": "detected-only kinds: inventory and evidence, never decompilation"},
            tested_support=[], experimental=True)

    def smoke(self) -> ToolProbe:
        tool = self.probe().tools[0]
        with tempfile.TemporaryDirectory(prefix="rs-triage-smoke-") as td:
            p = Path(td) / "sample.macho"
            p.write_bytes(_smoke_macho())
            try:
                info = sp.inspect_macho(p)
            except Exception as e:  # noqa: BLE001
                tool.detail = f"smoke failed: {type(e).__name__}: {e}"
                return tool
            if info["architectures"] == ["x86_64"] and info["layout"] == "thin":
                tool.availability = Availability.USABLE
                tool.detail = "parsed a built-in thin Mach-O x86_64 header"
            else:
                tool.detail = f"smoke failed: unexpected result {info['architectures']}"
        return tool

    def op_describe(self, ctx: Any, path: str, **kw: Any) -> OperationResult:
        return self.describe(path, ctx=ctx, **kw)

    def op_inspect(self, ctx: Any, module_path: str, **kw: Any) -> OperationResult:
        return self.inspect(module_path, ctx=ctx, **kw)

    def describe(self, path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None, module_id: str | None = None,
                 **_: Any) -> OperationResult:
        from .detect import detect_path
        p = Path(path)
        if not p.is_file():
            return OperationResult(ok=False, error=f"module not found: {p}")
        d = detect_path(p)
        if d["support"] is None:
            return OperationResult(ok=False, error=f"{p.name}: profile {d['profile']} has no support record", data={"detection": d})
        body = {"schema": 1, "module": {"path": str(p), "sha256": sha256_file(p)}, "detection": {k: v for k, v in d.items() if k != "support"},
                "support": d["support"], "recovered": False, "equivalence_claimed": False}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "triage.support", f"Support statement: {p.name}", body, module_id=module_id,
                              inputs={"op": "describe", "backend": BACKEND_ID, "tool": "builtin-parsers", "tool_version": PARSER_VERSION,
                                      "module_sha256": body["module"]["sha256"]}, producer=BACKEND_ID)
        return OperationResult(ok=True, data=body, evidence_ids=[eid] if eid else [])

    def inspect(self, module_path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None, module_id: str | None = None,
                **_: Any) -> OperationResult:
        from .detect import detect_path
        p = Path(module_path)
        if not p.is_file():
            return OperationResult(ok=False, error=f"module not found: {p}")
        det = detect_path(p)
        sha = sha256_file(p)
        try:
            info = sp.inspect_path(p, det["profile"])
            err = None
        except (ValueError, OSError, struct.error) as e:
            info, err = {"can_still_get": [], "blocker": str(e)}, f"{type(e).__name__}: {e}"
        body = {"schema": 1, "module": {"path": str(p), "sha256": sha, "size": p.stat().st_size}, "profile": det["profile"],
                "support_status": det.get("support_status"), "inspect": info, "parse_error": err,
                "support_statement": (det["support"] or {}).get("statement"), "recovered": False, "equivalence_claimed": False,
                "claims": "Header/table facts only. No code was recovered and nothing here implies a rebuild is possible."}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "triage.inspect", f"Triage inspect: {p.name}", body, module_id=module_id,
                              inputs={"op": "inspect", "backend": BACKEND_ID, "tool": "builtin-parsers", "tool_version": PARSER_VERSION,
                                      "module_sha256": sha}, producer=BACKEND_ID)
        truncated = bool(info.get("identifiers_truncated") or info.get("strings_truncated"))
        return OperationResult(ok=err is None, data=body, evidence_ids=[eid] if eid else [], truncated=truncated, error=err)
