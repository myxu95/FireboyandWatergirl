"""Codex as worker: `codex exec --json`.

The stdout stream only reports usage once, when the turn ends. Live per-call usage
comes from the session rollout file Codex writes under $CODEX_HOME/sessions.
Formats verified against codex-cli 0.167.0.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..usage import CallUsage
from .base import RESULT_SCHEMA_PATH, Tracker, WorkerAdapter, clip

ROLLOUT_GRACE_S = 30


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def _call_usage(usage: dict) -> CallUsage:
    # OpenAI convention: input_tokens is the whole prompt and already includes the
    # cached and cache-write parts.
    cached = usage["cached_input_tokens"]
    cache_write = usage.get("cache_write_input_tokens", 0)
    return CallUsage(
        input_uncached=max(usage["input_tokens"] - cached - cache_write, 0),
        cache_read=cached,
        cache_write=cache_write,
        output=usage["output_tokens"],
    )


class CodexAdapter(WorkerAdapter):
    name = "codex"
    bin_env = "FWB_CODEX_BIN"
    default_bin = "codex"

    def __init__(self, spec: dict | None = None):
        super().__init__(spec)
        self._thread_seen_at: float | None = None
        self._rollout: Path | None = None
        self._offset = 0  # bytes of the rollout file already consumed
        self._seen_total = 0  # cumulative total_tokens of the last recorded call
        self._live_calls = 0
        self._live_tokens = 0  # tokens of the calls recorded by this process
        self._turn_usage: dict | None = None
        # Session-cumulative usage before this process, for resumed sessions.
        self._baseline: CallUsage | None = None

    def prepare(self, tracker: Tracker) -> None:
        session = self.spec.get("resume_session")
        if not session:
            return
        # A resumed session appends to its rollout file and keeps counting from its old totals.
        self._rollout = self._find_rollout(session)
        if self._rollout is None:
            tracker.note("rollout of the resumed session not found; totals include the session's earlier usage")
            return
        data = self._rollout.read_bytes()
        self._offset = data.rfind(b"\n") + 1
        for line in data[: self._offset].split(b"\n"):
            if b"token_count" not in line:
                continue
            try:
                payload = json.loads(line)["payload"]
                if payload.get("type") == "token_count" and payload.get("info"):
                    total = payload["info"]["total_token_usage"]
                    self._seen_total = total["total_tokens"]
                    self._baseline = _call_usage(total)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue

    def build_command(self, executable: str, budget_usd: float | None = None) -> list[str]:
        spec = self.spec
        sandbox = spec.get("sandbox") or "workspace-write"
        if spec.get("resume_session"):
            # `exec resume` takes neither -C nor -s; the supervisor sets the cwd.
            cmd = [executable, "exec", "resume", "--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd = [executable, "exec", "--json", "--skip-git-repo-check", "-C", spec["cwd"], "-s", sandbox]
        if spec.get("model"):
            cmd += ["-m", spec["model"]]
        if spec.get("autocompact_tokens") is not None:
            cmd += ["-c", f"model_auto_compact_token_limit={spec['autocompact_tokens']}"]
        if spec.get("structured"):
            cmd += ["--output-schema", str(RESULT_SCHEMA_PATH)]
        cmd += spec.get("worker_args") or []
        if spec.get("resume_session"):
            cmd.append(spec["resume_session"])
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def on_event(self, event: dict, tracker: Tracker) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            tracker.session_id = event.get("thread_id")
            self._thread_seen_at = time.time()
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                tracker.result_text = item["text"]  # the last message is the final answer
        elif kind == "turn.completed":
            tracker.outcome = "success"
            self._turn_usage = event.get("usage")
        elif kind in ("turn.failed", "error"):
            tracker.outcome = "error"
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else event.get("message")
            tracker.error = clip(message or json.dumps(event, ensure_ascii=False), 500)
        lines = self.describe(event)
        if lines:
            tracker.last_activity = lines[-1]

    def poll(self, tracker: Tracker) -> bool:
        if tracker.session_id is None:
            return False
        if self._rollout is None:
            self._rollout = self._find_rollout(tracker.session_id)
            if self._rollout is None:
                if time.time() - (self._thread_seen_at or 0) > ROLLOUT_GRACE_S:
                    tracker.note("codex rollout file not found; live token usage is unavailable and the context limit is not enforced")
                return False
        with open(self._rollout, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return False
        self._offset += end + 1
        for line in chunk[:end].split(b"\n"):
            self._on_rollout_line(line, tracker)
        return True

    @staticmethod
    def _find_rollout(thread_id: str) -> Path | None:
        matches = list((codex_home() / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
        return matches[0] if matches else None

    def _on_rollout_line(self, line: bytes, tracker: Tracker) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            return
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            tracker.model = payload.get("model") or tracker.model
        if record.get("type") == "compacted":
            tracker.compactions += 1
            tracker.last_activity = "compacted context"
        if record.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
            return
        try:
            info = payload.get("info")
            if info and info["total_token_usage"]["total_tokens"] > self._seen_total:
                self._seen_total = info["total_token_usage"]["total_tokens"]
                call = _call_usage(info["last_token_usage"])
                tracker.record_call(call, "codex.rollout")
                self._live_calls += 1
                self._live_tokens += call.context + call.output
            primary = (payload.get("rate_limits") or {}).get("primary")
            if primary:
                tracker.rate_limit = {
                    "used_percent": primary["used_percent"],
                    "window_minutes": primary["window_minutes"],
                    "resets_at": primary.get("resets_at"),  # Unix epoch seconds
                }
        except (KeyError, TypeError) as exc:
            tracker.note(f"unrecognized codex token_count record ({exc!r}); live token usage may be incomplete")

    def finalize(self, tracker: Tracker) -> None:
        if self.spec.get("structured") and tracker.result_text is not None:
            # With --output-schema the final message is the JSON document itself.
            try:
                parsed = json.loads(tracker.result_text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                tracker.structured = parsed
        if self._turn_usage is None:
            return
        try:
            turn = _call_usage(self._turn_usage)
        except (KeyError, TypeError):
            tracker.note("unrecognized usage in codex turn.completed event")
            return
        if self._baseline is not None:
            # turn.completed of a resumed session reports the session's cumulative usage.
            base = self._baseline
            turn = CallUsage(
                input_uncached=max(turn.input_uncached - base.input_uncached, 0),
                cache_read=max(turn.cache_read - base.cache_read, 0),
                cache_write=max(turn.cache_write - base.cache_write, 0),
                output=max(turn.output - base.output, 0),
            )
        totals = tracker.totals
        if self._live_calls == 0:
            # Only the per-turn aggregate is known: per-call context stays unobserved.
            totals.input_uncached += turn.input_uncached
            totals.cache_read += turn.cache_read
            totals.cache_write += turn.cache_write
            totals.output += turn.output
            tracker.note("token totals come from the end-of-turn aggregate; API call count and context size were not observed")
        elif turn.context + turn.output != self._live_tokens:
            tracker.note(f"live token total {self._live_tokens} differs from end-of-turn total {turn.context + turn.output}")

    def describe(self, event: dict) -> list[str]:
        kind = event.get("type")
        if kind == "thread.started":
            return [f"session {event.get('thread_id')}"]
        if kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message":
                return ["assistant: " + clip(item.get("text", ""))] if kind == "item.completed" else []
            if item_type == "command_execution":
                if kind == "item.started":
                    return ["tool shell: " + clip(item.get("command", ""))]
                exit_code = item.get("exit_code")
                return [f"tool error: shell exited {exit_code}"] if exit_code else []
            if kind != "item.completed" or item_type == "reasoning":
                return []
            return [f"tool error: {item_type} failed"] if item.get("status") == "failed" else [f"item {item_type}"]
        if kind == "turn.completed":
            return ["turn completed"]
        if kind in ("turn.failed", "error"):
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else event.get("message")
            return ["error: " + clip(message or "")]
        return []
