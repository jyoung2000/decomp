# Rebuild Studio for Codex

`config.toml` (`[mcp_servers.rebuild_studio]`), `skills/rebuild-studio/` and an `AGENTS.md` snippet.

**The desktop app needs none of this.** No global skill is required to use Rebuild Studio from its UI.

## Install
```
python scripts/install-clients.py --client codex --dry-run
python scripts/install-clients.py --client codex [--mcp-command /path/to/rebuild-mcp] [--rules]
```
- Merges a marked block `[mcp_servers.rebuild_studio]` into `~/.codex/config.toml` (`$CODEX_HOME`); other tables are untouched.
- Copies the skill to `~/.agents/skills/rebuild-studio/` (Codex's user skill root; verified in openai/codex
  `codex-rs/ext/skills/src/host_roots.rs`; `$CODEX_HOME/skills` is the deprecated location and is checked for duplicates).
- `--rules` also adds the short block from `AGENTS.md` to `~/.codex/AGENTS.md`. Without it the rules live only in the skill.
- Backs up before modifying, is idempotent, and refuses to duplicate a skill with the same name from another source.

## Remove
```
python scripts/install-clients.py --client codex --remove
```
Manual alternative: paste `config.toml` into your config and copy `skills/rebuild-studio` into `~/.agents/skills/`.
