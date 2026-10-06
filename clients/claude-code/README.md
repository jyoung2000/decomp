# Rebuild Studio for Claude Code

A skill (`rebuild-studio`, actions `rebuild` and `diagnose`) plus the `rebuild-studio` MCP server entry.

**The desktop app needs none of this.** No global skill is required to use Rebuild Studio from its UI. These packages only let
Claude Code drive the same typed case API through MCP. Nothing here is installed unless you run one of the commands below.

## Install (user scope)
```
python scripts/install-clients.py --client claude-code --dry-run      # shows the diff
python scripts/install-clients.py --client claude-code [--mcp-command /path/to/rebuild-mcp] [--toolset analysis]
```
Adds `mcpServers.rebuild-studio` to `~/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json`) and the skill to
`~/.claude/skills/rebuild-studio/`. Existing keys are kept, a backup (`.bak-<timestamp>`) is made first, running it twice
changes nothing, and it refuses if a different skill named `rebuild-studio` already exists. Restart Claude Code afterwards.

## Remove
```
python scripts/install-clients.py --client claude-code --remove
```

## Alternative: load as a plugin without touching user config
```
claude --plugin-dir clients/claude-code
```
The plugin (`.claude-plugin/plugin.json`, `.mcp.json`, `skills/`) expects `rebuild-mcp` on `PATH`; edit `.mcp.json` otherwise.
Validate with `claude plugin validate clients/claude-code`.

Backend details live in `clients/common/REFERENCE.md` and are copied to `skills/rebuild-studio/references/` (loaded on demand).
