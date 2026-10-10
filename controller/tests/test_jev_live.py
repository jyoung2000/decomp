"""Opt-in LIVE JeV check (one real TypeSafe request, a fraction of a cent). Never part of the default suite.

Run (PowerShell):  $env:REBUILD_LIVE_JEV="1"; $env:REBUILD_JEV_KEY="<key>"; pytest -m live tests/test_jev_live.py
Without REBUILD_JEV_KEY the key is taken from the JeV install's documented key file (the same explicit import the
"Use the key from my JeV install" button performs). The key is never printed; the recorded result goes to
<data_dir>/jev/live-check.json (model, choice, confidence, tokens, cost - no key, no request content).
"""
import json
import os
import warnings

import pytest

from rebuild_controller.budget import BudgetLedger
from rebuild_controller.providers import jev as J
from rebuild_controller.providers.secrets import SecretStore

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("REBUILD_LIVE_JEV") != "1", reason="set REBUILD_LIVE_JEV=1 for the live JeV call")]


def test_live_jev_route_advice(db, events, tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        secrets = SecretStore(tmp_path / "s")
    adv = J.JeVRouter(BudgetLedger(db, events), events, tmp_path / "data", secrets=secrets)
    if os.environ.get("REBUILD_JEV_KEY"):
        adv.set_key(os.environ["REBUILD_JEV_KEY"])
    else:
        adv.import_install_key()
    cands = [{"index": 0, "provider": "anthropic", "model": "claude-sonnet-5-5", "locality": "cloud", "price_known": True,
              "input_per_mtok": 3.0, "output_per_mtok": 15.0},
             {"index": 1, "provider": "local", "model": "qwen2.5-coder:14b", "locality": "local", "price_known": True,
              "input_per_mtok": 0.0, "output_per_mtok": 0.0}]
    d = adv.advise("repair", cands, {"needs": [], "est_input_tokens": 6000, "max_output_tokens": 4000}, case_id=None)
    st = adv.status()
    rec = {"source": d.source, "reason": d.reason, "order": d.order, "confidence": d.confidence, "model": d.model,
           "spent_usd": d.spent_usd, "month_spent_usd": st["month"]["spent_usd"], "breaker": st["breaker"]["state"]}
    (adv.dir / "live-check.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print("JeV live check:", json.dumps(rec))
    assert d.source in ("jev", "fallback") and st["month"]["spent_usd"] <= st["monthly_cap_usd"]
    assert d.source == "jev" or d.reason == "low_confidence", rec        # a real, validated answer (or an honest low-confidence one)
    assert (d.model or "").startswith("jev-")
