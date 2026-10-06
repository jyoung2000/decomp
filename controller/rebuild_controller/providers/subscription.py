"""Subscription handoff modes: capability-gated, user-driven, credential-free.

A "handoff" means the USER's own vendor CLI, signed in by the user, is started with a task file. Rebuild Studio never:
reads or copies CLI session/credential files, scrapes sessions, proxies subscription tokens, or relays credentials. The
child process gets an isolated environment (no provider API keys) so a subscription login cannot be silently swapped for
a BYOK key, and no task text is placed on the command line (it goes to stdin), so there is no shell-quoting surface.

``supported`` is True only where the vendor's own documentation describes the path and it was read on ``checked_on``.
Anything unverified or forbidden is ``supported=False`` with the reason in ``access_method``.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .base import UsageLimit
from .secrets import isolated_env, redact

CHECKED_ON = "2026-10-06"
MAX_TASK_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 1_000_000


@dataclass(frozen=True)
class HandoffMode:
    key: str                       # openai_siwc | claude_agent_sdk | gemini_cli
    provider: str
    label: str
    supported: bool
    access_method: str
    limits: str
    checked_on: str
    cli: str                       # executable name on the user's PATH
    argv: tuple[str, ...]          # fixed arguments; the task goes to stdin
    sources: tuple[str, ...] = ()
    verification_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "provider": self.provider, "label": self.label, "supported": self.supported,
                "access_method": self.access_method, "limits": self.limits, "checked_on": self.checked_on, "cli": self.cli,
                "argv": list(self.argv), "sources": list(self.sources), "verification_gaps": list(self.verification_gaps)}


_MODES: dict[str, HandoffMode] = {
    "openai_siwc": HandoffMode(
        key="openai_siwc", provider="openai", label="Codex CLI (Sign in with ChatGPT)", supported=True,
        access_method=("The user's own `codex` CLI signed in with 'Sign in with ChatGPT' (the openai/codex README documents this as "
                       "the way to use Codex with a ChatGPT Plus/Pro/Business/Edu/Enterprise plan). Rebuild Studio only runs "
                       "`codex exec -` with the task on stdin; it never touches Codex auth files or tokens."),
        limits=("Usage counts against the user's ChatGPT plan Codex limits. Exact numbers are plan-specific and could not be read "
                "(developers.openai.com unreachable on 2026-10-06; verify)."),
        checked_on=CHECKED_ON, cli="codex", argv=("exec", "-"),
        sources=("https://raw.githubusercontent.com/openai/codex/main/README.md",
                 "https://raw.githubusercontent.com/openai/codex/main/codex-rs/exec/src/cli.rs"),
        verification_gaps=("developers.openai.com/codex/auth unreachable: sign-in details and plan limits not read",
                           "terms for scripted/orchestrated use not read; the user remains responsible for their plan's terms")),
    "claude_agent_sdk": HandoffMode(
        key="claude_agent_sdk", provider="anthropic", label="Claude Code / Agent SDK with claude.ai login", supported=False,
        access_method=("NOT OFFERED. Anthropic's Agent SDK overview (read 2026-10-06) states: 'Unless previously approved, Anthropic "
                       "does not allow third party developers to offer claude.ai login or rate limits for their products, "
                       "including agents built on the Claude Agent SDK. Use the API key authentication methods'. Use an Anthropic "
                       "API key connection (BYOK) instead, or run `claude` yourself in a terminal. Launching is refused unless the "
                       "caller asserts vendor_approved=True."),
        limits="n/a (not offered); API-key usage is billed per token under the BYOK connection.",
        checked_on=CHECKED_ON, cli="claude", argv=("-p", "Follow the task provided on stdin."),
        sources=("https://code.claude.com/docs/en/agent-sdk/overview",),
        verification_gaps=("approval status for this product cannot be determined from the docs",)),
    "gemini_cli": HandoffMode(
        key="gemini_cli", provider="gemini", label="Gemini CLI (Sign in with Google)", supported=True,
        access_method=("The user's own `gemini` CLI signed in with 'Sign in with Google' (OAuth, documented in the google-gemini/"
                       "gemini-cli README). Rebuild Studio only runs `gemini -p <fixed instruction>` with the task on stdin; it "
                       "never touches Gemini CLI credential files."),
        limits=("Quota and terms follow the user's Google account/plan (see https://cloud.google.com/gemini/docs/quotas); numbers "
                "are not copied here because they change."),
        checked_on=CHECKED_ON, cli="gemini", argv=("-p", "Follow the task provided on stdin."),
        sources=("https://raw.githubusercontent.com/google-gemini/gemini-cli/main/README.md",),
        verification_gaps=("authentication guide and headless docs pages were not read (README only)",
                           "stdin+`-p` combination taken from common CLI behaviour; verify with `gemini --help`")),
}


def get_mode(key: str) -> HandoffMode:
    try:
        return _MODES[key]
    except KeyError:
        raise KeyError(f"unknown handoff mode {key!r}; expected one of {sorted(_MODES)}") from None


def all_modes() -> list[HandoffMode]:
    return list(_MODES.values())


def cli_available(mode: HandoffMode, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which(mode.cli) is not None


_LIMIT_RE = re.compile(r"(?i)(usage limit|rate limit|limit (has been )?reached|quota (exceeded|exhausted)|resource_exhausted|"
                       r"you(?:'|’)ve hit your|too many requests|\b429\b)")


@dataclass
class LaunchResult:
    launched: bool
    mode: str
    reason: str = ""
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    usage_limit: bool = False
    argv: list[str] = field(default_factory=list)

    def raise_for_usage_limit(self) -> None:
        if self.usage_limit:
            raise UsageLimit(f"{self.mode}: the vendor CLI reported a usage/rate limit: {self.stderr[-300:] or self.stdout[-300:]}",
                             provider=self.mode, retry_safe=False)


Runner = Callable[..., tuple[int, str, str, bool]]


def _default_runner(argv: Sequence[str], *, input: str, cwd: str | None, env: Mapping[str, str], timeout: float) -> tuple[int, str, str, bool]:
    kw: dict[str, Any] = {}
    if os.name == "posix":
        kw["start_new_session"] = True
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            env=dict(env), text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        except (OSError, ProcessLookupError):
            proc.kill()
        out, err = proc.communicate()
        return -9, out or "", err or "", True


def launch_external(mode_key: str, task_file: str | Path | None, *, cwd: str | Path | None = None, timeout_s: float = 900.0,
                    runner: Runner | None = None, which: Callable[[str], str | None] = shutil.which,
                    vendor_approved: bool = False, env: Mapping[str, str] | None = None) -> LaunchResult:
    """Run the user's own CLI with ``task_file`` on stdin. Returns a structured result; never raises for ordinary
    failures (use ``result.raise_for_usage_limit()`` to turn a vendor limit message into ``UsageLimit``)."""
    mode = get_mode(mode_key)
    if not mode.supported and not vendor_approved:
        return LaunchResult(False, mode.key, f"{mode.label} is not a supported mode: {mode.access_method}")
    if not task_file:
        return LaunchResult(False, mode.key, "no task file given")
    tf = Path(task_file)
    if not tf.is_file():
        return LaunchResult(False, mode.key, f"task file not found: {tf}")
    if tf.stat().st_size > MAX_TASK_BYTES:
        return LaunchResult(False, mode.key, f"task file larger than {MAX_TASK_BYTES} bytes; open the CLI yourself instead")
    exe = which(mode.cli)
    if not exe:
        return LaunchResult(False, mode.key, f"`{mode.cli}` was not found on PATH; install it and sign in yourself, then retry")
    argv = [exe, *mode.argv]
    child_env = isolated_env(env or None)
    run = runner or _default_runner
    started = time.time()
    rc, out, err, timed_out = run(argv, input=tf.read_text(encoding="utf-8", errors="replace"),
                                  cwd=str(cwd) if cwd else None, env=child_env, timeout=timeout_s)
    out, err = redact(out)[-MAX_OUTPUT_CHARS:], redact(err)[-MAX_OUTPUT_CHARS:]
    limited = rc != 0 and not timed_out and bool(_LIMIT_RE.search(err) or _LIMIT_RE.search(out))
    return LaunchResult(True, mode.key, reason=f"finished in {time.time() - started:.1f}s", returncode=rc, stdout=out, stderr=err,
                        timed_out=timed_out, usage_limit=limited, argv=[mode.cli, *mode.argv])
