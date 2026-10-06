"""Unified backend adapter contract shared by GUI, CLI, Cutter plugin and MCP.

Availability states are distinct and never collapsed:
  missing   - executable/library not found
  detected  - found on disk, version unknown/unprobed
  installed - version probed successfully
  usable    - a smoke operation succeeded on this host
  verified  - a fixture regression passed on this host with this version
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Availability(str, Enum):
    MISSING = "missing"
    DETECTED = "detected"
    INSTALLED = "installed"
    USABLE = "usable"
    VERIFIED = "verified"


@dataclass
class ToolProbe:
    name: str
    availability: Availability
    path: str | None = None
    version: str | None = None
    detail: str = ""
    prerequisites: list[str] = field(default_factory=list)
    license: str = ""
    source: str = ""          # upstream URL
    pinned: str = ""          # pinned release/commit
    integrity: str = ""       # sha256 of downloaded artifact if known
    integration: str = "cli"  # cli|library|plugin|service
    optional: bool = False    # optional tools never lower the backend's overall availability

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["availability"] = self.availability.value
        return d


@dataclass
class Operation:
    name: str
    description: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    dangerous: bool = False   # dangerous operations are never exposed to models


@dataclass
class OperationResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


@dataclass
class BackendInfo:
    backend_id: str
    title: str
    formats: list[str]           # e.g. ["pe", "elf", "dotnet", "godot_pck", "js_bundle"]
    platforms: list[str]         # host platforms it runs on
    profiles: list[str]          # which detected profiles it serves
    operations: list[Operation]
    tools: list[ToolProbe]
    resources: dict[str, Any] = field(default_factory=dict)   # expected RAM/disk/time
    tested_support: list[str] = field(default_factory=list)   # fixture ids passed on this host
    experimental: bool = False

    @property
    def availability(self) -> Availability:
        required = [t for t in self.tools if not t.optional]
        if not required:
            return Availability.USABLE
        order = list(Availability)
        return min((t.availability for t in required), key=order.index)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id, "title": self.title, "formats": self.formats, "platforms": self.platforms,
            "profiles": self.profiles, "operations": [o.__dict__ for o in self.operations],
            "tools": [t.to_dict() for t in self.tools], "resources": self.resources,
            "tested_support": self.tested_support, "experimental": self.experimental, "availability": self.availability.value,
        }


class BackendAdapter(abc.ABC):
    backend_id: str = ""

    @abc.abstractmethod
    def probe(self) -> BackendInfo:
        """Cheap, side-effect-free probe of tool availability."""

    @abc.abstractmethod
    def smoke(self) -> ToolProbe:
        """Run one real operation on a tiny built-in sample; upgrades availability to usable."""

    def operations(self) -> list[Operation]:
        return self.probe().operations

    def call(self, operation: str, ctx: Any, **kwargs: Any) -> OperationResult:
        fn = getattr(self, f"op_{operation}", None)
        if fn is None:
            return OperationResult(ok=False, error=f"{self.backend_id} has no operation {operation}")
        return fn(ctx, **kwargs)
