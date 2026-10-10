"""One tools folder for a pipeline test when the pinned tools live in different folders (REBUILD_STUDIO_TOOLS and the per-user
%LOCALAPPDATA%/RebuildStudio/tools). Directory junctions on Windows: the tests only read through them, and ``remove`` deletes the
junctions themselves, never the tool folders they point to."""
from __future__ import annotations

import os
from pathlib import Path

TOOL_DIRS = ("ilspycmd", "dotnet", "dotnet-sdk", "cfr", "jre", "jdk21")


def tool_roots() -> list[Path]:
    roots = []
    if os.environ.get("REBUILD_STUDIO_TOOLS"):
        roots.append(Path(os.environ["REBUILD_STUDIO_TOOLS"]))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "RebuildStudio" / "tools")
    return [r for r in roots if r.is_dir()]


def find_tool_dir(name: str) -> Path | None:
    return next((r / name for r in tool_roots() if (r / name).is_dir()), None)


def merged_tools(dest: Path) -> tuple[Path, list[Path]]:
    roots = tool_roots()
    if os.name != "nt":
        return (roots[0] if roots else dest), []
    import _winapi
    dest.mkdir(parents=True, exist_ok=True)
    links = []
    for name in TOOL_DIRS:
        src = find_tool_dir(name)
        if src is not None:
            _winapi.CreateJunction(str(src), str(dest / name))
            links.append(dest / name)
    return dest, links


def remove(links: list[Path]) -> None:
    for ln in links:
        try:
            os.rmdir(ln)
        except OSError:
            pass
