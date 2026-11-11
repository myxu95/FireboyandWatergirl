"""The host: the agent session that started a task. Find it and read its token usage.

A host keeps spending tokens while a task runs (every status check or wake-up is a
model call that re-reads the host's whole context), so its usage during the task is
reported next to the worker's.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .usage import CallUsage, Totals

ROLLOUT_RE = re.compile(r"/sessions/\d{4}/\d{2}/\d{2}/rollout-.*-([0-9a-f-]{36})\.jsonl$")


def _claude_transcript(session_id: str) -> Path | None:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    matches = sorted((config / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _codex_rollout_of_ancestor() -> Path | None:
    """Rollout file held open by the nearest ancestor process (a running Codex)."""
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return None
        rollouts = []
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if ROLLOUT_RE.search(target):
                    rollouts.append(Path(target))
        except OSError:
            pass  # another user's process, or it already exited
        existing = [r for r in rollouts if r.exists()]
        if existing:
            return max(existing, key=lambda r: r.stat().st_mtime)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            return None
        pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
    return None


def detect_host() -> dict | None:
    """{"kind", "session_id", "transcript"} of the calling agent session, or None."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        transcript = _claude_transcript(session_id)
        if transcript is not None:
            return {"kind": "claude", "session_id": session_id, "transcript": str(transcript)}
    rollout = _codex_rollout_of_ancestor()
    if rollout is not None:
        return {"kind": "codex", "session_id": ROLLOUT_RE.search(str(rollout)).group(1), "transcript": str(rollout)}
    return None


def _epoch(timestamp: str) -> float | None:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _claude_calls(path: Path, start: float, end: float):
    calls: dict[str, tuple[float, dict]] = {}
    with open(path, errors="replace") as transcript:
        for line in transcript:
            if '"assistant"' not in line or '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            ts = _epoch(entry.get("timestamp"))
            if entry.get("type") != "assistant" or message.get("model") == "<synthetic>" or ts is None:
                continue
            if start <= ts <= end and isinstance(message.get("usage"), dict):
                # A message is written once per content block; the last copy is the most complete.
                calls[message.get("id") or str(ts)] = (ts, message["usage"])
    for _, usage in calls.values():
        yield CallUsage(
            input_uncached=usage.get("input_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            output=usage.get("output_tokens", 0),
        )


def _codex_calls(path: Path, start: float, end: float):
    from .workers.codex import _call_usage

    seen_total = 0
    with open(path, errors="replace") as rollout:
        for line in rollout:
            if '"token_count"' not in line:
                continue
            try:
                record = json.loads(line)
                info = record["payload"]["info"]
                total = info["total_token_usage"]["total_tokens"]
            except (ValueError, KeyError, TypeError):
                continue
            if total <= seen_total:
                continue  # repeated record, or the empty one written after a compaction
            seen_total = total
            ts = _epoch(record.get("timestamp"))
            if ts is not None and start <= ts <= end:
                yield _call_usage(info["last_token_usage"])


def host_usage(host: dict, start: float, end: float) -> dict | None:
    """Host token usage between two Unix times, or None if its transcript is gone."""
    path = Path(host["transcript"])
    if not path.exists():
        return None
    reader = _claude_calls if host["kind"] == "claude" else _codex_calls
    totals = Totals()
    for call in reader(path, start, end):
        totals.add(call)
    return totals.to_dict()
