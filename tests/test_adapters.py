"""Adapters parsed against event streams recorded from the real CLIs."""

import json
import shutil
from pathlib import Path

import pytest

from fwbridge.workers import ADAPTERS, Tracker

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_THREAD = "01a1c55e-814b-7453-9187-2847af9c3018"


def feed(adapter, tracker, fixture):
    for line in (FIXTURES / fixture).read_text().splitlines():
        adapter.on_event(json.loads(line), tracker)


def test_claude_stream(tmp_path):
    adapter, tracker = ADAPTERS["claude"](), Tracker(tmp_path / "usage.jsonl")
    feed(adapter, tracker, "claude_stream.jsonl")
    totals = tracker.totals

    assert tracker.outcome == "success"
    assert tracker.result_text == "done"
    assert tracker.model == "claude-haiku-5-5"
    # Two API calls, although the first one is streamed as two events.
    assert totals.api_calls == 2
    assert len((tmp_path / "usage.jsonl").read_text().splitlines()) == 2
    # Totals equal the authoritative figures of the result event.
    assert (totals.input_uncached, totals.cache_write, totals.cache_read, totals.output) == (4, 25032, 24860, 135)
    assert totals.cost_usd == 0.0034455
    assert totals.max_context == 2 + 172 + 24860
    assert tracker.notes == []


def test_claude_live_totals_before_result(tmp_path):
    adapter, tracker = ADAPTERS["claude"](), Tracker()
    for line in (FIXTURES / "claude_stream.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["type"] != "result":
            adapter.on_event(event, tracker)
    # Input-side tokens are exact while the task is still running.
    assert (tracker.totals.input_uncached, tracker.totals.cache_write, tracker.totals.cache_read) == (4, 25032, 24860)
    assert tracker.outcome is None


def test_codex_stream_with_live_rollout(tmp_path, monkeypatch):
    day = tmp_path / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_rollout.jsonl", day / f"rollout-2026-11-10T20-11-12-{CODEX_THREAD}.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    adapter, tracker = ADAPTERS["codex"](), Tracker()
    feed(adapter, tracker, "codex_stream.jsonl")
    assert adapter.poll(tracker) is True
    assert adapter.poll(tracker) is False  # nothing new
    adapter.finalize(tracker)
    totals = tracker.totals

    assert tracker.session_id == CODEX_THREAD
    assert tracker.outcome == "success"
    assert totals.api_calls == 3
    assert totals.max_context == 24418
    assert totals.cache_read == 48512
    assert totals.total_tokens == 72857 + 315  # matches the end-of-turn aggregate
    assert tracker.rate_limit["used_percent"] == 55.0
    assert tracker.notes == []


def test_claude_structured_output():
    adapter, tracker = ADAPTERS["claude"]({"structured": True}), Tracker()
    feed(adapter, tracker, "claude_structured.jsonl")
    assert tracker.structured["outcome"] == "completed"
    assert tracker.structured["files_changed"] == ["/home/user/project/tmp/p1/ok.txt"]


def test_claude_compaction_and_thrashing_error():
    adapter, tracker = ADAPTERS["claude"](), Tracker()
    feed(adapter, tracker, "claude_compact.jsonl")
    assert tracker.compactions == 3
    # The locally generated "thrashing" notice is not a model call.
    assert tracker.totals.api_calls == 0
    assert tracker.outcome == "error"
    assert "Autocompact is thrashing" in tracker.error
    # Totals still come from the result event, which includes the compaction calls.
    assert tracker.totals.total_tokens > 300_000


def test_codex_compaction_in_rollout(tmp_path, monkeypatch):
    thread = "01a1c56d-eafe-7df3-b896-66fc7a41482f"
    day = tmp_path / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_rollout_compact.jsonl", day / f"rollout-2026-11-10T20-28-00-{thread}.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    adapter, tracker = ADAPTERS["codex"](), Tracker()
    adapter.on_event({"type": "thread.started", "thread_id": thread}, tracker)
    adapter.poll(tracker)
    assert tracker.compactions == 2
    # The zero-usage record after each compaction is not a model call.
    assert tracker.totals.api_calls == 5
    assert tracker.totals.total_tokens == 124213
    # Compaction brought the context back down from its peak.
    assert tracker.totals.max_context == 30224 and tracker.totals.last_context == 21334


def test_codex_structured_result_is_parsed():
    adapter, tracker = ADAPTERS["codex"]({"structured": True}), Tracker()
    answer = {"outcome": "blocked", "summary": "s", "files_changed": [], "verification": "none", "remaining": ["r"]}
    adapter.on_event({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(answer)}}, tracker)
    adapter.finalize(tracker)
    assert tracker.structured == answer


def test_claude_resume_subtracts_session_baseline(tmp_path):
    from fwbridge.store import TaskPaths
    previous = TaskPaths(tmp_path / "prev")
    previous.dir.mkdir()
    shutil.copy(FIXTURES / "claude_resume_prev.jsonl", previous.events)
    session = json.loads((FIXTURES / "claude_resume_prev.jsonl").read_text())["session_id"]
    baseline = ADAPTERS["claude"].session_baseline(previous, session)
    assert baseline["cache_write"] == 25287 and baseline["output"] == 4

    adapter = ADAPTERS["claude"]({"resume_session": session, "resume_baseline": baseline})
    tracker = Tracker()
    adapter.prepare(tracker)
    feed(adapter, tracker, "claude_resume.jsonl")
    totals = tracker.totals
    assert tracker.result_text == "4711"
    # The resumed process made one call; the result event's totals cover the whole session.
    assert totals.api_calls == 1
    assert (totals.input_uncached, totals.cache_read, totals.cache_write, totals.output) == (4, 17114, 8197, 4)
    assert totals.input_uncached + totals.cache_read + totals.cache_write == totals.max_context
    assert totals.cost_usd == pytest.approx(0.00436124 - 0.003163075)
    assert tracker.notes == []


def test_codex_resume_subtracts_session_baseline(tmp_path, monkeypatch):
    thread = "01a1c584-57d5-7921-93ae-af54e57c280f"
    day = tmp_path / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-11-10T20-52-00-{thread}.jsonl"
    shutil.copy(FIXTURES / "codex_resume_rollout_before.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    adapter, tracker = ADAPTERS["codex"]({"resume_session": thread}), Tracker()
    adapter.prepare(tracker)
    with open(rollout, "a") as f:
        f.write((FIXTURES / "codex_resume_rollout_after.jsonl").read_text())
    feed(adapter, tracker, "codex_resume_stream.jsonl")
    adapter.poll(tracker)
    adapter.finalize(tracker)
    totals = tracker.totals
    assert tracker.result_text == "4711"
    assert totals.api_calls == 1  # the earlier session's call is not counted again
    assert totals.total_tokens == 21082
    assert tracker.notes == []  # live and end-of-turn figures agree after the baseline


def test_codex_resume_command():
    cmd = ADAPTERS["codex"]({"resume_session": "abc", "cwd": "/w", "sandbox": "danger-full-access"}).build_command("codex")
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert 'sandbox_mode="danger-full-access"' in cmd and "-C" not in cmd
    assert cmd[-2:] == ["abc", "-"]


def test_codex_without_rollout_keeps_context_unobserved(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    adapter, tracker = ADAPTERS["codex"](), Tracker()
    feed(adapter, tracker, "codex_stream.jsonl")
    adapter.poll(tracker)
    adapter.finalize(tracker)

    assert tracker.totals.total_tokens == 72857 + 315
    assert tracker.totals.max_context is None
    assert tracker.totals.api_calls == 0
    assert any("end-of-turn aggregate" in note for note in tracker.notes)
