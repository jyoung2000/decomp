# Rebuild Studio for Gemini CLI

`settings.json` (`mcpServers.rebuild-studio`), `skills/rebuild-studio/` and a `GEMINI.md` snippet.

**The desktop app needs none of this.** No global skill is required to use Rebuild Studio from its UI.

## Install
```
python scripts/install-clients.py --client gemini --dry-run
python scripts/install-clients.py --client gemini [--mcp-command /path/to/rebuild-mcp] [--rules]
```
- Merges `mcpServers.rebuild-studio` into `~/.gemini/settings.json` (strict JSON only; if your file contains comments the installer
  refuses and prints the entry to add by hand). The server name has no underscore because Gemini CLI parses `mcp_<server>_<tool>`
  names on the first underscore.
- Copies the skill to `~/.gemini/skills/rebuild-studio/`; `~/.agents/skills` is also checked for a same-named foreign skill.
- `--rules` adds the short block from `GEMINI.md` to `~/.gemini/GEMINI.md`.
- Backup before modifying, idempotent, duplicate-skill refusal.

## Remove
```
python scripts/install-clients.py --client gemini --remove
```
