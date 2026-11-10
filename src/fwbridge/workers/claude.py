"""Claude Code as worker: `claude -p --output-format stream-json`.

Event format verified against Claude Code 2.1.322.
"""

from __future__ import annotations

import json

from ..usage import CallUsage
from .base import RESULT_SCHEMA_PATH, Tracker, WorkerAdapter, clip

# Range accepted by `claude --autocompact`.
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000


class ClaudeAdapter(WorkerAdapter):
    name = "claude"
    bin_env = "FWB_CLAUDE_BIN"
    default_bin = "claude"
    supports_cost = True

    def __init__(self, spec: dict | None = None):
        super().__init__(spec)
        # message id -> output tokens seen so far; one API message is streamed as
        # several events that repeat the same id and usage.
        self._output_by_message: dict[str, int] = {}
        # Tokens this process has added to the task totals: [uncached, cache read, cache write, output].
        self._streamed = [0, 0, 0, 0]

    @classmethod
    def validate(cls, spec: dict) -> None:
        window = spec.get("autocompact_tokens")
        if window is not None and not AUTOCOMPACT_MIN <= window <= AUTOCOMPACT_MAX:
            raise ValueError(f"claude accepts --autocompact between {AUTOCOMPACT_MIN} and {AUTOCOMPACT_MAX} tokens, got {window}")

    @classmethod
    def session_baseline(cls, previous, session_id: str) -> dict | None:
        # A resumed session reports usage and cost accumulated since its creation.
        baseline = None
        if not previous.events.exists():
            return None
        with open(previous.events) as events:
            for line in events:
                if '"result"' not in line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") != "result" or event.get("session_id") != session_id:
                        continue
                    models = list(event["modelUsage"].values())
                    baseline = {
                        "input_uncached": sum(m["inputTokens"] for m in models),
                        "cache_read": sum(m["cacheReadInputTokens"] for m in models),
                        "cache_write": sum(m["cacheCreationInputTokens"] for m in models),
                        "output": sum(m["outputTokens"] for m in models),
                        "cost_usd": event.get("total_cost_usd") or 0.0,
                    }
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        return baseline

    def prepare(self, tracker: Tracker) -> None:
        if self.spec.get("resume_session") and not self.spec.get("resume_baseline"):
            tracker.note("the resumed session's earlier usage is unknown, so the final totals include it")

    def build_command(self, executable: str, budget_usd: float | None = None) -> list[str]:
        spec = self.spec
        # The prompt arrives on stdin.
        cmd = [executable, "-p", "--output-format", "stream-json", "--verbose"]
        cmd += ["--permission-mode", spec.get("permission_mode") or "acceptEdits"]
        if spec.get("allowed_tools"):
            cmd += ["--allowedTools", *spec["allowed_tools"]]
        if spec.get("model"):
            cmd += ["--model", spec["model"]]
        if spec.get("resume_session"):
            cmd += ["--resume", spec["resume_session"]]
        if budget_usd is not None:
            cmd += ["--max-budget-usd", f"{budget_usd:.4f}"]
        if spec.get("autocompact_tokens") is not None:
            cmd += ["--autocompact", str(spec["autocompact_tokens"])]
        if spec.get("structured"):
            cmd += ["--json-schema", json.dumps(json.loads(RESULT_SCHEMA_PATH.read_text()))]
        cmd += spec.get("worker_args") or []
        return cmd

    def on_event(self, event: dict, tracker: Tracker) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            tracker.session_id = event.get("session_id")
            tracker.model = event.get("model")
        elif kind == "system" and event.get("subtype") == "compact_boundary":
            tracker.compactions += 1
            # The summarization call itself is not streamed; the result event accounts for it.
        elif kind == "assistant":
            self._on_assistant(event, tracker)
        elif kind == "result":
            self._on_result(event, tracker)
        lines = self.describe(event)
        if lines and kind != "result":
            tracker.last_activity = lines[-1]

    def _on_assistant(self, event: dict, tracker: Tracker) -> None:
        message = event.get("message") or {}
        if message.get("model") == "<synthetic>":
            return  # locally generated notice, not a model call
        usage = message.get("usage")
        message_id = message.get("id")
        if not isinstance(usage, dict) or message_id is None:
            tracker.note("assistant event without id/usage; live token totals may be low")
            return
        try:
            call = CallUsage(
                input_uncached=usage["input_tokens"],
                cache_read=usage["cache_read_input_tokens"],
                cache_write=usage["cache_creation_input_tokens"],
                output=usage["output_tokens"],
            )
        except KeyError as missing:
            tracker.note(f"assistant usage lacks {missing}; live token totals may be low")
            return
        seen_output = self._output_by_message.get(message_id)
        if seen_output is None:
            tracker.record_call(call, "claude.stream")
            for i, amount in enumerate((call.input_uncached, call.cache_read, call.cache_write, call.output)):
                self._streamed[i] += amount
        elif call.output > seen_output:
            tracker.totals.output += call.output - seen_output
            self._streamed[3] += call.output - seen_output
        self._output_by_message[message_id] = max(call.output, seen_output or 0)

    def _on_result(self, event: dict, tracker: Tracker) -> None:
        if event.get("is_error") or event.get("subtype") != "success":
            tracker.outcome = "error"
            detail = event.get("result") or event.get("terminal_reason") or ""
            # Some errors (e.g. compaction thrashing) arrive with subtype "success".
            subtype = event.get("subtype")
            tracker.error = clip(detail if subtype == "success" and detail else f"{subtype}: {detail}", 500)
            if "Not logged in" in str(detail):
                tracker.note("claude found no credentials: if they live in environment variables, the program that "
                             "started fwb may have filtered them out (e.g. Codex MCP servers need env_vars)")
        else:
            tracker.outcome = "success"
        if isinstance(event.get("result"), str):
            tracker.result_text = event["result"]
        if isinstance(event.get("structured_output"), dict):
            tracker.structured = event["structured_output"]
        baseline = self.spec.get("resume_baseline") or {}
        if event.get("total_cost_usd") is not None:
            own_cost = event["total_cost_usd"] - baseline.get("cost_usd", 0.0)
            tracker.totals.cost_usd = tracker.cost_base + max(own_cost, 0.0)
        for denial in event.get("permission_denials") or []:
            tracker.permission_denials.append(clip(f"{denial.get('tool_name')}: {json.dumps(denial.get('tool_input'), ensure_ascii=False)}"))

        # Streamed events under-report output tokens and omit compaction calls;
        # the result event is authoritative.
        model_usage = event.get("modelUsage")
        try:
            models = list(model_usage.values())
            final = [
                sum(m["inputTokens"] for m in models) - baseline.get("input_uncached", 0),
                sum(m["cacheReadInputTokens"] for m in models) - baseline.get("cache_read", 0),
                sum(m["cacheCreationInputTokens"] for m in models) - baseline.get("cache_write", 0),
                sum(m["outputTokens"] for m in models) - baseline.get("output", 0),
            ]
            # Replace this process's streamed contribution, keeping earlier rounds intact.
            totals = tracker.totals
            totals.input_uncached += final[0] - self._streamed[0]
            totals.cache_read += final[1] - self._streamed[1]
            totals.cache_write += final[2] - self._streamed[2]
            totals.output += final[3] - self._streamed[3]
            self._streamed = final
        except (AttributeError, KeyError, TypeError):
            tracker.note("result event has no usable modelUsage; token totals come from streamed events and under-count output")

    def describe(self, event: dict) -> list[str]:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            return [f"session {event.get('session_id')} model {event.get('model')}"]
        if kind == "system" and event.get("subtype") == "compact_boundary":
            meta = event.get("compact_metadata") or {}
            return [f"compacted context: {meta.get('pre_tokens')} -> {meta.get('post_tokens')} tokens"]
        if kind == "assistant":
            lines = []
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append("assistant: " + clip(block["text"]))
                elif block.get("type") == "tool_use":
                    tool_input = block.get("input") or {}
                    brief = tool_input.get("command") or tool_input.get("file_path") or json.dumps(tool_input, ensure_ascii=False)
                    lines.append(f"tool {block.get('name')}: " + clip(brief))
            return lines
        if kind == "user":
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list):
                return [
                    "tool error: " + clip(json.dumps(b.get("content"), ensure_ascii=False))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
                ]
            return []
        if kind == "result":
            return [f"result [{event.get('subtype')}] turns={event.get('num_turns')} cost_usd={event.get('total_cost_usd')}"]
        return []
