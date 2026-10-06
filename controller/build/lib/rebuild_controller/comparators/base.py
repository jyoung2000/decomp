from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Channel(str, Enum):
    EXIT_CODE = "exit_code"
    STDOUT = "stdout"
    STDERR = "stderr"
    FILES = "files"
    STATE = "state"
    DOM = "dom"
    STORAGE = "storage"
    OFFLINE = "offline"
    SCREENSHOT = "screenshot"
    PERFORMANCE = "performance"


@dataclass
class ComparisonResult:
    channel: str
    rule: str                       # e.g. "exact", "normalize:crlf", "pixel:tolerance=0.02"
    verdict: str                    # pass|fail|error|skipped
    details: dict[str, Any] = field(default_factory=dict)
    original_hash: str | None = None
    candidate_hash: str | None = None
    tolerance: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def normalize_text(text: str, rules: list[str]) -> str:
    out = text
    for r in rules:
        if r == "crlf":
            out = out.replace("\r\n", "\n")
        elif r == "trailing_ws":
            out = "\n".join(line.rstrip() for line in out.split("\n"))
        elif r == "strip":
            out = out.strip()
        elif r.startswith("mask:"):
            import re
            out = re.sub(r[5:], "<masked>", out)
    return out
