# Rebuild Studio for Hermes

A Hermes skill (`skills/rebuild-studio/SKILL.md`) plus the `mcp_servers` snippet for `config.yaml`.

**The desktop app needs none of this.** No global skill is required to use Rebuild Studio from its UI (the app's own Hermes bridge
handles pairing separately).

## Install (user level)
```
python scripts/install-clients.py --client hermes --dry-run
python scripts/install-clients.py --client hermes [--mcp-command C:\path\rebuild-mcp.exe] [--data-dir DIR]
```
- Adds `mcp_servers.rebuild_studio` to `$HERMES_HOME/config.yaml` (default `~/.hermes/config.yaml`; Windows
  `%LOCALAPPDATA%\hermes\config.yaml`) inside a marked block; other keys and comments are untouched. Hermes starts MCP servers with a
  filtered environment, so the installer always writes `env.REBUILD_STUDIO_DATA`. Tools appear as `mcp_rebuild_studio_<tool>`.
- Copies the skill to `$HERMES_HOME/skills/rebuild-studio/`.
- Backup before modifying, idempotent, refuses to duplicate a same-named skill.

## Project-level skill instead
Copy `skills/rebuild-studio` to `<project>/.hermes/skills/` (or `.agents/skills/`) and run `hermes skills trust <project>`; Hermes loads
project skills only from trusted roots. Still add the MCP block from `mcp_servers.yaml` to your config.

## Remove
```
python scripts/install-clients.py --client hermes --remove
```
Format sources (hermes-agent): `skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md`, `skills/AGENTS.md`,
`agent/skill_utils.py`, `hermes_cli/mcp_security.py`, `hermes_constants.py`.
