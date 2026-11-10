"""Compact text views of tasks, shared by the CLI and the MCP server.

Output is deliberately short: the reader is often another agent, and everything
returned lands in its context window.
"""

from __future__ import annotations

import json
import time

from .host import host_usage
from .store import TaskPaths, list_tasks, read_json
from .tasks import load_state
from .workers import ADAPTERS


def tokens(n) -> str:
    if n is None:
        return "n/a"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def host_usage_for(spec: dict, state: dict) -> dict | None:
    """Token usage of the host session while the task ran (until now if still running)."""
    host = spec.get("host")
    if not host or state.get("started_ts") is None:
        return None
    return host_usage(host, spec["created_ts"], state.get("ended_ts") or time.time())


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
    if spec.get("resume_of"):
        out.append(f"  follow-up of task {spec['resume_of']} (resumed session {spec['resume_session']})")

    started = state.get("started_ts")
    if started is not None:
        timing = f"  elapsed {duration((state.get('ended_ts') or now) - started)}"
        if state["status"] == "running" and state.get("last_event_ts"):
            timing += f", last event {duration(now - state['last_event_ts'])} ago"
        out.append(timing)

    usage = state.get("usage")
    if usage:
        out.append(
            f"  tokens: total {tokens(usage['total_tokens'])} / limit {tokens(limits['max_total_tokens'])}"
            f" (uncached in {tokens(usage['input_uncached'])}, cache read {tokens(usage['cache_read'])},"
            f" cache write {tokens(usage['cache_write'])}, out {tokens(usage['output'])})"
        )
        out.append(
            f"  context: last {tokens(usage['last_context'])}, peak {tokens(usage['max_context'])}"
            f" / limit {tokens(limits['max_context_tokens'])}; api calls {usage['api_calls']}"
            f" / limit {limits['max_api_calls'] if limits['max_api_calls'] is not None else 'n/a'}"
        )
        if state.get("round", 1) > 1:
            trail = ", ".join(str(r["outcome"]) for r in state.get("rounds") or [])
            out.append(f"  round {state['round']} of at most {spec.get('max_rounds', 1)} (finished rounds: {trail or 'none'})")
        if state.get("compactions"):
            out.append(f"  context compactions: {state['compactions']}")
        if usage.get("cost_usd") is not None:
            out.append(f"  cost: ${usage['cost_usd']:.4f} (list price; nominal under a subscription)")
    host = spec.get("host")
    if host:
        label = f"  host: {host['kind']} session {host['session_id'][:8]}"
        host_use = host_usage_for(spec, state)
        if host_use is None:
            out.append(f"{label} (its transcript is not available)")
        else:
            out.append(
                f"{label}: {host_use['api_calls']} model calls, {tokens(host_use['total_tokens'])} tokens "
                f"(cache read {tokens(host_use['cache_read'])}) while the task ran"
            )
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
        out.append("  the supervisor process died; kill the task to stop the worker and close it")
    denials = state.get("permission_denials") or []
    if denials:
        out.append(f"  permission denials: {len(denials)} (first: {denials[0]})")
    if state.get("unparsed_lines"):
        out.append(f"  unparsed output lines: {state['unparsed_lines']}")
    for message in (state.get("warnings") or []) + (state.get("notes") or []):
        out.append(f"  note: {message}")
    return "\n".join(out)


def task_rows(limit: int) -> list[dict]:
    rows = []
    for paths in list_tasks()[-limit:]:
        state = load_state(paths)
        spec = read_json(paths.spec)
        usage = state.get("usage") or {}
        rows.append({
            "id": spec["id"], "status": state["status"], "outcome": state.get("outcome"),
            "worker": spec["worker"], "name": spec.get("name"),
            "total_tokens": usage.get("total_tokens"), "max_context": usage.get("max_context"),
            "cost_usd": usage.get("cost_usd"), "cwd": spec["cwd"],
        })
    return rows


def format_rows(rows: list[dict]) -> str:
    if not rows:
        return "no tasks"
    lines = []
    for row in rows:
        cost = f" ${row['cost_usd']:.2f}" if row["cost_usd"] is not None else ""
        lines.append(
            f"{row['id']} {row['status']:<9} {row['outcome'] or '-':<9} {row['worker']:<6} tokens {tokens(row['total_tokens'])}"
            f" peak ctx {tokens(row['max_context'])}{cost}  {row['name'] or row['cwd']}"
        )
    running = sum(row["status"] in ("queued", "running") for row in rows)
    worker_tokens = sum(row["total_tokens"] or 0 for row in rows)
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    footer = f"{len(rows)} tasks shown, {running} running; worker tokens {tokens(worker_tokens)}"
    if costs:
        footer += f"; claude cost ${sum(costs):.2f}"
    return "\n".join(lines + [footer])


def log_renderer(paths: TaskPaths):
    """Return a function turning raw events.jsonl lines into readable activity lines."""
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

    return render


def tail_log(paths: TaskPaths, lines: int) -> str:
    if not paths.events.exists():
        return "no events yet"
    with open(paths.events) as events:
        rendered = log_renderer(paths)(events.readlines())
    return "\n".join(rendered[-lines:]) if rendered else "no activity yet"
