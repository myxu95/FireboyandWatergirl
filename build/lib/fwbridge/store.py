"""On-disk task store. Every task is one directory; there is no daemon."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "killed"})


class TaskError(Exception):
    """A user-facing error about a task (unknown id, invalid request, ...)."""


def home() -> Path:
    override = os.environ.get("FWB_HOME")
    return Path(override) if override else Path.home() / ".local" / "state" / "fwbridge"


def tasks_root() -> Path:
    return home() / "tasks"


class TaskPaths:
    def __init__(self, task_dir: Path | str):
        self.dir = Path(task_dir)
        self.spec = self.dir / "task.json"  # immutable request
        self.prompt = self.dir / "prompt.txt"
        self.state = self.dir / "state.json"  # written only by the supervisor once it starts
        self.supervisor_pid = self.dir / "supervisor.pid"
        self.supervisor_log = self.dir / "supervisor.log"
        self.events = self.dir / "events.jsonl"  # raw worker stdout
        self.stderr = self.dir / "stderr.log"
        self.usage = self.dir / "usage.jsonl"  # one line per model API call
        self.worker_input = self.dir / "worker_input.txt"  # preamble + prompt, as sent to the worker
        self.result = self.dir / "result.md"
        self.result_json = self.dir / "result.json"  # structured result, when the worker produced one
        self.cancel = self.dir / "cancel"

    @property
    def task_id(self) -> str:
        return self.dir.name


def new_task_dir() -> TaskPaths:
    root = tasks_root()
    root.mkdir(parents=True, exist_ok=True)
    task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    paths = TaskPaths(root / task_id)
    paths.dir.mkdir()
    return paths


def list_tasks() -> list[TaskPaths]:
    """All tasks, oldest first."""
    root = tasks_root()
    if not root.is_dir():
        return []
    # task.json is written once at creation, so its mtime orders tasks even
    # when two ids share the same second.
    specs = [d / "task.json" for d in root.iterdir() if (d / "task.json").is_file()]
    return [TaskPaths(spec.parent) for spec in sorted(specs, key=lambda s: (s.stat().st_mtime_ns, s.parent.name))]


def resolve_task(ref: str) -> TaskPaths:
    """Resolve a full id, a unique id prefix or suffix, or the alias 'last'."""
    tasks = list_tasks()
    if ref == "last":
        if not tasks:
            raise TaskError("no tasks exist yet")
        return tasks[-1]
    # Tasks started in the same second differ only in the random suffix.
    matches = [t for t in tasks if t.task_id.startswith(ref) or t.task_id.endswith(ref)]
    exact = [t for t in matches if t.task_id == ref]
    if exact:
        return exact[0]
    if not matches:
        raise TaskError(f"no task matches '{ref}'")
    if len(matches) > 1:
        ids = ", ".join(t.task_id for t in matches[:5])
        raise TaskError(f"'{ref}' is ambiguous: {ids}")
    return matches[0]


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def read_json(path: Path):
    return json.loads(path.read_text())


def proc_start_ticks(pid: int) -> int | None:
    """Start time of a live process in clock ticks since boot; None if gone or a zombie.

    Paired with the pid it identifies a process even after pid reuse.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        return None
    return int(fields[19])


def is_alive(pid: int | None, start_ticks: int | None) -> bool:
    if pid is None or start_ticks is None:
        return False
    return proc_start_ticks(pid) == start_ticks
