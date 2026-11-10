"""fwb: delegate a task to Claude Code or Codex and watch what it costs.

Output is deliberately compact: the caller is often another agent, and everything
printed here lands in its context window.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, tasks
from .limits import Limits
from .store import TERMINAL_STATUSES, TaskError, TaskPaths, list_tasks, read_json, resolve_task
from .workers import ADAPTERS

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_COMPLETED = 3  # the worker finished but reported partial / blocked / failed
EXIT_STILL_RUNNING = 124


def _tokens(n) -> str:
    if n is None:
        return "n/a"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _limit_arg(kind):
    def parse(text: str):
        value = kind(text)
        if value < 0:
            raise argparse.ArgumentTypeError("must be >= 0 (0 disables the limit)")
        return value
    return parse


def format_status(paths: TaskPaths, state: dict) -> str:
    spec = read_json(paths.spec)
    limits = spec["limits"]
    now = time.time()
    title = f"task {spec['id']} [{state['status']}] worker={spec['worker']}"
    if state.get("outcome"):
        title += f" outcome={state['outcome']}"
    if spec.get("name"):
        title += f" name={spec['name']}"
    out = [title, f"  cwd: {spec['cwd']}"]

    started = state.get("started_ts")
    if started is not None:
        timing = f"  elapsed {_duration((state.get('ended_ts') or now) - started)}"
        if state["status"] == "running" and state.get("last_event_ts"):
            timing += f", last event {_duration(now - state['last_event_ts'])} ago"
        out.append(timing)

    usage = state.get("usage")
    if usage:
        out.append(
            f"  tokens: total {_tokens(usage['total_tokens'])} / limit {_tokens(limits['max_total_tokens'])}"
            f" (uncached in {_tokens(usage['input_uncached'])}, cache read {_tokens(usage['cache_read'])},"
            f" cache write {_tokens(usage['cache_write'])}, out {_tokens(usage['output'])})"
        )
        out.append(
            f"  context: last {_tokens(usage['last_context'])}, peak {_tokens(usage['max_context'])}"
            f" / limit {_tokens(limits['max_context_tokens'])}; api calls {usage['api_calls']}"
            f" / limit {limits['max_api_calls'] if limits['max_api_calls'] is not None else 'n/a'}"
        )
        if state.get("round", 1) > 1:
            trail = ", ".join(str(r["outcome"]) for r in state.get("rounds") or [])
            out.append(f"  round {state['round']} of at most {spec.get('max_rounds', 1)} (finished rounds: {trail or 'none'})")
        if state.get("compactions"):
            out.append(f"  context compactions: {state['compactions']}")
        if usage.get("cost_usd") is not None:
            out.append(f"  cost: ${usage['cost_usd']:.4f} (list price; nominal under a subscription)")
    rate = state.get("rate_limit")
    if rate:
        out.append(f"  codex plan window: {rate['used_percent']:.0f}% used ({rate['window_minutes'] // 60}h window)")
    if state.get("last_activity") and state["status"] == "running":
        out.append(f"  last: {state['last_activity']}")
    if state.get("kill_reason"):
        out.append(f"  killed: {state['kill_reason']}")
    if state.get("error"):
        out.append(f"  error: {state['error']}")
    if state["status"] == "orphaned":
        out.append("  the supervisor process died; run `fwb kill` to stop the worker and close the task")
    denials = state.get("permission_denials") or []
    if denials:
        out.append(f"  permission denials: {len(denials)} (first: {denials[0]})")
    if state.get("unparsed_lines"):
        out.append(f"  unparsed output lines: {state['unparsed_lines']}")
    for message in (state.get("warnings") or []) + (state.get("notes") or []):
        out.append(f"  note: {message}")
    return "\n".join(out)


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

    defaults = Limits()
    chosen = {}
    for field in defaults.to_dict():
        value = getattr(args, field)
        chosen[field] = getattr(defaults, field) if value is None else (value or None)
    paths = tasks.create_task(
        worker=args.worker, prompt=prompt, cwd=args.cwd, limits=Limits(**chosen), name=args.name,
        model=args.model, permission_mode=args.permission_mode, allowed_tools=args.allowed_tools,
        sandbox=args.sandbox, worker_args=args.worker_arg, allow_concurrent=args.allow_concurrent,
        autocompact_tokens=tasks.AUTOCOMPACT_DEFAULT if args.autocompact is None else (args.autocompact or None),
        structured=not args.raw_result, max_rounds=args.max_rounds,
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
        print(json.dumps(state, ensure_ascii=False, indent=1) if args.json else format_status(paths, state))
        return 0
    rows = []
    for paths in list_tasks()[-args.limit:]:
        state = tasks.load_state(paths)
        spec = read_json(paths.spec)
        usage = state.get("usage") or {}
        rows.append({
            "id": spec["id"], "status": state["status"], "outcome": state.get("outcome"),
            "worker": spec["worker"], "name": spec.get("name"),
            "total_tokens": usage.get("total_tokens"), "max_context": usage.get("max_context"),
            "cost_usd": usage.get("cost_usd"), "cwd": spec["cwd"],
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("no tasks")
    else:
        for row in rows:
            cost = f" ${row['cost_usd']:.2f}" if row["cost_usd"] is not None else ""
            print(
                f"{row['id']} {row['status']:<9} {row['outcome'] or '-':<9} {row['worker']:<6} tokens {_tokens(row['total_tokens'])}"
                f" peak ctx {_tokens(row['max_context'])}{cost}  {row['name'] or row['cwd']}"
            )
    return 0


def cmd_wait(args) -> int:
    paths = resolve_task(args.task)
    state = tasks.wait_task(paths, args.timeout or None)
    print(format_status(paths, state))
    return _exit_code(state)


def cmd_log(args) -> int:
    paths = resolve_task(args.task)
    spec = read_json(paths.spec)
    adapter = ADAPTERS[spec["worker"]](spec)

    def render(raw_lines) -> list[str]:
        rendered = []
        for raw in raw_lines:
            try:
                event = json.loads(raw)
            except ValueError:
                rendered.append("unparsed: " + raw.strip()[:200])
                continue
            if isinstance(event, dict):
                rendered.extend(adapter.describe(event))
        return rendered

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fwb", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--version", action="version", version=f"fwb {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a task in the background")
    run.add_argument("--worker", "-w", required=True, choices=sorted(ADAPTERS), help="agent that executes the task")
    run.add_argument("--cwd", default=".", help="working directory for the worker (default: current)")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--prompt", "-p", help="task prompt (default: read from stdin)")
    source.add_argument("--prompt-file", help="read the prompt from this file")
    run.add_argument("--name", help="short label shown in listings")
    run.add_argument("--model", help="model override passed to the worker")
    run.add_argument("--wait", type=float, nargs="?", const=0, metavar="SECONDS",
                     help="block until the task ends (optionally at most SECONDS) and print its status")
    run.add_argument("--allow-concurrent", action="store_true", help="allow another running task in the same cwd")
    run.add_argument("--worker-arg", action="append", metavar="ARG", help="extra argument for the worker CLI (repeatable)")
    run.add_argument("--autocompact", type=_limit_arg(int), metavar="TOKENS",
                     help="context size at which the worker summarizes its own history "
                          f"(default: {tasks.DEFAULT_AUTOCOMPACT_TOKENS} when below --max-context-tokens; "
                          "0 leaves the worker's own default; claude accepts 100000-1000000)")
    run.add_argument("--max-rounds", type=int, default=1, metavar="N",
                     help="when the worker reports a partial outcome, continue in a fresh session seeded with "
                          "its report, up to N worker sessions in total (default: 1, no continuation); "
                          "limits apply to all rounds together")
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
