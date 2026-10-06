from rebuild_controller.adapters import verification
from rebuild_controller.adapters.contract import Availability, BackendAdapter, BackendInfo, ToolProbe
from rebuild_controller.adapters.registry import BackendRegistry


class Fake(BackendAdapter):
    backend_id = "fake"

    def probe(self):
        return BackendInfo("fake", "Fake", ["x"], ["linux"], ["x"], [], [ToolProbe("fake-tool", Availability.INSTALLED, version="1.0"), ToolProbe("opt", Availability.MISSING, optional=True)])

    def smoke(self):
        return ToolProbe("fake-tool", Availability.USABLE, version="1.0")


def test_ladder_missing_detected_installed_usable_verified(tmp_path):
    reg = BackendRegistry(tmp_path); reg.register(Fake())
    assert reg.doctor()["backends"][0]["availability"] == "installed"          # optional missing tool does not lower it
    assert reg.doctor(smoke=True)["backends"][0]["availability"] == "usable"
    # verification record bound to the exact version
    verification.record_path(tmp_path).write_text('{"fake": {"ok": true, "versions": {"fake-tool": "1.0"}, "when": "t", "command": "c", "host": "h"}}')
    r = reg.doctor()["backends"][0]
    assert r["availability"] == "verified" and r["verification"]["host"] == "h"
    verification.record_path(tmp_path).write_text('{"fake": {"ok": true, "versions": {"fake-tool": "0.9"}, "when": "t", "command": "c", "host": "h"}}')
    assert reg.doctor()["backends"][0]["availability"] == "installed"          # version changed → not verified anymore
    verification.record_path(tmp_path).write_text('{"fake": {"ok": false, "versions": {"fake-tool": "1.0"}}}')
    assert reg.doctor()["backends"][0]["availability"] == "installed"          # failed regression never verifies
