#!/usr/bin/env python3
"""Lightweight structural check for the PowerShell scripts when `pwsh` is not installed.

Checks, per file: balanced (), {}, [] outside strings/comments; every here-string (@' ... '@ / @" ... "@) is terminated at
the start of a line; block comments (<# ... #>) and quoted strings are terminated; no tab-indented here-string terminators;
UTF-8 decodable; (optional) a UTF-8 BOM or non-ASCII characters in a file are reported as warnings because Windows
PowerShell 5.1 reads BOM-less files as ANSI. When `pwsh` is on PATH the real parser is used as well
([System.Management.Automation.Language.Parser]::ParseFile).

This is NOT a PowerShell parser: it cannot find a misspelled cmdlet or a type error. It catches the failures a truncated or
hand-edited file usually has (cut-off file, missing brace, unterminated here-string/quote).

Usage: python3 scripts/windows/tests/parse_check.py [files or dirs ...]   (default: scripts/windows/**/*.ps1)
Exit 0 = all files pass, 1 = at least one structural error, 2 = usage.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OPEN = {"(": ")", "{": "}", "[": "]"}
CLOSE = {v: k for k, v in OPEN.items()}


def check_text(text: str) -> list[str]:
    errors: list[str] = []
    n = len(text)
    i = 0
    line = 1
    stack: list[tuple[str, int]] = []
    # Stack of contexts so that "$( ... )" inside a double-quoted string can contain code again.
    # Each entry: ("code", None) | ("dq", start_line) ; code contexts opened by $( remember paren depth.
    ctx: list[list] = [["code", 0, 0]]  # [kind, start_line, paren_depth_for_subexpr]

    def at_line_start(pos: int) -> bool:
        return pos == 0 or text[pos - 1] == "\n"

    while i < n:
        c = text[i]
        kind = ctx[-1][0]
        if c == "\n":
            line += 1
        if kind == "dq":
            if c == "`" and i + 1 < n:
                if text[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                ctx.pop()
                i += 1
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "(":
                ctx.append(["code", line, 1])  # subexpression: parens depth starts at 1
                stack.append(("(", line))
                i += 2
                continue
            i += 1
            continue
        # ---- code context
        if c == "#" :
            # line comment (a '#' inside a bareword like foo#bar is rare in these scripts)
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "<" and text.startswith("<#", i):
            j = text.find("#>", i + 2)
            if j < 0:
                errors.append(f"line {line}: unterminated block comment <# ... #>")
                return errors
            line += text.count("\n", i, j)
            i = j + 2
            continue
        if c == "@" and i + 1 < n and text[i + 1] in "'\"":
            q = text[i + 1]
            # here-string header must be followed by end of line
            j = i + 2
            while j < n and text[j] in " \t\r":
                j += 1
            if j < n and text[j] == "\n":
                term = q + "@"
                k = j + 1
                found = -1
                while k <= n:
                    if text.startswith(term, k) and at_line_start(k):
                        found = k
                        break
                    nl = text.find("\n", k)
                    if nl < 0:
                        break
                    k = nl + 1
                if found < 0:
                    errors.append(f"line {line}: here-string @{q} is never terminated by a line starting with {term}")
                    return errors
                line += text.count("\n", i, found)
                i = found + 2
                continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                if text[j] == "\n":
                    line += 1
                j += 1
            else:
                errors.append(f"line {line}: unterminated single-quoted string")
                return errors
            i = j + 1
            continue
        if c == '"':
            ctx.append(["dq", line, 0])
            i += 1
            continue
        if c == "`":
            if i + 1 < n and text[i + 1] == "\n":
                line += 1
            i += 2
            continue
        if c in OPEN:
            stack.append((c, line))
            if len(ctx) > 1 and c == "(":
                ctx[-1][2] += 1
        elif c in CLOSE:
            if not stack or stack[-1][0] != CLOSE[c]:
                top = f"'{stack[-1][0]}' from line {stack[-1][1]}" if stack else "nothing open"
                errors.append(f"line {line}: unexpected '{c}' ({top})")
                return errors
            stack.pop()
            if len(ctx) > 1 and c == ")":
                ctx[-1][2] -= 1
                if ctx[-1][2] == 0:
                    ctx.pop()  # end of $( ... ) -> back into the double-quoted string
        i += 1

    if len(ctx) > 1:
        errors.append(f"line {ctx[-1][1]}: unterminated {'double-quoted string' if ctx[-1][0] == 'dq' else '$( ) subexpression'}")
    for ch, ln in stack:
        errors.append(f"line {ln}: '{ch}' is never closed")
    return errors


def check_file(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [f"not valid UTF-8: {exc}"], warnings
    if not text.strip():
        errors.append("file is empty")
        return errors, warnings
    if not raw.startswith(b"\xef\xbb\xbf") and any(ord(ch) > 127 for ch in text):
        bad = next(i for i, ch in enumerate(text) if ord(ch) > 127)
        warnings.append(f"line {text.count(chr(10), 0, bad) + 1}: non-ASCII character without a UTF-8 BOM (Windows PowerShell 5.1 reads it as ANSI)")
    errors.extend(check_text(text))
    return errors, warnings


def pwsh_parse(paths: list[Path]) -> dict[Path, list[str]]:
    exe = shutil.which("pwsh")
    if not exe:
        return {}
    script = (
        "$ErrorActionPreference='Stop'; foreach($f in ($env:RS_PARSE_FILES -split \"`n\")){ $t=$null;$e=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);"
        "foreach($x in $e){ Write-Output ('{0}|{1}|{2}' -f $f,$x.Extent.StartLineNumber,$x.Message) } }"
    )
    out: dict[Path, list[str]] = {}
    env = dict(os.environ, RS_PARSE_FILES="\n".join(str(p) for p in paths))
    proc = subprocess.run([exe, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=300, env=env)
    for ln in proc.stdout.splitlines():
        f, num, msg = ln.split("|", 2)
        out.setdefault(Path(f), []).append(f"line {num}: {msg}")
    if proc.returncode != 0:
        out.setdefault(paths[0], []).append(f"pwsh parser run failed: {proc.stderr.strip()[:200]}")
    return out


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    targets = [Path(a) for a in argv] or [root]
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(sorted(t.rglob("*.ps1")))
        elif t.is_file():
            files.append(t)
        else:
            print(f"not found: {t}", file=sys.stderr)
            return 2
    if not files:
        print("no .ps1 files found", file=sys.stderr)
        return 2
    failed = 0
    real = pwsh_parse(files)
    print(f"parser: {'pwsh AST + structural check' if shutil.which('pwsh') else 'structural check only (pwsh not installed)'}")
    for f in files:
        errs, warns = check_file(f)
        errs += [f"pwsh: {m}" for m in real.get(f, [])]
        status = "FAIL" if errs else "ok  "
        print(f"{status} {f}")
        for e in errs:
            print(f"       error: {e}")
        for w in warns:
            print(f"       warn:  {w}")
        failed += bool(errs)
    print(f"{len(files)} file(s) checked, {failed} with errors")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
