"""Live log: plain-English lines about what a rebuild is doing right now.

Stages call ``ctx.log(text, level, detail)``; each call becomes a persisted ``job.log`` event
``{job_id, stage, milestone, plan_item_id, level, text, detail, at}`` (plus ``message``, a legacy alias of ``text``).

- Rate limited per job: at most ``MAX_PER_SECOND`` (5) info lines per second. Excess info lines are coalesced: only the latest
  one per coalesce key is kept and is emitted when the window rolls over (or when the job ends), so a progress counter never
  floods the feed and the final count is never lost. Warnings and errors have their own budget of the same size.
- Redacted with the providers' ``redact()`` (registered secrets, ``sk-`` keys, bearer/Authorization headers, ...). Prompts and
  message bodies are never accepted: only ``text`` and a short ``detail`` are stored.

``case_log`` builds GET /cases/{id}/log: recent ``job.log`` / ``ai.activity`` rows merged with job state transitions.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

from .ids import now_iso
from .providers.secrets import redact

LEVELS = ("info", "warn", "error")
MAX_PER_SECOND = 5
TEXT_MAX = 400
DETAIL_MAX = 1500
_FORBIDDEN_KEYS = {"prompt", "messages", "system", "system_prompt", "body", "headers", "authorization", "api_key", "token", "secret"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(text: Any, limit: int) -> str:
    s = redact(_ANSI.sub("", "" if text is None else str(text))).replace("\r", "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def tail_lines(text: str | bytes | None, n: int = 5, limit: int = DETAIL_MAX) -> str:
    """Last ``n`` non-empty lines of tool output, redacted and bounded (for failure details)."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [ln.rstrip() for ln in _ANSI.sub("", text).splitlines() if ln.strip()]
    out = redact("\n".join(lines[-n:]))
    return out if len(out) <= limit else "…" + out[-(limit - 1):]


class LogLimiter:
    """Per-job limiter: <= per_second info and <= per_second problem lines per 1 s window; info overflow is coalesced."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, per_second: int = MAX_PER_SECOND):
        self.clock = clock
        self.per_second = per_second
        self._lock = threading.Lock()
        self._start = -1e9
        self._n = {"info": 0, "problem": 0}
        self._pending: dict[str, dict[str, Any]] = {}   # coalesce key -> latest held entry
        self.suppressed = 0

    def _roll(self) -> None:
        now = self.clock()
        if now - self._start >= 1.0:
            self._start = now
            self._n = {"info": 0, "problem": 0}

    def admit(self, entry: dict[str, Any], key: str | None) -> list[dict[str, Any]]:
        """Entries to emit now: held ones from earlier windows first, then ``entry`` if it fits the budget."""
        out: list[dict[str, Any]] = []
        with self._lock:
            self._roll()
            bucket = "info" if entry["level"] == "info" else "problem"
            for k in list(self._pending):
                if self._n["info"] < self.per_second:
                    self._n["info"] += 1
                    out.append(self._pending.pop(k))
            if bucket == "info" and key and key in self._pending:
                self._pending.pop(key)              # a newer line for the same key supersedes the held one
                self.suppressed += 1
            if self._n[bucket] < self.per_second:
                self._n[bucket] += 1
                out.append(entry)
            elif bucket == "info":
                k = key or "_"
                if k in self._pending:
                    self.suppressed += 1
                self._pending[k] = entry
            else:
                self.suppressed += 1
        return out

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._pending.values())
            self._pending.clear()
        return out


def emit_job_log(events, job, limiter: LogLimiter, text: str, level: str = "info", detail: str | None = None, *,
                 plan_item_id: str | None = None, key: str | None = None, extra: dict[str, Any] | None = None) -> None:
    level = level if level in LEVELS else "info"
    entry: dict[str, Any] = {"job_id": job.job_id, "stage": job.stage, "milestone": getattr(job, "milestone_id", None),
                             "plan_item_id": plan_item_id, "level": level, "text": clean(text, TEXT_MAX), "at": now_iso()}
    if not entry["text"]:
        return
    if detail:
        entry["detail"] = clean(detail, DETAIL_MAX)
    entry["message"] = entry["text"]
    for k, v in (extra or {}).items():
        if k.lower() in _FORBIDDEN_KEYS or k in entry:
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            entry[k] = v
        elif isinstance(v, str):
            entry[k] = clean(v, 200)
    for e in limiter.admit(entry, key):
        events.emit("job.log", e, case_id=job.case_id, job_id=job.job_id)


def flush_job_log(events, job, limiter: LogLimiter) -> None:
    for e in limiter.drain():
        events.emit("job.log", e, case_id=job.case_id, job_id=job.job_id)


# --------------------------------------------------------------------------------------------- GET /cases/{id}/log
_KINDS = ("job.log", "job.started", "job.completed", "job.failed", "job.blocked", "job.cancelled", "job.retry", "ai.activity")
LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}


def _first_line(s: Any, limit: int = 300) -> str:
    for ln in str(s or "").splitlines():
        if ln.strip():
            return clean(ln, limit)
    return ""


def render_event(row: dict[str, Any], lookup: Callable[[str | None], dict[str, Any]]) -> dict[str, Any] | None:
    """One stored event row -> a log entry, or None if the event is not shown in the live log."""
    kind, p = row["kind"], row["payload"]
    jid = p.get("job_id") or row.get("job_id")
    info = lookup(jid) if jid else {}
    base = {"seq": row["seq"], "at": p.get("at") or row["ts"], "job_id": jid, "stage": p.get("stage") or info.get("stage"),
            "milestone": p.get("milestone") or info.get("milestone"), "plan_item_id": p.get("plan_item_id"), "detail": None}
    title = p.get("title") or info.get("title") or base["stage"] or "job"
    if kind == "job.log":
        text = p.get("text") or p.get("message")
        if not text:
            return None
        return {**base, "kind": "log", "level": p.get("level") if p.get("level") in LEVELS else "info", "text": clean(text, TEXT_MAX),
                "detail": clean(p["detail"], DETAIL_MAX) if p.get("detail") else None}
    if kind == "ai.activity":
        text = p.get("text")
        if not text:
            return None
        bad = str(p.get("outcome") or "") in ("failed", "error", "refused", "timeout")
        return {**base, "kind": "ai", "level": "warn" if bad else "info", "text": clean(text, TEXT_MAX), "provider": p.get("provider"),
                "model": p.get("model"), "outcome": p.get("outcome")}
    if kind == "job.started":
        att = int(p.get("attempt") or 1)
        return {**base, "kind": "job", "level": "info", "text": f"Started: {title}" + (f" (attempt {att})" if att > 1 else "")}
    if kind == "job.completed":
        return {**base, "kind": "job", "level": "info", "text": f"Finished: {title}"}
    if kind == "job.failed":
        return {**base, "kind": "job", "level": "error", "text": f"Failed: {title}" + (f" — {_first_line(p.get('error'))}" if p.get("error") else ""),
                "detail": tail_lines(p.get("error"), 5) or None}
    if kind == "job.cancelled":
        return {**base, "kind": "job", "level": "info", "text": f"Cancelled: {title}"}
    if kind == "job.blocked":
        return {**base, "kind": "job", "level": "warn", "text": f"Waiting: {title} — {_first_line(p.get('blocker') or p.get('error') or 'blocked')}"}
    if kind == "job.retry":
        return {**base, "kind": "job", "level": "warn", "text": f"Retrying {title} (attempt {p.get('attempt')} did not finish): {_first_line(p.get('error'), 200)}"}
    return None


def case_log(events, jobs, case_id: str, *, since: int = 0, limit: int = 300, level: str | None = None, stage: str | None = None) -> dict[str, Any]:
    """Recent log entries, oldest first. ``since=0`` returns the newest ``limit`` entries; ``since>0`` returns up to ``limit``
    entries newer than that seq. ``level`` warn|error keeps that level and worse; info/all keeps everything."""
    limit = int(min(max(limit, 1), 2000))
    since = max(int(since), 0)
    marks = ",".join("?" for _ in _KINDS)
    head = f"SELECT seq, ts, job_id, kind, payload FROM events WHERE case_id=? AND kind IN ({marks}) "
    rank = LEVEL_RANK.get(level or "", 0)
    cache: dict[str, dict[str, Any]] = {}

    def lookup(jid: str | None) -> dict[str, Any]:
        if jid and jid not in cache:
            try:
                j = jobs.get(jid)
                cache[jid] = {"stage": j.stage, "title": j.title, "milestone": j.milestone_id}
            except Exception:  # noqa: BLE001
                cache[jid] = {}
        return cache.get(jid or "", {})

    out: list[dict[str, Any]] = []
    cursor: int | None = None          # tail mode: seq of the oldest row read so far; head mode: seq of the newest
    for _ in range(40):                # page through rows so a filter that drops many rows still fills the page
        if since <= 0:
            rows = events.db.query(head + ("AND seq<? " if cursor is not None else "") + "ORDER BY seq DESC LIMIT 1000",
                                   (case_id, *_KINDS, *([cursor] if cursor is not None else [])))
        else:
            rows = events.db.query(head + "AND seq>? ORDER BY seq ASC LIMIT 1000", (case_id, *_KINDS, since if cursor is None else cursor))
        if not rows:
            break
        for r in rows:
            r["payload"] = json.loads(r["payload"])
            e = render_event(r, lookup)
            if e is None or LEVEL_RANK[e["level"]] < rank or (stage and e.get("stage") != stage):
                continue
            out.append(e)
        cursor = rows[-1]["seq"]
        if len(out) >= limit or len(rows) < 1000:
            break
    out = list(reversed(out[:limit])) if since <= 0 else out[:limit]
    return {"entries": out, "latest_seq": events.latest_seq(), "limit": limit}
