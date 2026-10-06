"""Backend registry and `doctor` report."""
from __future__ import annotations

from typing import Any

from .contract import Availability, BackendAdapter, BackendInfo, ToolProbe


class BackendRegistry:
    def __init__(self, data_dir=None):
        self._adapters: dict[str, BackendAdapter] = {}
        self._cache: dict[str, BackendInfo] = {}
        self.data_dir = data_dir

    def register(self, adapter: BackendAdapter) -> None:
        self._adapters[adapter.backend_id] = adapter

    def get(self, backend_id: str) -> BackendAdapter:
        return self._adapters[backend_id]

    def ids(self) -> list[str]:
        return sorted(self._adapters)

    def info(self, backend_id: str, *, refresh: bool = False) -> BackendInfo:
        if refresh or backend_id not in self._cache:
            self._cache[backend_id] = self._adapters[backend_id].probe()
        return self._cache[backend_id]

    def for_profile(self, profile: str, *, min_availability: Availability = Availability.INSTALLED) -> list[BackendAdapter]:
        order = list(Availability)
        out = []
        for bid in self.ids():
            info = self.info(bid)
            if profile in info.profiles and order.index(info.availability) >= order.index(min_availability):
                out.append(self._adapters[bid])
        return out

    def doctor(self, *, smoke: bool = False, verify: bool = False) -> dict[str, Any]:
        """Availability ladder: missing → detected → installed → usable (smoke op passed now) → verified (fixture regression passed
        on this host with these exact tool versions; recorded in <data_dir>/backend-verification.json)."""
        from . import verification
        report: dict[str, Any] = {"backends": [], "summary": {}}
        counts = {a.value: 0 for a in Availability}
        for bid in self.ids():
            info = self.info(bid, refresh=True)
            versions = {t.name: t.version for t in info.tools if not t.optional}
            if verify and self.data_dir is not None and info.availability in (Availability.INSTALLED, Availability.USABLE, Availability.VERIFIED):
                verification.run_regression(bid, self.data_dir, versions)
            rec = verification.is_verified(bid, self.data_dir, versions) if self.data_dir is not None else None
            if rec and info.availability in (Availability.INSTALLED, Availability.USABLE):
                for t in info.tools:
                    if not t.optional and t.availability in (Availability.INSTALLED, Availability.USABLE):
                        t.availability = Availability.VERIFIED
            entry = info.to_dict()
            if rec:
                entry["verification"] = {k: rec[k] for k in ("when", "command", "host", "versions")}
            if smoke and info.availability in (Availability.INSTALLED, Availability.DETECTED):
                try:
                    probe: ToolProbe = self._adapters[bid].smoke()
                    entry["smoke"] = probe.to_dict()
                    if probe.availability == Availability.USABLE:
                        proves = {probe.name, *((getattr(probe, "extra", None) or {}).get("proves", []))}   # a smoke may prove its runtime too (cfr proves java)
                        for t in info.tools:
                            if t.name in proves and t.availability != Availability.VERIFIED:
                                t.availability = Availability.USABLE
                        entry = info.to_dict(); entry["smoke"] = probe.to_dict()
                except Exception as e:  # smoke must never crash doctor
                    entry["smoke"] = {"error": f"{type(e).__name__}: {e}"}
            counts[info.availability.value] += 1
            report["backends"].append(entry)
        report["summary"] = counts
        return report
