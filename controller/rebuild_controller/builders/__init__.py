"""Builders turn a candidate source dir into dist/ with a launch spec. Staged builds; publication is atomic (pipeline)."""
from __future__ import annotations

from typing import Any

from ..jobs.runner import StageContext


def build(ctx: StageContext, target_language: str, source_dir, dist_dir) -> dict[str, Any]:
    if target_language in ("rust", "rust_bevy"):
        from .rust import build_rust
        return build_rust(ctx, source_dir, dist_dir, bevy=(target_language == "rust_bevy"))
    if target_language == "web":
        from .web import build_web
        return build_web(ctx, source_dir, dist_dir)
    raise ValueError(f"unsupported target language {target_language}")
