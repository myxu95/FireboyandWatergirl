"""Shared pieces of the worker adapters."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..usage import CallUsage, Totals

RESULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "result_schema.json"


class Tracker:
    """What the supervisor knows about a running worker, filled in by its adapter."""

    def __init__(self, usage_log: Path | None = None):
        self.usage_log = usage_log
        self.totals = Totals()
        self.session_id: str | None = None
        self.model: str | None = None
        self.last_activity: str | None = None
        self.result_text: str | None = None
        self.structured: dict | None = None  # final answer following result_schema.json
        self.compactions = 0  # times the worker summarized its own history
        self.outcome: str | None = None  # "success" | "error" once the worker reports one
        self.error: str | None = None
        self.permission_denials: list[str] = []
        self.rate_limit: dict | None = None
        self.notes: list[str] = []
        self.cost_base = 0.0  # USD spent by earlier worker processes of this task

    def begin_round(self) -> None:
        """Reset per-process fields before another worker process starts; totals carry over."""
        self.cost_base = self.totals.cost_usd or 0.0
        self.session_id = self.result_text = self.structured = self.outcome = self.error = None

    def record_call(self, call: CallUsage, source: str) -> None:
        self.totals.add(call)
        if self.usage_log is not None:
            record = {
                "ts": time.time(),  # Unix epoch seconds
                "source": source,
                "input_uncached": call.input_uncached,
                "cache_read": call.cache_read,
                "cache_write": call.cache_write,
                "output": call.output,
                "context": call.context,
            }
            with open(self.usage_log, "a") as f:
                f.write(json.dumps(record) + "\n")

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


class WorkerAdapter:
    """Translates one agent CLI: how to launch it and how to read its event stream."""

    name: str
    bin_env: str  # environment variable that overrides the executable
    default_bin: str
    supports_cost = False

    def __init__(self, spec: dict | None = None):
        self.spec = spec or {}

    @classmethod
    def validate(cls, spec: dict) -> None:
        """Raise ValueError when the spec asks for something this worker cannot do."""

    @classmethod
    def session_baseline(cls, previous, session_id: str) -> dict | None:
        """Usage a resumed session has already accumulated, as reported in an earlier
        task's events: {input_uncached, cache_read, cache_write, output, cost_usd}.
        None when the worker does not report per-session totals or none was found."""
        return None

    def prepare(self, tracker: Tracker) -> None:
        """Called right before the worker process starts."""

    def build_command(self, executable: str, budget_usd: float | None = None) -> list[str]:
        """budget_usd: cost still available to this process, for workers that support it."""
        raise NotImplementedError

    def on_event(self, event: dict, tracker: Tracker) -> None:
        """Consume one parsed stdout event."""
        raise NotImplementedError

    def poll(self, tracker: Tracker) -> bool:
        """Check side channels; return True if the worker showed activity."""
        return False

    def finalize(self, tracker: Tracker) -> None:
        """Called once after the worker exited and the last poll ran."""

    def describe(self, event: dict) -> list[str]:
        """Human-readable lines for one stdout event (may be empty)."""
        raise NotImplementedError


def clip(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
