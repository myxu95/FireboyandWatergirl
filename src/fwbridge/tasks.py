"""Task lifecycle operations shared by every front end (CLI now, MCP later)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .limits import Limits
from .store import (
    TERMINAL_STATUSES, TaskError, TaskPaths, is_alive, list_tasks, new_task_dir,
    proc_start_ticks, read_json, write_json_atomic,
)
from .workers import ADAPTERS

DEFAULT_MAX_DEPTH = 1
# Context size (tokens) at which the worker is told to summarize its own history.
# This keeps long tasks alive; Limits.max_context_tokens stays the hard backstop.
DEFAULT_AUTOCOMPACT_TOKENS = 120_000
AUTOCOMPACT_DEFAULT = "default"


def _resolve_autocompact(requested, limits: Limits) -> int | None:
    hard = limits.max_context_tokens
    if requested == AUTOCOMPACT_DEFAULT:
        fits = hard is None or DEFAULT_AUTOCOMPACT_TOKENS < hard
        return DEFAULT_AUTOCOMPACT_TOKENS if fits else None
    if requested is not None and hard is not None and requested >= hard:
        raise TaskError(
            f"--autocompact {requested} must be below --max-context-tokens {hard}, "
            "otherwise the worker is stopped before it can compact"
        )
    return requested


def create_task(
    *,
    worker: str,
    prompt: str,
    cwd: str,
    limits: Limits,
    name: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    sandbox: str | None = None,
    worker_args: list[str] | None = None,
    allow_concurrent: bool = False,
    autocompact_tokens: int | str | None = AUTOCOMPACT_DEFAULT,
    structured: bool = True,
    max_rounds: int = 1,
) -> TaskPaths:
    """Register a task. autocompact_tokens: a token count, None to leave the worker's
    own default, or AUTOCOMPACT_DEFAULT."""
    if worker not in ADAPTERS:
        raise TaskError(f"unknown worker '{worker}'; choose from {sorted(ADAPTERS)}")
    adapter = ADAPTERS[worker]
    if not prompt.strip():
        raise TaskError("the prompt is empty")
    if max_rounds < 1:
        raise TaskError("--max-rounds must be at least 1")
    if max_rounds > 1 and not structured:
        raise TaskError("--max-rounds needs the structured result to detect a partial outcome; drop --raw-result")

    # A worker that delegates back would loop between the two agents and burn tokens.
    depth = int(os.environ.get("FWB_DEPTH", "0"))
    max_depth = int(os.environ.get("FWB_MAX_DEPTH", DEFAULT_MAX_DEPTH))
    if depth >= max_depth:
        raise TaskError(
            f"refusing to delegate from inside a delegated task (depth {depth}, max {max_depth}); "
            "set FWB_MAX_DEPTH to allow nesting"
        )

    if limits.max_cost_usd is not None and not adapter.supports_cost:
        raise TaskError(f"the {worker} worker does not report cost; use token limits instead of --max-cost-usd")

    workdir = Path(cwd).resolve()
    if not workdir.is_dir():
        raise TaskError(f"working directory does not exist: {workdir}")
    if not allow_concurrent:
        for other in list_tasks():
            if effective_status(other) in ("queued", "running") and read_json(other.spec)["cwd"] == str(workdir):
                raise TaskError(
                    f"task {other.task_id} is already running in {workdir}; "
                    "two agents editing one tree will conflict (pass --allow-concurrent to override)"
                )

    executable = shutil.which(os.environ.get(adapter.bin_env) or adapter.default_bin)
    if executable is None:
        raise TaskError(f"cannot find the {worker} executable (override with {adapter.bin_env})")

    spec = {
        "name": name,
        "worker": worker,
        "worker_bin": executable,
        "cwd": str(workdir),
        "model": model,
        "permission_mode": permission_mode,
        "allowed_tools": allowed_tools,
        "sandbox": sandbox,
        "worker_args": worker_args,
        "autocompact_tokens": _resolve_autocompact(autocompact_tokens, limits),
        "structured": structured,
        "max_rounds": max_rounds,
        "limits": limits.to_dict(),
        "depth": depth,
        "created_ts": time.time(),  # Unix epoch seconds
    }
    try:
        adapter.validate(spec)
    except ValueError as exc:
        raise TaskError(str(exc)) from None

    paths = new_task_dir()
    paths.prompt.write_text(prompt)
    write_json_atomic(paths.spec, {"id": paths.task_id, **spec})
    write_json_atomic(paths.state, {"id": paths.task_id, "worker": worker, "status": "queued"})
    return paths


def spawn_supervisor(paths: TaskPaths) -> int:
    with open(paths.supervisor_log, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fwbridge.supervisor", str(paths.dir)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    write_json_atomic(paths.supervisor_pid, {"pid": proc.pid, "start_ticks": proc_start_ticks(proc.pid)})
    return proc.pid


def _supervisor_alive(paths: TaskPaths) -> bool | None:
    """None while the supervisor has not been spawned yet."""
    if not paths.supervisor_pid.exists():
        return None
    record = read_json(paths.supervisor_pid)
    return is_alive(record["pid"], record["start_ticks"])


def load_state(paths: TaskPaths) -> dict:
    """state.json plus an 'orphaned' status when the supervisor died without finishing."""
    state = read_json(paths.state)
    if state["status"] not in TERMINAL_STATUSES and _supervisor_alive(paths) is False:
        # The supervisor may have finished between the two reads above.
        state = read_json(paths.state)
        if state["status"] not in TERMINAL_STATUSES:
            state["status"] = "orphaned"
    return state


def effective_status(paths: TaskPaths) -> str:
    return load_state(paths)["status"]


def wait_task(paths: TaskPaths, timeout_s: float | None) -> dict:
    """Block until the task leaves queued/running or the timeout passes; return the state."""
    deadline = None if timeout_s is None else time.time() + timeout_s
    while True:
        state = load_state(paths)
        if state["status"] not in ("queued", "running"):
            return state
        if deadline is not None and time.time() >= deadline:
            return state
        time.sleep(1.0)


def kill_task(paths: TaskPaths, wait_s: float = 30) -> dict:
    state = load_state(paths)
    if state["status"] in TERMINAL_STATUSES:
        return state
    if state["status"] == "orphaned":
        # Nothing supervises the worker any more: stop it directly and close the record.
        pid = state.get("worker_pid")
        if is_alive(pid, state.get("worker_start_ticks")):
            os.killpg(pid, signal.SIGKILL)
        state.update(status="killed", kill_reason="supervisor died; worker stopped by kill", ended_ts=time.time())
        write_json_atomic(paths.state, state)
        return state
    paths.cancel.touch()
    if paths.supervisor_pid.exists():
        record = read_json(paths.supervisor_pid)
        if is_alive(record["pid"], record["start_ticks"]):
            os.kill(record["pid"], signal.SIGTERM)
    return wait_task(paths, wait_s)
