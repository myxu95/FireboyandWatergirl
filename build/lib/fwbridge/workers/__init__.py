from .base import Tracker, WorkerAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, type[WorkerAdapter]] = {"claude": ClaudeAdapter, "codex": CodexAdapter}

__all__ = ["ADAPTERS", "Tracker", "WorkerAdapter"]
