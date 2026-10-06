"""Recovery backends. Each module exposes a BackendAdapter; register_backends wires the ones importable on this host."""
from __future__ import annotations

from ..adapters.registry import BackendRegistry
from ..config import Settings


def register_backends(registry: BackendRegistry, settings: Settings) -> None:
    # Each import is isolated: a missing optional dependency must not take down the controller.
    for modname, clsname in [
        ("rizin_worker", "RizinBackend"),
        ("ilspy", "ILSpyBackend"),
        ("gdre", "GDREBackend"),
        ("jvm", "JVMBackend"),
        ("triage", "TriageBackend"),
        ("jsweb", "JSWebBackend"),
        ("ghidra", "GhidraBackend"),
    ]:
        try:
            mod = __import__(f"{__name__}.{modname}", fromlist=[clsname])
            registry.register(getattr(mod, clsname)(settings))
        except Exception as e:  # pragma: no cover - reported via doctor as missing backend module
            registry.register(_BrokenBackend(modname, f"{type(e).__name__}: {e}"))


from ..adapters.contract import Availability, BackendAdapter, BackendInfo, ToolProbe  # noqa: E402


class _BrokenBackend(BackendAdapter):
    def __init__(self, name: str, error: str):
        self.backend_id = name
        self.error = error

    def probe(self) -> BackendInfo:
        return BackendInfo(self.backend_id, f"{self.backend_id} (failed to load)", [], [], [], [],
                           [ToolProbe(self.backend_id, Availability.MISSING, detail=self.error)])

    def smoke(self) -> ToolProbe:
        return ToolProbe(self.backend_id, Availability.MISSING, detail=self.error)
