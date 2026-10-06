#!/usr/bin/env python3
"""Install or remove the Rebuild Studio client packages (MCP server entry + skill [+ short rules]) for
Claude Code, Codex, Gemini CLI and Hermes. Standard library only; works on Linux, macOS and Windows.

Design rules
  * Idempotent: running twice changes nothing the second time.
  * Merge, never overwrite: unrelated keys/servers/sections in the user's config are preserved; JSON is parsed and
    rewritten, TOML and YAML are edited as text inside a clearly marked managed block (no TOML/YAML writer dependency).
  * Everything is backed up (`<file>.bak-<timestamp>`) before it is modified.
  * `--dry-run` prints a unified diff / file list and touches nothing.
  * A skill named `rebuild-studio` that came from another source is never overwritten or duplicated: the run refuses.
  * `--remove` takes out exactly what was added and leaves the rest of every file as it was.
  * The desktop app needs no global skill; installing these packages is optional and only for CLI agents.

Usage:
  install-clients.py --client claude-code|codex|gemini|hermes|all [--dry-run] [--remove] [--mcp-command PATH]
                     [--data-dir DIR] [--toolset all] [--rules] [--home DIR] [--clients-dir DIR]
Exit codes: 0 ok, 1 refused (conflict / unparsable config), 2 usage.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:  # Python >= 3.11
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

SKILL_NAME = "rebuild-studio"
MARKER_FILE = ".rebuild-studio-managed.json"
BEGIN = "# >>> rebuild-studio (managed) >>>"
END = "# <<< rebuild-studio (managed) <<<"
MD_BEGIN = "<!-- >>> rebuild-studio (managed) >>> -->"
MD_END = "<!-- <<< rebuild-studio (managed) <<< -->"
CLIENTS = ("claude-code", "codex", "gemini", "hermes")
SERVER_KEY = {"claude-code": "rebuild-studio", "codex": "rebuild_studio", "gemini": "rebuild-studio", "hermes": "rebuild_studio"}


class Refused(Exception):
    """The requested change would clobber something that is not ours or cannot be edited safely."""


# ------------------------------------------------------------------------------------------------ text helpers
def _read_text(path: Path) -> tuple[str, str, bool]:
    """(text, newline, had_bom); text uses '\\n' internally."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), nl, bom


def _encode(text: str, nl: str, bom: bool) -> bytes:
    data = text.replace("\n", nl).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def _q(s: str) -> str:
    """Quoted string valid as a JSON, TOML basic and YAML double-quoted scalar."""
    return json.dumps(s, ensure_ascii=False)


def _unified(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{path} (current)", tofile=f"{path} (after)")) or ""


# ------------------------------------------------------------------------------------------------ plan objects
@dataclass
class FileChange:
    path: Path
    old: Optional[str]            # None = does not exist
    new: Optional[str]            # None = delete
    nl: str = "\n"
    bom: bool = False
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.old != self.new


@dataclass
class DirChange:
    path: Path
    files: dict[str, bytes]       # relative posix path -> content (empty dict + remove=True: delete dir)
    remove: bool = False
    status: str = ""              # created|updated|unchanged|removed|absent
    detail: list[str] = field(default_factory=list)


@dataclass
class Plan:
    client: str
    files: list[FileChange] = field(default_factory=list)
    dirs: list[DirChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------------------ server entry
@dataclass
class ServerSpec:
    command: str
    args: list[str]
    env: dict[str, str]


def resolve_server_spec(mcp_command: Optional[str], toolset: str, data_dir: Optional[str], hermes: bool) -> ServerSpec:
    if mcp_command:
        command, base_args = mcp_command, []
    else:
        found = shutil.which("rebuild-mcp")
        if found:
            command, base_args = found, []
        else:
            command, base_args = sys.executable, ["-m", "rebuild_controller.mcp.server"]
    env: dict[str, str] = {}
    if data_dir:
        env["REBUILD_STUDIO_DATA"] = str(data_dir)
    elif hermes:
        # Hermes starts MCP servers with a filtered environment (PATH/HOME/USER/XDG_* only), so name the data dir explicitly.
        env["REBUILD_STUDIO_DATA"] = str(default_data_dir())
    return ServerSpec(command, base_args + ["--toolset", toolset], env)


def default_data_dir() -> Path:
    """Mirror of rebuild_controller.config.default_data_dir (kept in sync by a test)."""
    e = os.environ.get("REBUILD_STUDIO_DATA")
    if e:
        return Path(e)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RebuildStudio"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "rebuild-studio"


# ------------------------------------------------------------------------------------------------ JSON merge
def _is_ours_json(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    blob = " ".join([str(entry.get("command", ""))] + [str(a) for a in entry.get("args", []) if isinstance(entry.get("args"), list)])
    return "rebuild-mcp" in blob or "rebuild_controller.mcp.server" in blob


def merge_json(text: Optional[str], key: str, entry: dict[str, Any], container: str = "mcpServers") -> str:
    """Insert/replace our server entry, preserving every other key and the file's indentation."""
    if text is None or not text.strip():
        data: dict[str, Any] = {}
        indent: Any = 2
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise Refused(f"config is not strict JSON ({exc}); comments are not supported. Add this entry by hand under "
                          f"\"{container}\": {json.dumps({key: entry})}")
        if not isinstance(data, dict):
            raise Refused("config root is not a JSON object")
        indent = _detect_indent(text)
    servers = data.setdefault(container, {})
    if not isinstance(servers, dict):
        raise Refused(f"\"{container}\" is not an object")
    if key in servers and servers[key] != entry and not _is_ours_json(servers[key]):
        raise Refused(f"\"{container}.{key}\" already exists and was not created by Rebuild Studio; refusing to replace it")
    servers[key] = entry
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


def remove_json(text: Optional[str], key: str, container: str = "mcpServers") -> Optional[str]:
    """Remove our entry. Returns None when the file ends up empty (caller deletes it)."""
    if text is None or not text.strip():
        return text
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refused(f"config is not strict JSON ({exc}); remove \"{container}.{key}\" by hand")
    servers = data.get(container) if isinstance(data, dict) else None
    if not isinstance(servers, dict) or key not in servers:
        return text
    if not _is_ours_json(servers[key]):
        raise Refused(f"\"{container}.{key}\" was not created by Rebuild Studio; leaving it alone")
    del servers[key]
    if not servers:
        del data[container]
    if not data:
        return None
    return json.dumps(data, indent=_detect_indent(text), ensure_ascii=False) + "\n"


def _detect_indent(text: str) -> Any:
    m = re.search(r"^([ \t]+)\S", text, re.M)
    if not m:
        return 2
    ws = m.group(1)
    return "\t" if ws.startswith("\t") else len(ws)


def json_entry(spec: ServerSpec, claude: bool) -> dict[str, Any]:
    e: dict[str, Any] = {}
    if claude:
        e["type"] = "stdio"
    e["command"] = spec.command
    e["args"] = list(spec.args)
    if spec.env:
        e["env"] = dict(spec.env)
    return e


# ------------------------------------------------------------------------------------------------ TOML merge (Codex)
def toml_block(spec: ServerSpec) -> str:
    lines = [BEGIN, "[mcp_servers.rebuild_studio]", f"command = {_q(spec.command)}",
             "args = [" + ", ".join(_q(a) for a in spec.args) + "]", "startup_timeout_sec = 30"]
    if spec.env:
        lines += ["", "[mcp_servers.rebuild_studio.env]"] + [f"{k} = {_q(v)}" for k, v in spec.env.items()]
    lines.append(END)
    return "\n".join(lines) + "\n"


def _managed_span(text: str, begin: str, end: str) -> Optional[tuple[int, int]]:
    """Character span [start, end) covering the managed block including its trailing newline."""
    b = text.find(begin)
    if b < 0:
        return None
    e = text.find(end, b)
    if e < 0:
        raise Refused("found the start of a managed block without its end marker; fix the file by hand")
    e += len(end)
    if e < len(text) and text[e] == "\n":
        e += 1
    # also swallow the line indentation before the begin marker
    ls = text.rfind("\n", 0, b) + 1
    if text[ls:b].strip() == "":
        b = ls
    return b, e


def merge_toml(text: Optional[str], spec: ServerSpec) -> str:
    text = text or ""
    block = toml_block(spec)
    if tomllib is not None and text.strip():
        try:
            parsed = tomllib.loads(text)
        except Exception as exc:
            raise Refused(f"config.toml does not parse ({exc}); fix it first")
    else:
        parsed = {}
    span = _managed_span(text, BEGIN, END)
    if span:
        new = text[:span[0]] + block + text[span[1]:]
    else:
        if "rebuild_studio" in (parsed.get("mcp_servers") or {}):
            raise Refused("[mcp_servers.rebuild_studio] already exists and was not created by this installer; refusing to replace it")
        if text and not text.endswith("\n"):
            text += "\n"
        new = text + ("\n" if text.strip() else "") + block
    if tomllib is not None:
        try:
            got = tomllib.loads(new)["mcp_servers"]["rebuild_studio"]
        except Exception as exc:
            raise Refused(f"merged config.toml would not be valid TOML ({exc}); add this block by hand:\n{block}")
        if got.get("command") != spec.command or got.get("args") != spec.args:
            raise Refused("merged config.toml does not contain the expected server entry")
    return new


def remove_toml(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    span = _managed_span(text, BEGIN, END)
    if not span:
        if tomllib is not None and text.strip():
            try:
                present = "rebuild_studio" in (tomllib.loads(text).get("mcp_servers") or {})
            except Exception:
                present = False
            if present:
                raise Refused("[mcp_servers.rebuild_studio] was not created by this installer; leaving it alone")
        return text
    return _cut_block(text, span)


def _cut_block(text: str, span: tuple[int, int]) -> Optional[str]:
    """Remove the span; also remove the single separator blank line we add when appending at end of file."""
    head, tail = text[:span[0]], text[span[1]:]
    if tail == "" and head.endswith("\n\n"):
        head = head[:-1]
    new = head + tail
    return None if not new.strip() else new


# ------------------------------------------------------------------------------------------------ YAML merge (Hermes)
def yaml_child_block(spec: ServerSpec, indent: int) -> str:
    p = " " * indent
    lines = [f"{p}{BEGIN}", f"{p}rebuild_studio:", f"{p}  command: {_q(spec.command)}",
             f"{p}  args: [" + ", ".join(_q(a) for a in spec.args) + "]", f"{p}  timeout: 120", f"{p}  connect_timeout: 60"]
    if spec.env:
        lines.append(f"{p}  env:")
        lines += [f"{p}    {k}: {_q(v)}" for k, v in spec.env.items()]
    lines.append(f"{p}{END}")
    return "\n".join(lines) + "\n"


_TOP_KEY = re.compile(r"^mcp_servers[ \t]*:(?P<rest>.*)$")


def _find_mcp_servers(lines: list[str]) -> Optional[int]:
    for i, ln in enumerate(lines):
        if _TOP_KEY.match(ln):
            return i
    return None


def _block_end(lines: list[str], start: int) -> int:
    """Index after the last line belonging to the top-level key at `start` (indented / blank / comment lines)."""
    j = start + 1
    last = start
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "":
            j += 1
            continue
        if ln[0] in " \t":
            last = j
            j += 1
            continue
        if ln.lstrip().startswith("#") and ln[0] == "#":
            # a column-0 comment may belong to either; stop here (conservative)
            break
        break
    return last + 1


def merge_yaml(text: Optional[str], spec: ServerSpec) -> str:
    text = text or ""
    span = _managed_span(text, BEGIN, END)
    if span:
        b = text.rfind("\n", 0, text.find(BEGIN)) + 1
        indent = len(text[b:text.find(BEGIN)])
        new = text[:span[0]] + yaml_child_block(spec, indent) + text[span[1]:]
        return _check_yaml(new, spec)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    idx = _find_mcp_servers(lines)
    if idx is None:
        new_lines = lines + (["" ] if lines and lines[-1].strip() else []) + ["mcp_servers:"] + yaml_child_block(spec, 2).rstrip("\n").split("\n")
        return _check_yaml("\n".join(new_lines) + "\n", spec)
    rest = _TOP_KEY.match(lines[idx]).group("rest").split("#")[0].strip()  # type: ignore[union-attr]
    end = _block_end(lines, idx)
    body = lines[idx + 1:end]
    if re.search(r"^\s+rebuild_studio\s*:", "\n".join(body), re.M):
        raise Refused("mcp_servers.rebuild_studio already exists and was not created by this installer; refusing to replace it")
    if rest in ("{}", "null", "~", ""):
        pass
    else:
        raise Refused("mcp_servers is written as an inline/flow value; convert it to block style or add this by hand:\n"
                      + yaml_child_block(spec, 2))
    child_indent = 2
    for ln in body:
        if ln.strip() and not ln.lstrip().startswith("#"):
            child_indent = len(ln) - len(ln.lstrip(" "))
            break
    head = lines[idx] if rest == "" else "mcp_servers:"
    new_lines = lines[:idx] + [head] + yaml_child_block(spec, child_indent).rstrip("\n").split("\n") + body + lines[end:]
    return _check_yaml("\n".join(new_lines) + "\n", spec)


def _check_yaml(new: str, spec: ServerSpec) -> str:
    try:
        import yaml  # optional: only used to double-check the result
    except ImportError:
        return new
    try:
        data = yaml.safe_load(new)
        got = data["mcp_servers"]["rebuild_studio"]
    except Exception as exc:
        raise Refused(f"merged config.yaml would not be valid / lacks the entry ({exc}); add the block by hand:\n"
                      + yaml_child_block(spec, 2))
    if got.get("command") != spec.command or got.get("args") != spec.args:
        raise Refused("merged config.yaml does not contain the expected server entry")
    return new


def remove_yaml(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    span = _managed_span(text, BEGIN, END)
    if not span:
        if re.search(r"^[ \t]+rebuild_studio[ \t]*:", text, re.M) and _find_mcp_servers(text.split("\n")) is not None:
            raise Refused("mcp_servers.rebuild_studio was not created by this installer; leaving it alone")
        return text
    new = text[:span[0]] + text[span[1]:]
    lines = new.split("\n")
    if new.endswith("\n"):
        lines.pop()
    idx = _find_mcp_servers(lines)
    if idx is not None:
        rest = _TOP_KEY.match(lines[idx]).group("rest").split("#")[0].strip()  # type: ignore[union-attr]
        end = _block_end(lines, idx)
        children = [l for l in lines[idx + 1:end] if l.strip() and not l.lstrip().startswith("#")]
        if rest == "" and not children:
            del lines[idx:end]
            if idx == len(lines) and lines and lines[-1].strip() == "":
                lines.pop()  # the separator blank line added when we appended the key
    new = "\n".join(lines) + ("\n" if lines else "")
    return None if not new.strip() else new


# ------------------------------------------------------------------------------------------------ markdown rules block
def merge_markdown(text: Optional[str], snippet: str) -> str:
    block = f"{MD_BEGIN}\n{snippet.strip()}\n{MD_END}\n"
    text = text or ""
    span = _managed_span(text, MD_BEGIN, MD_END)
    if span:
        return text[:span[0]] + block + text[span[1]:]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text.strip() else "") + block


def remove_markdown(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    span = _managed_span(text, MD_BEGIN, MD_END)
    return text if not span else _cut_block(text, span)


# ------------------------------------------------------------------------------------------------ skills
def _frontmatter_name(skill_md: Path) -> Optional[str]:
    try:
        head = skill_md.read_text("utf-8", errors="replace")[:4096]
    except OSError:
        return None
    m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
    if not m:
        return None
    n = re.search(r"^name\s*:\s*(.+?)\s*$", m.group(1), re.M)
    return n.group(1).strip("'\"") if n else None


def find_foreign_skills(roots: list[Path], our_dir: Path) -> list[Path]:
    """Directories (other than ours) that define a skill called `rebuild-studio` and are not managed by this installer."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        cands: list[Path] = []
        for child in sorted(root.iterdir()):
            if child.is_dir():
                cands.append(child)
                try:  # one nested level (Hermes categories)
                    cands += [c for c in sorted(child.iterdir()) if c.is_dir()]
                except OSError:
                    pass
        for d in cands:
            if (d / "SKILL.md").is_file() and _frontmatter_name(d / "SKILL.md") == SKILL_NAME:
                if d.resolve() == our_dir.resolve():
                    continue
                if (d / MARKER_FILE).is_file():
                    continue
                found.append(d)
    if our_dir.is_dir() and not (our_dir / MARKER_FILE).is_file() and our_dir not in found:
        found.append(our_dir)
    return found


def collect_skill_files(src: Path, common_reference: Optional[Path], client: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for p in sorted(src.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            files[p.relative_to(src).as_posix()] = p.read_bytes()
    if common_reference and common_reference.is_file():
        files["references/REFERENCE.md"] = common_reference.read_bytes()
    digest = hashlib.sha256(b"".join(k.encode() + b"\0" + v for k, v in sorted(files.items()))).hexdigest()
    files[MARKER_FILE] = (json.dumps({"managed_by": "rebuild-studio installer", "client": client, "sha256": digest}, indent=1) + "\n").encode()
    return files


def plan_skill(dest: Path, files: dict[str, bytes], extra_roots: list[Path], remove: bool) -> DirChange:
    if remove:
        if not dest.exists():
            return DirChange(dest, {}, remove=True, status="absent")
        if not (dest / MARKER_FILE).is_file():
            raise Refused(f"{dest} was not installed by this installer; leaving it alone")
        return DirChange(dest, {}, remove=True, status="removed")
    foreign = find_foreign_skills(extra_roots + [dest.parent], dest)
    if foreign:
        raise Refused("a skill named 'rebuild-studio' already exists from another source: "
                      + ", ".join(str(f) for f in foreign) + ". Remove or rename it first; refusing to create a duplicate.")
    if not dest.exists():
        return DirChange(dest, files, status="created", detail=sorted(files))
    existing = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    if existing == files:
        return DirChange(dest, files, status="unchanged")
    detail = [f for f in sorted(files) if existing.get(f) != files[f]] + [f"(delete) {f}" for f in sorted(set(existing) - set(files))]
    return DirChange(dest, files, status="updated", detail=detail)


# ------------------------------------------------------------------------------------------------ per-client planning
@dataclass
class Env:
    home: Path
    explicit_home: bool
    clients_dir: Path
    spec_args: dict[str, Any]
    rules: bool

    def env(self, name: str) -> Optional[str]:
        return None if self.explicit_home else os.environ.get(name)


def _read_opt(path: Path) -> tuple[Optional[str], str, bool]:
    if path.is_file():
        t, nl, bom = _read_text(path)
        return t, nl, bom
    return None, "\n", False


def _file_change(path: Path, new: Optional[str], note: str = "") -> FileChange:
    old, nl, bom = _read_opt(path)
    return FileChange(path, old, new, nl, bom, note)


def _rules_snippet(clients_dir: Path, client: str) -> str:
    name = {"codex": "AGENTS.md", "gemini": "GEMINI.md"}[client]
    p = clients_dir / client / name
    if not p.is_file():
        raise Refused(f"rules snippet {p} not found")
    return p.read_text("utf-8")


def plan_client(client: str, env: Env, remove: bool) -> Plan:
    plan = Plan(client)
    home, cdir = env.home, env.clients_dir
    reference = cdir / "common" / "REFERENCE.md"
    skill_src = cdir / client / "skills" / SKILL_NAME
    if not remove and not skill_src.is_dir():
        raise Refused(f"skill source {skill_src} not found (use --clients-dir)")
    sa = env.spec_args
    spec = resolve_server_spec(sa["mcp_command"], sa["toolset"], sa["data_dir"], client == "hermes")
    files = collect_skill_files(skill_src, reference, client) if not remove else {}

    if client == "claude-code":
        cfg_dir = Path(env.env("CLAUDE_CONFIG_DIR") or home / ".claude")
        cfg_file = (Path(env.env("CLAUDE_CONFIG_DIR")) / ".claude.json") if env.env("CLAUDE_CONFIG_DIR") else home / ".claude.json"
        old, _, _ = _read_opt(cfg_file)
        new = remove_json(old, SERVER_KEY[client]) if remove else merge_json(old, SERVER_KEY[client], json_entry(spec, True))
        plan.files.append(_file_change(cfg_file, new, "MCP server (user scope)"))
        skills = cfg_dir / "skills"
        plan.dirs.append(plan_skill(skills / SKILL_NAME, files, [], remove))
        plan.notes.append("Claude Code rewrites ~/.claude.json while running; restart it after installing. Alternative without touching "
                          "user config: `claude --plugin-dir clients/claude-code`.")
    elif client == "codex":
        codex_home = Path(env.env("CODEX_HOME") or home / ".codex")
        cfg_file = codex_home / "config.toml"
        old, _, _ = _read_opt(cfg_file)
        new = remove_toml(old) if remove else merge_toml(old, spec)
        plan.files.append(_file_change(cfg_file, new, "[mcp_servers.rebuild_studio]"))
        agents_skills = home / ".agents" / "skills"
        plan.dirs.append(plan_skill(agents_skills / SKILL_NAME, files, [codex_home / "skills"], remove))
        if env.rules or remove:
            plan.files.append(_rules_change(codex_home / "AGENTS.md", client, env, remove))
    elif client == "gemini":
        gem = home / ".gemini"
        cfg_file = gem / "settings.json"
        old, _, _ = _read_opt(cfg_file)
        new = remove_json(old, SERVER_KEY[client]) if remove else merge_json(old, SERVER_KEY[client], json_entry(spec, False))
        plan.files.append(_file_change(cfg_file, new, "mcpServers"))
        plan.dirs.append(plan_skill(gem / "skills" / SKILL_NAME, files, [home / ".agents" / "skills"], remove))
        if env.rules or remove:
            plan.files.append(_rules_change(gem / "GEMINI.md", client, env, remove))
    elif client == "hermes":
        hh = env.env("HERMES_HOME")
        if hh:
            hermes_home = Path(os.path.expandvars(os.path.expanduser(hh)))
        elif os.name == "nt" and not env.explicit_home:
            hermes_home = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "hermes"
        else:
            hermes_home = home / ".hermes"
        cfg_file = hermes_home / "config.yaml"
        old, _, _ = _read_opt(cfg_file)
        new = remove_yaml(old) if remove else merge_yaml(old, spec)
        plan.files.append(_file_change(cfg_file, new, "mcp_servers.rebuild_studio"))
        plan.dirs.append(plan_skill(hermes_home / "skills" / SKILL_NAME, files, [], remove))
        if env.rules:
            plan.notes.append("hermes: --rules has no effect (the skill carries the rules).")
    return plan


def _rules_change(path: Path, client: str, env: Env, remove: bool) -> FileChange:
    old, _, _ = _read_opt(path)
    new = remove_markdown(old) if remove else merge_markdown(old, _rules_snippet(env.clients_dir, client))
    return _file_change(path, new, "short rules block")


def detected(client: str, env: Env) -> bool:
    home = env.home
    probes = {
        "claude-code": [home / ".claude", home / ".claude.json"],
        "codex": [Path(env.env("CODEX_HOME") or home / ".codex")],
        "gemini": [home / ".gemini"],
        "hermes": [Path(env.env("HERMES_HOME") or home / ".hermes")],
    }[client]
    exe = {"claude-code": "claude", "codex": "codex", "gemini": "gemini", "hermes": "hermes"}[client]
    return any(p.exists() for p in probes) or (not env.explicit_home and shutil.which(exe) is not None)


# ------------------------------------------------------------------------------------------------ apply
def _backup(path: Path) -> Path:
    b = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
    n = 1
    while b.exists():
        b = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}-{n}")
        n += 1
    shutil.copy2(path, b)
    return b


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def apply_plan(plan: Plan, dry_run: bool, out: Callable[[str], None]) -> None:
    out(f"== {plan.client}")
    for fc in plan.files:
        if not fc.changed:
            out(f"  unchanged: {fc.path}  ({fc.note})")
            continue
        verb = "delete" if fc.new is None else ("create" if fc.old is None else "update")
        out(f"  {'would ' if dry_run else ''}{verb}: {fc.path}  ({fc.note})")
        if dry_run:
            diff = _unified(fc.path, fc.old or "", fc.new or "")
            out(diff.rstrip("\n") if diff else "")
            continue
        if fc.old is not None:
            out(f"  backup: {_backup(fc.path)}")
        if fc.new is None:
            fc.path.unlink()
        else:
            _write_atomic(fc.path, _encode(fc.new, fc.nl, fc.bom))
    for dc in plan.dirs:
        if dc.status in ("unchanged", "absent"):
            out(f"  {dc.status}: {dc.path}")
            continue
        verb = {"created": "create", "updated": "update", "removed": "remove"}[dc.status]
        out(f"  {'would ' if dry_run else ''}{verb}: {dc.path}")
        for d in dc.detail:
            out(f"      {d}")
        if dry_run:
            continue
        if dc.remove:
            shutil.rmtree(dc.path)
            continue
        if dc.path.exists():
            shutil.rmtree(dc.path)  # managed directory (marker verified in plan): replace wholesale
        for rel, data in dc.files.items():
            _write_atomic(dc.path / rel, data)
    for n in plan.notes:
        out(f"  note: {n}")


# ------------------------------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="install-clients.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--client", required=True, choices=list(CLIENTS) + ["all"])
    p.add_argument("--dry-run", action="store_true", help="print what would change (with diffs) and touch nothing")
    p.add_argument("--remove", action="store_true", help="remove what this installer added")
    p.add_argument("--mcp-command", default=None, help="path to rebuild-mcp (default: PATH lookup, else python -m ...)")
    p.add_argument("--data-dir", default=None, help="REBUILD_STUDIO_DATA to pass to the server (always set for Hermes)")
    p.add_argument("--toolset", choices=("minimal", "analysis", "rebuild", "all"), default="all")
    p.add_argument("--rules", action="store_true", help="also add the short rules block to the global AGENTS.md / GEMINI.md (Codex, Gemini)")
    p.add_argument("--home", default=None, help="treat this directory as the user's home (testing; ignores *_HOME env overrides)")
    p.add_argument("--clients-dir", default=None, help="directory holding the client packages (default: <repo>/clients)")
    return p


def main(argv: Optional[list[str]] = None, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home) if args.home else Path.home()
    clients_dir = Path(args.clients_dir) if args.clients_dir else Path(__file__).resolve().parent.parent / "clients"
    env = Env(home=home, explicit_home=bool(args.home), clients_dir=clients_dir, rules=args.rules,
              spec_args={"mcp_command": args.mcp_command, "toolset": args.toolset, "data_dir": args.data_dir})
    targets = list(CLIENTS) if args.client == "all" else [args.client]
    plans: list[Plan] = []
    try:
        for c in targets:
            if args.client == "all" and not detected(c, env):
                out(f"== {c}\n  skipped: not detected (run with --client {c} to install anyway)")
                continue
            plans.append(plan_client(c, env, args.remove))
    except Refused as exc:
        out(f"REFUSED: {exc}")
        return 1
    # Everything validated before the first write: a refusal for one client leaves all clients untouched.
    for plan in plans:
        apply_plan(plan, args.dry_run, out)
    if args.dry_run:
        out("dry run: nothing was changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
