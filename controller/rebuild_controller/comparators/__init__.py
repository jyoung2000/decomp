"""Deterministic comparators. Each returns a ComparisonResult; the Verifier is the only caller that records verdicts."""
from .base import ComparisonResult, Channel  # noqa: F401
from .cli import compare_cli_scenario  # noqa: F401
from .files import compare_files  # noqa: F401
from .images import compare_images  # noqa: F401
from .web import run_web_scenario, compare_web_capture  # noqa: F401
