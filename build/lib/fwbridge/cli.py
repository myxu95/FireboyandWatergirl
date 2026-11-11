"""fwb: delegate a task to Claude Code or Codex and watch what it costs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, report, tasks
from .limits import Limits
from .report import format_status
from .store import TERMINAL_STATUSES, TaskError, resolve_task
from .workers import ADAPTERS

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_COMPLETED = 3  # the worker finished but reported partial / blocked / failed
EXIT_STILL_RUNNING = 124


def _limit_arg(kind):
    def parse(text: str):
        value = kind(text)
        if value < 0:
            raise argparse.ArgumentTypeError("must be >= 0 (0 disables the limit)")
        return value
    return parse


def _exit_code(state: dict) -> int:
    if state["status"] == "succeeded":
        return 0 if state.get("outcome") in (None, "completed") else EXIT_NOT_COMPLETED
    if state["status"] in ("queued", "running"):
        return EXIT_STILL_RUNNING
    return EXIT_FAILED


def cmd_run(args) -> int:
    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file is not None:
        with open(args.prompt_file) as f:
            prompt = f.read()
    else:
        prompt = sys.stdin.read()

    limits = Limits.from_overrides({field: getattr(args, field) for field in Limits().to_dict()})
    paths = tasks.create_task(
        worker=args.worker, prompt=prompt, cwd=args.cwd, limits=limits, name=args.name,
        model=args.model, permission_mode=args.permission_mode, allowed_tools=args.allowed_tools,
        sandbox=args.sandbox, worker_args=args.worker_arg, allow_concurrent=args.allow_concurrent,
        autocompact_tokens=tasks.AUTOCOMPACT_DEFAULT if args.autocompact is None else (args.autocompact or None),
        structured=not args.raw_result, max_rounds=args.max_rounds, resume_task=args.resume,
        notify_command=args.notify if args.notify is not None else os.environ.get("FWB_NOTIFY_CMD"),
    )
    tasks.spawn_supervisor(paths)
    if args.wait is None:
        print(f"started task {paths.task_id}")
        print(f"  check: fwb status {paths.task_id}   block: fwb wait {paths.task_id} --timeout 600")
        return 0
    state = tasks.wait_task(paths, args.wait or None)
    print(format_status(paths, state))
    return _exit_code(state)


def cmd_status(args) -> int:
    if args.task:
        paths = resolve_task(args.task)
        state = tasks.load_state(paths)
        if args.json:
            from .store import read_json
            state["host_usage"] = report.host_usage_for(read_json(paths.spec), state)
            print(json.dumps(state, ensure_ascii=False, indent=1))
        else:
            print(format_status(paths, state))
        return 0
    rows = report.task_rows(args.limit)
    print(json.dumps(rows, ensure_ascii=False, indent=1) if args.json else report.format_rows(rows))
    return 0


def cmd_wait(args) -> int:
    paths = resolve_task(args.task)
    state = tasks.wait_task(paths, args.timeout or None)
    print(format_status(paths, state))
    return _exit_code(state)


def cmd_log(args) -> int:
    paths = resolve_task(args.task)
    render = report.log_renderer(paths)
    if not paths.events.exists():
        print("no events yet")
        return 0
    with open(paths.events) as events:
        for line in render(events.readlines())[-args.lines:]:
            print(line)
        while args.follow:
            chunk = events.readlines()
            for line in render(chunk):
                print(line, flush=True)
            if not chunk:
                if tasks.load_state(paths)["status"] not in ("queued", "running"):
                    break
                time.sleep(1.0)
    return 0


def cmd_result(args) -> int:
    paths = resolve_task(args.task)
    state = tasks.load_state(paths)
    source = paths.result_json if args.json else paths.result
    if not source.exists():
        kind = "structured result" if args.json else "result"
        raise TaskError(f"task {paths.task_id} has no {kind} (status: {state['status']})")
    if state["status"] != "succeeded":
        print(f"[fwb] task status is {state['status']}; this result may be partial", file=sys.stderr)
    sys.stdout.write(source.read_text().rstrip("\n") + "\n")
    return 0


def cmd_kill(args) -> int:
    paths = resolve_task(args.task)
    state = tasks.kill_task(paths)
    print(format_status(paths, state))
    return 0 if state["status"] in TERMINAL_STATUSES else EXIT_FAILED


def cmd_mcp(args) -> int:
    from .mcp_server import serve
    serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fwb", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--version", action="version", version=f"fwb {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a task in the background")
    run.add_argument("--worker", "-w", choices=sorted(ADAPTERS),
                     help="agent that executes the task (required unless --resume is given)")
    run.add_argument("--cwd", help="working directory for the worker (default: current, or that of the --resume task)")
    run.add_argument("--resume", metavar="TASK",
                     help="send a follow-up to the worker session of a finished task; worker and cwd default to that task's")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--prompt", "-p", help="task prompt (default: read from stdin)")
    source.add_argument("--prompt-file", help="read the prompt from this file")
    run.add_argument("--name", help="short label shown in listings")
    run.add_argument("--model", help="model override passed to the worker")
    run.add_argument("--wait", type=float, nargs="?", const=0, metavar="SECONDS",
                     help="block until the task ends (optionally at most SECONDS) and print its status")
    run.add_argument("--allow-concurrent", action="store_true", help="allow another running task in the same cwd")
    run.add_argument("--worker-arg", action="append", metavar="ARG",
                     help="extra argument for the worker CLI (repeatable); write --worker-arg=-c for values starting with '-'")
    run.add_argument("--autocompact", type=_limit_arg(int), metavar="TOKENS",
                     help="context size at which the worker summarizes its own history "
                          f"(default: {tasks.DEFAULT_AUTOCOMPACT_TOKENS} when below --max-context-tokens; "
                          "0 leaves the worker's own default; claude accepts 100000-1000000)")
    run.add_argument("--max-rounds", type=int, default=1, metavar="N",
                     help="when the worker reports a partial outcome, continue in a fresh session seeded with "
                          "its report, up to N worker sessions in total (default: 1, no continuation); "
                          "limits apply to all rounds together")
    run.add_argument("--notify", metavar="COMMAND",
                     help="shell command run when the task ends or first nears a limit, with FWB_EVENT, FWB_MESSAGE, "
                          "FWB_TASK_ID, FWB_STATUS and FWB_OUTCOME set (default: $FWB_NOTIFY_CMD; '' disables)")
    run.add_argument("--raw-result", action="store_true",
                     help="do not ask the worker for a structured result (outcome, summary, files, verification)")

    claude = run.add_argument_group("claude worker")
    claude.add_argument("--permission-mode", help="Claude Code permission mode (default: acceptEdits)")
    claude.add_argument("--allowed-tools", nargs="+", metavar="TOOL", help='pre-approved tools, e.g. "Bash(pytest:*)"')
    codex = run.add_argument_group("codex worker")
    codex.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"],
                       help="Codex sandbox mode (default: workspace-write)")

    defaults = Limits()
    group = run.add_argument_group("limits (0 disables a limit)")
    group.add_argument("--max-cost-usd", dest="max_cost_usd", type=_limit_arg(float),
                       help="stop above this cost in USD (claude only; default: no limit)")
    group.add_argument("--max-total-tokens", dest="max_total_tokens", type=_limit_arg(int),
                       help=f"stop above this many tokens summed over all calls (default: {defaults.max_total_tokens})")
    group.add_argument("--max-context-tokens", dest="max_context_tokens", type=_limit_arg(int),
                       help=f"stop when one call's prompt exceeds this (default: {defaults.max_context_tokens})")
    group.add_argument("--max-api-calls", dest="max_api_calls", type=_limit_arg(int),
                       help=f"stop after this many model calls (default: {defaults.max_api_calls})")
    group.add_argument("--stall-timeout", dest="stall_timeout_s", type=_limit_arg(float),
                       help=f"stop after this many seconds without any event (default: {defaults.stall_timeout_s})")
    group.add_argument("--wall-timeout", dest="wall_timeout_s", type=_limit_arg(float),
                       help=f"stop after this many seconds in total (default: {defaults.wall_timeout_s})")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show one task, or list recent tasks")
    status.add_argument("task", nargs="?", help="task id, unique prefix, or 'last'")
    status.add_argument("--json", action="store_true")
    status.add_argument("--limit", type=int, default=15, help="how many recent tasks to list (default: 15)")
    status.set_defaults(func=cmd_status)

    wait = sub.add_parser("wait", help="block until a task ends; exit 0 completed, 3 finished but not completed, "
                                       "1 failed or killed, 124 still running")
    wait.add_argument("task")
    wait.add_argument("--timeout", type=float, default=0, help="give up waiting after this many seconds (0: no timeout)")
    wait.set_defaults(func=cmd_wait)

    log = sub.add_parser("log", help="show what the worker has been doing")
    log.add_argument("task")
    log.add_argument("-n", "--lines", type=int, default=20)
    log.add_argument("-f", "--follow", action="store_true")
    log.set_defaults(func=cmd_log)

    result = sub.add_parser("result", help="print the worker's final answer")
    result.add_argument("task")
    result.add_argument("--json", action="store_true", help="print the structured result as JSON")
    result.set_defaults(func=cmd_result)

    kill = sub.add_parser("kill", help="stop a running task")
    kill.add_argument("task")
    kill.set_defaults(func=cmd_kill)

    mcp = sub.add_parser("mcp", help="serve the task tools over MCP (stdio), for agents that cannot run fwb in a shell")
    mcp.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskError as exc:
        print(f"fwb: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
