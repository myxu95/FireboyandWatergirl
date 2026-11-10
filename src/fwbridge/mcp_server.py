"""MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing fwb's task tools.

An agent whose shell is sandboxed cannot start a networked worker or write the task
store; MCP servers run outside that sandbox. Start with `fwb mcp`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

from . import __version__, report, tasks
from .limits import Limits
from .store import TaskError, resolve_task
from .workers import ADAPTERS

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
# Longest single task_wait call (seconds). It must stay below the client's tool
# timeout: Codex defaults to 300 s, raised per server with tool_timeout_sec.
WAIT_MAX_S = float(os.environ.get("FWB_MCP_WAIT_MAX_S", "280"))

# Codex runs MCP calls inside a code cell that hands control back to the model after
# 30 s by default; every hand-back is a full model call. The cell's first-line pragma
# keeps it blocked for the whole wait instead.
CODEX_WAIT_HINT = (
    "In Codex code mode, put task_wait alone in an exec cell whose first line is exactly "
    '`// @exec: {{"yield_time_ms": {ms}}}`; a shorter yield wakes you before the wait ends, '
    "and each wake-up costs a full model call."
)

INSTRUCTIONS = """\
fwb runs a task on another coding agent (Claude Code or Codex) in the background, with
token and time limits. Typical use: delegate -> task_wait (repeat while it says the task
is still running) -> task_result. Every status check or wake-up costs you a model call,
so wait with long task_wait calls instead of polling task_status. Worker reports are the
worker's own claims: check important ones yourself.
""" + CODEX_WAIT_HINT.format(ms=int(WAIT_MAX_S * 1000) + 15000)

LIMIT_PROPERTIES = {
    "max_context_tokens": {"type": "integer", "minimum": 0, "description": "stop when one model call's prompt exceeds this many tokens"},
    "max_total_tokens": {"type": "integer", "minimum": 0, "description": "stop when tokens summed over all calls exceed this"},
    "max_api_calls": {"type": "integer", "minimum": 0, "description": "stop after this many model calls"},
    "max_cost_usd": {"type": "number", "minimum": 0, "description": "stop above this cost in USD (claude worker only)"},
    "stall_timeout_s": {"type": "number", "minimum": 0, "description": "stop after this many seconds without worker activity"},
    "wall_timeout_s": {"type": "number", "minimum": 0, "description": "stop after this many seconds in total"},
}

TASK_ARG = {"type": "string", "description": "task id, a unique prefix or suffix of it, or 'last'"}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "delegate",
        # Starts another agent that can edit files and spend tokens.
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
        "description": (
            "Start a task on another coding agent in the background and return its task id at once. "
            "The worker gets the prompt plus fixed rules (work autonomously, do not delegate further, "
            "report honestly) and ends with a structured report: outcome (completed/partial/blocked/failed), "
            "summary, files_changed, verification, remaining. Omitted limits keep their defaults; 0 disables one. "
            "Pass `resume` with an earlier task id to send a follow-up into that task's worker session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "the task, written for an agent that knows nothing of your conversation"},
                "worker": {"type": "string", "enum": sorted(ADAPTERS), "description": "agent that executes the task (not needed with resume)"},
                "cwd": {"type": "string", "description": "absolute path of the working directory (not needed with resume)"},
                "resume": {"type": "string", "description": "id of a finished task whose worker session should receive this prompt as a follow-up"},
                "name": {"type": "string", "description": "short label shown in listings"},
                "model": {"type": "string", "description": "model override for the worker"},
                "max_rounds": {"type": "integer", "minimum": 1, "description": "if the worker reports 'partial', continue in a fresh session seeded with its report, up to this many sessions (default 1)"},
                "autocompact_tokens": {"type": "integer", "minimum": 0, "description": "context size at which the worker compacts its history (default 120000 when below max_context_tokens; 0 keeps the worker's own default)"},
                "limits": {"type": "object", "properties": LIMIT_PROPERTIES, "additionalProperties": False},
                "permission_mode": {"type": "string", "description": "claude worker: permission mode (default acceptEdits)"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": 'claude worker: pre-approved tools, e.g. "Bash(pytest:*)"'},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"], "description": "codex worker: sandbox mode (default workspace-write)"},
                "allow_concurrent": {"type": "boolean", "description": "allow another running task in the same directory"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_wait",
        "annotations": READ_ONLY,
        "description": (
            f"Block until the task ends or {WAIT_MAX_S:.0f} s pass, then return its status; if it is still running, call again. "
            "For an immediate look without waiting, use task_status instead. "
            + CODEX_WAIT_HINT.format(ms=int(WAIT_MAX_S * 1000) + 15000)
        ),
        "inputSchema": {
            "type": "object",
            # No timeout argument: agents tend to pick short waits, and each one is a model call.
            "properties": {"task": TASK_ARG},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_status",
        "annotations": READ_ONLY,
        "description": "Status of one task (tokens, context size, limits, last activity), or a list of recent tasks when no task is given.",
        "inputSchema": {"type": "object", "properties": {"task": TASK_ARG}, "additionalProperties": False},
    },
    {
        "name": "task_result",
        "annotations": READ_ONLY,
        "description": "The worker's final report (outcome, summary, files changed, verification, remaining).",
        "inputSchema": {"type": "object", "properties": {"task": TASK_ARG}, "required": ["task"], "additionalProperties": False},
    },
    {
        "name": "task_log",
        "annotations": READ_ONLY,
        "description": "The worker's recent activity: tool calls, messages, context compactions.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": TASK_ARG, "lines": {"type": "integer", "minimum": 1, "description": "default 20"}},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_kill",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        "description": "Stop a running task.",
        "inputSchema": {"type": "object", "properties": {"task": TASK_ARG}, "required": ["task"], "additionalProperties": False},
    },
]


def _status_text(paths, state) -> str:
    text = report.format_status(paths, state)
    if state["status"] in ("queued", "running"):
        text += "\n(still running: call task_wait again)"
    elif state["status"] == "succeeded":
        text += "\n(finished: read the report with task_result)"
    return text


def _delegate(args: dict) -> str:
    cwd = args.get("cwd")
    if cwd is not None and not Path(cwd).is_absolute():
        raise TaskError("cwd must be an absolute path")
    if args.get("resume") is None and cwd is None:
        raise TaskError("cwd is required unless resume is given")
    try:
        limits = Limits.from_overrides(args.get("limits") or {})
    except ValueError as exc:
        raise TaskError(str(exc)) from None
    autocompact = args.get("autocompact_tokens")
    paths = tasks.create_task(
        worker=args.get("worker"), prompt=args.get("prompt") or "", cwd=cwd, limits=limits,
        name=args.get("name"), model=args.get("model"), permission_mode=args.get("permission_mode"),
        allowed_tools=args.get("allowed_tools"), sandbox=args.get("sandbox"),
        allow_concurrent=bool(args.get("allow_concurrent")),
        autocompact_tokens=tasks.AUTOCOMPACT_DEFAULT if autocompact is None else (autocompact or None),
        max_rounds=args.get("max_rounds") or 1, resume_task=args.get("resume"),
    )
    tasks.spawn_supervisor(paths)
    hint = CODEX_WAIT_HINT.format(ms=int(WAIT_MAX_S * 1000) + 15000)
    return f"started task {paths.task_id}. Next: task_wait with task={paths.task_id}. {hint}"


def call_tool(name: str, args: dict) -> str:
    if name == "delegate":
        return _delegate(args)
    if name == "task_status" and not args.get("task"):
        return report.format_rows(report.task_rows(10))
    if not args.get("task"):
        raise TaskError(f"{name} needs a task id")
    paths = resolve_task(args["task"])
    if name == "task_wait":
        return _status_text(paths, tasks.wait_task(paths, WAIT_MAX_S))
    if name == "task_status":
        return _status_text(paths, tasks.load_state(paths))
    if name == "task_result":
        state = tasks.load_state(paths)
        if not paths.result.exists():
            raise TaskError(f"task {paths.task_id} has no result (status: {state['status']})")
        prefix = "" if state["status"] == "succeeded" else f"[task status is {state['status']}; this report may be partial]\n"
        return prefix + paths.result.read_text()
    if name == "task_log":
        return report.tail_log(paths, int(args.get("lines") or 20))
    if name == "task_kill":
        return report.format_status(paths, tasks.kill_task(paths))
    raise TaskError(f"unknown tool '{name}'")


class Server:
    def __init__(self, stdout):
        self.stdout = stdout
        self.lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self.lock:
            self.stdout.write(json.dumps(message) + "\n")
            self.stdout.flush()

    def reply(self, msg_id, result=None, error=None) -> None:
        body = {"jsonrpc": "2.0", "id": msg_id}
        body.update({"error": error} if error is not None else {"result": result})
        self.send(body)

    def handle(self, message: dict) -> None:
        method, msg_id = message.get("method"), message.get("id")
        if method is None or msg_id is None:
            return  # a notification, or a response to a request we never send
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.reply(msg_id, {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fwb", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        elif method == "ping":
            self.reply(msg_id, {})
        elif method == "tools/list":
            self.reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            # Tool calls may block (task_wait); run them off the reader thread.
            threading.Thread(target=self._call, args=(msg_id, params), daemon=True).start()
        else:
            self.reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})

    def _call(self, msg_id, params: dict) -> None:
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text, is_error = call_tool(name, args), False
        except TaskError as exc:
            text, is_error = f"fwb: {exc}", True
        except Exception:
            text, is_error = "fwb internal error:\n" + traceback.format_exc(limit=5), True
        self.reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


def serve(stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(stdout)
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            server.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(message, dict):
            server.handle(message)
        else:
            server.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches are not supported"}})
