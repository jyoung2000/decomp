"""Tests for scripts/install-clients.py and the client packages under clients/."""
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "install-clients.py"
CLIENTS = REPO / "clients"
MCP = "/opt/rebuild/bin/rebuild-mcp"

_spec = importlib.util.spec_from_file_location("install_clients", SCRIPT)
inst = importlib.util.module_from_spec(_spec)
sys.modules["install_clients"] = inst
_spec.loader.exec_module(inst)

try:  # optional: used to double-check YAML results
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def run(home, *args, clients=CLIENTS):
    lines: list[str] = []
    code = inst.main([*args, "--home", str(home), "--clients-dir", str(clients), "--mcp-command", MCP], out=lines.append)
    return code, "\n".join(lines)


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def config_files(home: Path) -> dict[str, Path]:
    return {"claude-code": home / ".claude.json", "codex": home / ".codex" / "config.toml",
            "gemini": home / ".gemini" / "settings.json", "hermes": home / ".hermes" / "config.yaml"}


def skill_dirs(home: Path) -> dict[str, Path]:
    return {"claude-code": home / ".claude" / "skills" / "rebuild-studio", "codex": home / ".agents" / "skills" / "rebuild-studio",
            "gemini": home / ".gemini" / "skills" / "rebuild-studio", "hermes": home / ".hermes" / "skills" / "rebuild-studio"}


ALL = ["claude-code", "codex", "gemini", "hermes"]


# ------------------------------------------------------------------------------------------------ dry run
@pytest.mark.parametrize("client", ALL)
def test_dry_run_prints_diff_and_touches_nothing(home, client):
    code, out = run(home, "--client", client, "--dry-run")
    assert code == 0
    assert snapshot(home) == {}
    assert "would create" in out and "dry run: nothing was changed" in out
    assert "+++ " in out and "rebuild" in out and MCP in out
    assert "references/REFERENCE.md" in out and ".rebuild-studio-managed.json" in out


def test_dry_run_with_existing_config_shows_a_minimal_diff(home):
    cfg = config_files(home)["gemini"]
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"theme": "dark", "mcpServers": {"x": {"command": "y"}}}, indent=2) + "\n")
    before = snapshot(home)
    code, out = run(home, "--client", "gemini", "--dry-run")
    assert code == 0 and snapshot(home) == before
    assert '+    "rebuild-studio": {' in out
    removed = [ln for ln in out.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    assert all('"theme"' not in ln for ln in removed), "unrelated keys must not show up as changed"


# ------------------------------------------------------------------------------------------------ install / idempotency
@pytest.mark.parametrize("client", ALL)
def test_install_creates_config_and_skill_and_is_idempotent(home, client):
    code, out = run(home, "--client", client)
    assert code == 0, out
    cfg, skill = config_files(home)[client], skill_dirs(home)[client]
    assert cfg.is_file() and (skill / "SKILL.md").is_file() and (skill / inst.MARKER_FILE).is_file()
    assert (skill / "references" / "REFERENCE.md").read_bytes() == (CLIENTS / "common" / "REFERENCE.md").read_bytes()
    first = snapshot(home)
    code, out2 = run(home, "--client", client)
    assert code == 0
    assert snapshot(home) == first, "second install must not change anything (including no new backups)"
    actions = [ln.split(":")[0].strip() for ln in out2.splitlines() if ln.startswith("  ") and ":" in ln and not ln.startswith("   ")]
    assert "unchanged" in actions and not {"create", "update", "delete", "remove"} & set(actions)
    assert not list(home.rglob("*.bak-*"))


def test_install_entries_have_the_right_shape(home):
    for c in ALL:
        assert run(home, "--client", c, "--toolset", "analysis")[0] == 0
    cl = json.loads(config_files(home)["claude-code"].read_text())["mcpServers"]["rebuild-studio"]
    assert cl == {"type": "stdio", "command": MCP, "args": ["--toolset", "analysis"]}
    ge = json.loads(config_files(home)["gemini"].read_text())["mcpServers"]["rebuild-studio"]
    assert ge == {"command": MCP, "args": ["--toolset", "analysis"]}
    cx = tomllib.loads(config_files(home)["codex"].read_text())["mcp_servers"]["rebuild_studio"]
    assert cx["command"] == MCP and cx["args"] == ["--toolset", "analysis"]
    hm_text = config_files(home)["hermes"].read_text()
    assert "REBUILD_STUDIO_DATA" in hm_text, "Hermes filters the environment, so the data dir must be explicit"
    if yaml:
        hm = yaml.safe_load(hm_text)["mcp_servers"]["rebuild_studio"]
        assert hm["command"] == MCP and hm["args"] == ["--toolset", "analysis"] and hm["env"]["REBUILD_STUDIO_DATA"]


def test_data_dir_option_sets_env(home):
    code, _ = run(home, "--client", "claude-code", "--data-dir", "/srv/studio")
    assert code == 0
    e = json.loads(config_files(home)["claude-code"].read_text())["mcpServers"]["rebuild-studio"]
    assert e["env"] == {"REBUILD_STUDIO_DATA": "/srv/studio"}
    code, _ = run(home, "--client", "codex", "--data-dir", "C:\\Users\\me\\Studio Data")
    t = tomllib.loads(config_files(home)["codex"].read_text())
    assert t["mcp_servers"]["rebuild_studio"]["env"]["REBUILD_STUDIO_DATA"] == "C:\\Users\\me\\Studio Data"


def test_default_data_dir_mirror_matches_controller(monkeypatch, tmp_path):
    from rebuild_controller.config import default_data_dir
    for env in ({"REBUILD_STUDIO_DATA": str(tmp_path / "x")}, {"XDG_DATA_HOME": str(tmp_path / "xdg")}, {}):
        monkeypatch.delenv("REBUILD_STUDIO_DATA", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert inst.default_data_dir() == default_data_dir()


# ------------------------------------------------------------------------------------------------ merge preserves unrelated content
def test_json_merge_preserves_keys_indent_and_restores_on_remove(home):
    cfg = config_files(home)["claude-code"]
    original = {"numStartups": 7, "projects": {"/p": {"allowedTools": []}}, "mcpServers": {"other": {"command": "o", "args": ["1"]}}}
    text = json.dumps(original, indent=4) + "\n"
    cfg.write_text(text)
    assert run(home, "--client", "claude-code")[0] == 0
    merged = json.loads(cfg.read_text())
    assert merged["numStartups"] == 7 and merged["projects"] == original["projects"]
    assert merged["mcpServers"]["other"] == original["mcpServers"]["other"] and "rebuild-studio" in merged["mcpServers"]
    assert cfg.read_text().startswith('{\n    "numStartups"'), "indentation of the user's file is kept"
    backups = list(home.glob(".claude.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == text
    assert run(home, "--client", "claude-code", "--remove")[0] == 0
    assert cfg.read_text() == text
    assert not skill_dirs(home)["claude-code"].exists()


def test_json_remove_from_fresh_install_deletes_empty_config(home):
    run(home, "--client", "gemini")
    cfg = config_files(home)["gemini"]
    assert cfg.exists()
    assert run(home, "--client", "gemini", "--remove")[0] == 0
    assert not cfg.exists() and not skill_dirs(home)["gemini"].exists()


def test_json_with_comments_is_refused_with_instructions(home):
    cfg = config_files(home)["gemini"]
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{\n  // my theme\n  "theme": "dark"\n}\n')
    code, out = run(home, "--client", "gemini")
    assert code == 1 and "REFUSED" in out and "by hand" in out
    assert cfg.read_text().startswith("{\n  // my theme")
    assert not skill_dirs(home)["gemini"].exists(), "nothing may be written when a refusal happens"


def test_foreign_json_server_entry_is_refused_but_our_old_entry_is_updated(home):
    cfg = config_files(home)["claude-code"]
    cfg.write_text(json.dumps({"mcpServers": {"rebuild-studio": {"command": "/usr/bin/other-thing", "args": []}}}))
    code, out = run(home, "--client", "claude-code")
    assert code == 1 and "not created by Rebuild Studio" in out
    cfg.write_text(json.dumps({"mcpServers": {"rebuild-studio": {"type": "stdio", "command": "/old/path/rebuild-mcp", "args": ["--toolset", "all"]}}}))
    code, out = run(home, "--client", "claude-code")
    assert code == 0
    assert json.loads(cfg.read_text())["mcpServers"]["rebuild-studio"]["command"] == MCP


TOML_ORIGINAL = '''# my codex config
model = "gpt-5"   # keep me

[mcp_servers.other]
command = "npx"
args = ["-y", "thing"]

[profiles.fast]
model = "mini"
'''


def test_toml_merge_is_marked_valid_idempotent_and_removable(home):
    cfg = config_files(home)["codex"]
    cfg.parent.mkdir(parents=True)
    cfg.write_text(TOML_ORIGINAL)
    assert run(home, "--client", "codex")[0] == 0
    text = cfg.read_text()
    assert text.startswith(TOML_ORIGINAL) and inst.BEGIN in text and inst.END in text
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5" and parsed["mcp_servers"]["other"]["command"] == "npx" and parsed["profiles"]["fast"]["model"] == "mini"
    assert parsed["mcp_servers"]["rebuild_studio"]["command"] == MCP
    assert run(home, "--client", "codex")[0] == 0 and cfg.read_text() == text
    # re-install with another command replaces inside the marked block only
    lines = []
    assert inst.main(["--client", "codex", "--home", str(home), "--clients-dir", str(CLIENTS), "--mcp-command", "/new/rebuild-mcp"], out=lines.append) == 0
    t2 = tomllib.loads(cfg.read_text())
    assert t2["mcp_servers"]["rebuild_studio"]["command"] == "/new/rebuild-mcp" and cfg.read_text().count(inst.BEGIN) == 1
    assert run(home, "--client", "codex", "--remove")[0] == 0
    assert cfg.read_text() == TOML_ORIGINAL
    assert not skill_dirs(home)["codex"].exists()


def test_toml_foreign_table_is_refused(home):
    cfg = config_files(home)["codex"]
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.rebuild_studio]\ncommand = "mine"\n')
    code, out = run(home, "--client", "codex")
    assert code == 1 and "already exists" in out and cfg.read_text() == '[mcp_servers.rebuild_studio]\ncommand = "mine"\n'
    code, out = run(home, "--client", "codex", "--remove")
    assert code == 1 and "not created by this installer" in out


def test_toml_that_does_not_parse_or_cannot_be_extended_is_refused(home):
    cfg = config_files(home)["codex"]
    cfg.parent.mkdir(parents=True)
    cfg.write_text("this is = = not toml\n")
    assert run(home, "--client", "codex")[0] == 1
    cfg.write_text('mcp_servers = { other = { command = "x" } }\n')  # inline table cannot be extended
    code, out = run(home, "--client", "codex")
    assert code == 1 and cfg.read_text() == 'mcp_servers = { other = { command = "x" } }\n'


def test_crlf_and_bom_are_preserved_and_restored(home):
    cfg = config_files(home)["codex"]
    cfg.parent.mkdir(parents=True)
    raw = b"\xef\xbb\xbf" + TOML_ORIGINAL.replace("\n", "\r\n").encode()
    cfg.write_bytes(raw)
    assert run(home, "--client", "codex")[0] == 0
    new = cfg.read_bytes()
    assert new.startswith(b"\xef\xbb\xbf") and b"\r\n" in new and b"\n" not in new.replace(b"\r\n", b"")
    assert run(home, "--client", "codex", "--remove")[0] == 0
    assert cfg.read_bytes() == raw


YAML_ORIGINAL = '''# hermes config
model:
  default: some/model
terminal:
  cwd: .   # keep me
skills:
  creation_nudge_interval: 15
'''

YAML_WITH_SERVERS = '''model:
  default: some/model
mcp_servers:
  # my servers
  time:
    command: "uvx"
    args: ["mcp-server-time"]
  fs:
    command: "npx"
    args: ["-y", "x", "/home/me"]
agent:
  max_turns: 50
'''


def _hermes_cfg(home, text):
    p = config_files(home)["hermes"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_yaml_merge_without_existing_mcp_servers(home):
    cfg = _hermes_cfg(home, YAML_ORIGINAL)
    assert run(home, "--client", "hermes")[0] == 0
    text = cfg.read_text()
    assert text.startswith(YAML_ORIGINAL) and "mcp_servers:\n  # >>> rebuild-studio (managed) >>>" in text
    if yaml:
        d = yaml.safe_load(text)
        assert d["model"] == {"default": "some/model"} and d["terminal"] == {"cwd": "."} and d["skills"]["creation_nudge_interval"] == 15
        assert d["mcp_servers"]["rebuild_studio"]["command"] == MCP
    assert run(home, "--client", "hermes")[0] == 0 and cfg.read_text() == text
    assert run(home, "--client", "hermes", "--remove")[0] == 0
    assert cfg.read_text() == YAML_ORIGINAL


def test_yaml_merge_into_existing_mcp_servers_keeps_siblings_and_comments(home):
    cfg = _hermes_cfg(home, YAML_WITH_SERVERS)
    assert run(home, "--client", "hermes")[0] == 0
    text = cfg.read_text()
    assert "  # my servers" in text and "agent:\n  max_turns: 50" in text
    if yaml:
        d = yaml.safe_load(text)
        assert set(d["mcp_servers"]) == {"time", "fs", "rebuild_studio"} and d["agent"] == {"max_turns": 50}
        assert d["mcp_servers"]["fs"]["args"] == ["-y", "x", "/home/me"]
    assert run(home, "--client", "hermes")[0] == 0 and cfg.read_text() == text
    assert run(home, "--client", "hermes", "--remove")[0] == 0
    assert cfg.read_text() == YAML_WITH_SERVERS


def test_yaml_empty_flow_mapping_is_converted_and_foreign_entries_refused(home):
    cfg = _hermes_cfg(home, "model: x\nmcp_servers: {}\n")
    assert run(home, "--client", "hermes")[0] == 0
    if yaml:
        assert yaml.safe_load(cfg.read_text())["mcp_servers"]["rebuild_studio"]["command"] == MCP
    assert run(home, "--client", "hermes", "--remove")[0] == 0
    assert cfg.read_text() == "model: x\n"
    cfg.write_text("mcp_servers:\n  rebuild_studio:\n    command: mine\n")
    code, out = run(home, "--client", "hermes")
    assert code == 1 and "already exists" in out and cfg.read_text() == "mcp_servers:\n  rebuild_studio:\n    command: mine\n"
    cfg.write_text("mcp_servers: {a: {command: x}}\n")
    code, out = run(home, "--client", "hermes")
    assert code == 1 and "inline" in out


def test_rules_block_added_and_removed_for_codex_and_gemini(home):
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# Mine\n\nBe terse.\n")
    gem = home / ".gemini" / "GEMINI.md"
    assert run(home, "--client", "codex", "--rules")[0] == 0
    assert run(home, "--client", "gemini", "--rules")[0] == 0
    t = agents.read_text()
    assert t.startswith("# Mine\n\nBe terse.\n") and inst.MD_BEGIN in t and "verification verdicts" in t
    assert "rebuild-studio" in gem.read_text()
    assert run(home, "--client", "codex", "--rules")[0] == 0 and agents.read_text() == t
    assert run(home, "--client", "codex", "--remove")[0] == 0
    assert agents.read_text() == "# Mine\n\nBe terse.\n"
    assert run(home, "--client", "gemini", "--remove")[0] == 0
    assert not gem.exists()


def test_rules_are_not_installed_without_the_flag(home):
    run(home, "--client", "codex")
    run(home, "--client", "gemini")
    assert not (home / ".codex" / "AGENTS.md").exists() and not (home / ".gemini" / "GEMINI.md").exists()


# ------------------------------------------------------------------------------------------------ duplicate skills / safety
@pytest.mark.parametrize("client,foreign", [
    ("claude-code", ".claude/skills/rebuild-studio"),
    ("claude-code", ".claude/skills/some-other-dir"),          # same frontmatter name, different folder
    ("codex", ".agents/skills/rebuild-studio"),
    ("codex", ".codex/skills/rebuild-studio"),                 # deprecated user root is checked too
    ("gemini", ".gemini/skills/rebuild-studio"),
    ("gemini", ".agents/skills/mine"),
    ("hermes", ".hermes/skills/rebuild-studio"),
    ("hermes", ".hermes/skills/devops/rebuild-studio"),        # Hermes category folder
])
def test_duplicate_skill_name_from_another_source_is_refused(home, client, foreign):
    d = home / foreign
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: rebuild-studio\ndescription: somebody else's\n---\nbody\n")
    before = snapshot(home)
    code, out = run(home, "--client", client)
    assert code == 1 and "REFUSED" in out and "another source" in out and str(d) in out
    assert snapshot(home) == before, "a refusal must leave every file untouched"


def test_refusal_for_one_client_stops_all_clients(home):
    for c in (".claude", ".codex", ".gemini", ".hermes"):
        (home / c).mkdir()
    d = home / ".hermes" / "skills" / "rebuild-studio"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: rebuild-studio\ndescription: x\n---\nx\n")
    code, out = run(home, "--client", "all")
    assert code == 1
    assert not config_files(home)["claude-code"].exists() and not config_files(home)["codex"].exists()


def test_same_name_different_skill_names_are_fine(home):
    d = home / ".claude" / "skills" / "other-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: other-skill\ndescription: x\n---\nx\n")
    assert run(home, "--client", "claude-code")[0] == 0
    assert run(home, "--client", "claude-code", "--remove")[0] == 0
    assert (d / "SKILL.md").exists(), "unrelated skills survive install and remove"


def test_remove_never_deletes_a_foreign_skill_directory(home):
    d = home / ".claude" / "skills" / "rebuild-studio"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: rebuild-studio\ndescription: mine\n---\nbody\n")
    code, out = run(home, "--client", "claude-code", "--remove")
    assert code == 1 and "not installed by this installer" in out and (d / "SKILL.md").exists()


def test_remove_with_nothing_installed_is_a_noop(home):
    for c in ALL:
        code, out = run(home, "--client", c, "--remove")
        assert code == 0
    assert snapshot(home) == {}


def test_managed_skill_is_updated_when_package_changes_and_foreign_files_are_replaced_only_in_managed_dir(home, tmp_path):
    import shutil
    clients = tmp_path / "clients"
    shutil.copytree(CLIENTS, clients)
    assert run(home, "--client", "claude-code", clients=clients)[0] == 0
    skill = clients / "claude-code" / "skills" / "rebuild-studio" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nExtra line.\n")
    code, out = run(home, "--client", "claude-code", "--dry-run", clients=clients)
    assert code == 0 and "update" in out and "SKILL.md" in out
    assert run(home, "--client", "claude-code", clients=clients)[0] == 0
    assert "Extra line." in (skill_dirs(home)["claude-code"] / "SKILL.md").read_text()


def test_all_installs_only_detected_clients(home):
    (home / ".codex").mkdir()
    code, out = run(home, "--client", "all")
    assert code == 0
    assert config_files(home)["codex"].exists()
    assert not config_files(home)["claude-code"].exists() and not config_files(home)["hermes"].exists()
    assert out.count("skipped: not detected") == 3


def test_claude_config_dir_env_is_honoured_when_home_is_not_forced(tmp_path, monkeypatch):
    # Direct unit check of the path logic (the --home flag deliberately ignores *_HOME overrides).
    env = inst.Env(home=tmp_path / "h", explicit_home=False, clients_dir=CLIENTS, rules=False,
                   spec_args={"mcp_command": MCP, "toolset": "all", "data_dir": None})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    assert inst.plan_client("claude-code", env, False).files[0].path == tmp_path / "cc" / ".claude.json"
    assert inst.plan_client("claude-code", env, False).dirs[0].path == tmp_path / "cc" / "skills" / "rebuild-studio"
    assert inst.plan_client("codex", env, False).files[0].path == tmp_path / "cx" / "config.toml"
    assert inst.plan_client("hermes", env, False).files[0].path == tmp_path / "hh" / "config.yaml"


# ------------------------------------------------------------------------------------------------ packages
def _frontmatter(path: Path) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert m, f"{path} has no frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        if re.match(r"^[a-z]+:", line):
            k, _, v = line.partition(":")
            out[k] = v.strip()
    return out


@pytest.mark.parametrize("client", ALL)
def test_each_package_has_skill_readme_and_synced_reference(client):
    skill = CLIENTS / client / "skills" / "rebuild-studio"
    fm = _frontmatter(skill / "SKILL.md")
    assert fm["name"] == "rebuild-studio" and fm["description"]
    body = (skill / "SKILL.md").read_text()
    assert "rebuild" in body and "diagnose" in body
    for rule in ("evidence", "provenance", "verdict", "untrusted"):
        assert rule in body.lower()
    assert len(body.splitlines()) < 90, "rules must stay short; details belong in REFERENCE.md"
    assert (skill / "references" / "REFERENCE.md").read_bytes() == (CLIENTS / "common" / "REFERENCE.md").read_bytes(), \
        "copy clients/common/REFERENCE.md into every package"
    readme = (CLIENTS / client / "README.md").read_text()
    assert "desktop app" in readme and "No global skill is required" in readme and "--remove" in readme and "install-clients.py" in readme


def test_hermes_skill_follows_hermes_authoring_limits():
    fm = _frontmatter(CLIENTS / "hermes" / "skills" / "rebuild-studio" / "SKILL.md")
    assert len(fm["description"]) <= 60 and fm["description"].endswith(".")
    body = (CLIENTS / "hermes" / "skills" / "rebuild-studio" / "SKILL.md").read_text()
    for section in ("## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification"):
        assert section in body


def test_claude_code_plugin_manifest_and_mcp_json():
    pj = json.loads((CLIENTS / "claude-code" / ".claude-plugin" / "plugin.json").read_text())
    assert pj["name"] == "rebuild-studio" and re.fullmatch(r"\d+\.\d+\.\d+", pj["version"]) and pj["description"]
    mcp = json.loads((CLIENTS / "claude-code" / ".mcp.json").read_text())
    assert mcp["mcpServers"]["rebuild-studio"]["command"] == "rebuild-mcp"
    assert not (CLIENTS / "claude-code" / ".claude-plugin" / "skills").exists(), "components live at the plugin root, not in .claude-plugin/"


def test_client_config_snippets_parse():
    t = tomllib.loads((CLIENTS / "codex" / "config.toml").read_text())
    assert t["mcp_servers"]["rebuild_studio"]["command"] == "rebuild-mcp"
    g = json.loads((CLIENTS / "gemini" / "settings.json").read_text())
    assert "rebuild-studio" in g["mcpServers"] and not any("_" in k for k in g["mcpServers"]), "Gemini server names must not contain underscores"
    if yaml:
        h = yaml.safe_load((CLIENTS / "hermes" / "mcp_servers.yaml").read_text())
        assert h["mcp_servers"]["rebuild_studio"]["command"] == "rebuild-mcp" and "REBUILD_STUDIO_DATA" in h["mcp_servers"]["rebuild_studio"]["env"]


def test_rules_snippets_are_short_and_say_the_important_things():
    for p in (CLIENTS / "codex" / "AGENTS.md", CLIENTS / "gemini" / "GEMINI.md"):
        t = p.read_text()
        assert len(t.splitlines()) <= 12
        for word in ("evidence", "provenance", "verdict", "untrusted", "rebuild", "diagnose"):
            assert word in t


def test_reference_documents_every_server_tool():
    from rebuild_controller.mcp.server import TOOLSETS
    ref = (CLIENTS / "common" / "REFERENCE.md").read_text()
    for tools in TOOLSETS.values():
        for name in tools:
            assert name in ref, f"{name} missing from REFERENCE.md"


def test_windows_wrappers_call_the_python_installer():
    ins = (REPO / "scripts" / "windows" / "Install-Clients.ps1").read_text()
    rem = (REPO / "scripts" / "windows" / "Remove-Clients.ps1").read_text()
    assert "install-clients.py" in ins and "--mcp-command" in ins and "--dry-run" in ins and "RuntimeDir" in ins and "rebuild-mcp.exe" in ins
    assert "Install-Clients.ps1" in rem and "Remove = $true" in rem and "DryRun" in rem
    assert "$ErrorActionPreference = 'Stop'" in ins and "exit $LASTEXITCODE" in ins and "exit $LASTEXITCODE" in rem
