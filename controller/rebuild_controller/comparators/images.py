"""Screenshot comparison. tolerance=0 → exact pixels required; otherwise the declared fraction of differing pixels is allowed
and the result is labelled 'approximate', never pixel-perfect."""
from __future__ import annotations

from pathlib import Path

from ..ids import sha256_file
from .base import ComparisonResult


def compare_images(expected: Path, actual: Path, *, tolerance: float = 0.0, per_channel_delta: int = 0, diff_out: Path | None = None) -> ComparisonResult:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return ComparisonResult("screenshot", "pixel", "error", {"error": "Pillow not installed"})
    if not expected.exists() or not actual.exists():
        return ComparisonResult("screenshot", "pixel", "error", {"error": "missing image", "expected": expected.exists(), "actual": actual.exists()})
    a = Image.open(expected).convert("RGB"); b = Image.open(actual).convert("RGB")
    rule = "pixel:exact" if tolerance == 0 and per_channel_delta == 0 else f"pixel:approximate(max_diff_fraction={tolerance},channel_delta={per_channel_delta})"
    if a.size != b.size:
        return ComparisonResult("screenshot", rule, "fail", {"error": "size mismatch", "expected": a.size, "actual": b.size},
                                original_hash=sha256_file(expected), candidate_hash=sha256_file(actual), tolerance={"max_diff_fraction": tolerance, "channel_delta": per_channel_delta})
    diff = ImageChops.difference(a, b)
    px = diff.getdata()
    total = a.size[0] * a.size[1]
    differing = sum(1 for p in px if max(p) > per_channel_delta)
    frac = differing / total if total else 0.0
    verdict = "pass" if frac <= tolerance else "fail"
    arts = []
    if diff_out is not None and differing:
        diff_out.parent.mkdir(parents=True, exist_ok=True)
        diff.point(lambda v: 255 if v > per_channel_delta else 0).save(diff_out)
        arts.append(str(diff_out))
    return ComparisonResult("screenshot", rule, verdict, {"differing_pixels": differing, "total_pixels": total, "diff_fraction": frac, "size": a.size,
                                                          "label": "exact" if rule == "pixel:exact" else "approximate visual similarity (not pixel-perfect)"},
                            original_hash=sha256_file(expected), candidate_hash=sha256_file(actual), tolerance={"max_diff_fraction": tolerance, "channel_delta": per_channel_delta}, artifacts=arts)
