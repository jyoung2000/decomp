"""Rust builder toolchain discovery: the private toolchain from Tools setup first, then PATH; blocker/forecast text."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebuild_controller import sandbox
from rebuild_controller.builders import rust
from rebuild_controller.config import Settings
from rebuild_controller.jobs.runner import StageError

EXE = ".exe" if os.name == "nt" else ""


def fake_private(tools: Path, tc: str = "1.97.0-x86_64-pc-windows-gnu") -> Path:
    b = tools / "rust" / "rustup" / "toolchains" / tc / "bin"
    b.mkdir(parents=True)
    (b / f"cargo{EXE}").write_bytes(b"x")
    (b / f"rustc{EXE}").write_bytes(b"x")
    return b


def ctx_for(tmp_path: Path, tools: Path) -> SimpleNamespace:
    st = Settings(data_dir=tmp_path / "data", tools_dir=tools)
    return SimpleNamespace(job=SimpleNamespace(case_id=None, inputs={}), services={"studio": SimpleNamespace(settings=st)}, limits=st.limits,
                           heartbeat=lambda *a, **k: None, log=lambda *a, **k: None, run=lambda *a, **k: SimpleNamespace(text="rustc 1.97.0 (test)"))


def project(tmp_path: Path) -> Path:
    src = tmp_path / "cand" / "source"
    (src / "src").mkdir(parents=True)
    (src / "Cargo.toml").write_text('[package]\nname = "hello"\nversion = "0.1.0"\nedition = "2021"\n')
    (src / "src" / "main.rs").write_text('fn main() { println!("hi"); }\n')
    return src


def test_private_rust_requires_both_binaries_and_picks_the_toolchain_dir(tmp_path):
    assert rust.private_rust(tmp_path / "none") is None
    b = fake_private(tmp_path)
    p = rust.private_rust(tmp_path)
    assert p and Path(p["cargo"]) == b / f"cargo{EXE}" and Path(p["rustc"]) == b / f"rustc{EXE}"
    assert Path(p["rustup_home"]) == tmp_path / "rust" / "rustup" and p["toolchain"] == "1.97.0-x86_64-pc-windows-gnu"
    (b / f"rustc{EXE}").unlink()
    assert rust.private_rust(tmp_path) is None            # half-installed => not used


def test_blocked_message_points_at_the_tools_page_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(rust, "cargo", lambda: None)
    with pytest.raises(StageError) as ei:
        rust.build_rust(ctx_for(tmp_path, tmp_path / "empty-tools"), project(tmp_path), tmp_path / "cand" / "dist")
    assert "not installed" in str(ei.value) and "cargo" in str(ei.value)
    assert "Rust compiler (private)" in ei.value.blocker and "Tools" in ei.value.blocker and "rustup" not in ei.value.blocker.lower()


def test_private_toolchain_wins_over_path_and_keeps_case_local_cargo_home(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    b = fake_private(tools)
    monkeypatch.setattr(rust, "cargo", lambda: str(tmp_path / "path-cargo"))             # a PATH cargo exists but must lose
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable-x86_64-pc-windows-msvc")              # developer overrides must not leak in
    monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-pc-windows-msvc")
    seen = {}

    def fake_run(args, **kw):
        seen.update(args=list(args), env=kw["env"], cwd=kw["cwd"])
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"stop here", timed_out=False, isolation={}, triggered=[])

    monkeypatch.setattr(sandbox, "run", fake_run)
    ctx = ctx_for(tmp_path, tools)
    with pytest.raises(StageError, match="cargo build failed"):
        rust.build_rust(ctx, project(tmp_path), tmp_path / "cand" / "dist")
    assert Path(seen["args"][0]) == b / f"cargo{EXE}"
    env = {k.upper(): v for k, v in seen["env"].items()}
    assert Path(env["RUSTUP_HOME"]) == tools / "rust" / "rustup" and Path(env["RUSTC"]) == b / f"rustc{EXE}"
    assert Path(env["CARGO_HOME"]).name == "cargo-home" and tools not in Path(env["CARGO_HOME"]).parents     # case-local cache, not the toolchain's
    assert "RUSTUP_TOOLCHAIN" not in env and "CARGO_BUILD_TARGET" not in env
    assert str(b).lower() in env["PATH"].lower()


def test_forecast_and_capabilities_name_the_tools_entry_when_no_compiler(tmp_path, monkeypatch):
    from rebuild_controller.implement import forecast, global_forecast
    monkeypatch.setattr(rust, "cargo", lambda: None)
    st = SimpleNamespace(settings=Settings(data_dir=tmp_path / "d", tools_dir=tmp_path / "t"), ai=None)
    f = forecast(st, target_language="rust", output_type="exe", ai_policy={"mode": "no_ai"}, launch_profile=None, profile="native")
    assert f["rust_toolchain"]["available"] is False and f["rust_toolchain"]["title"] == "Rust compiler (private)"
    assert any("Rust compiler (private)" in a for a in f["next_actions"]) and any("Tools" in d for d in f["details"])
    g = global_forecast(st)
    assert g["rust_toolchain"]["available"] is False and "Rust compiler (private)" in g["summary"]
    fake_private(tmp_path / "t")
    assert forecast(st, target_language="rust", output_type="exe", ai_policy={"mode": "no_ai"}, launch_profile=None, profile="native")["rust_toolchain"]["available"] is True
    assert "Rust compiler" not in global_forecast(st)["summary"]
