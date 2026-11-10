"""End-to-end runs of the supervisor against a fake worker."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from fwbridge import supervisor, tasks
from fwbridge.limits import Limits
from fwbridge.store import TaskError, is_alive, read_json

FAKE_WORKER = Path(__file__).parent / "fake_worker.py"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FWB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FWB_CLAUDE_BIN", str(FAKE_WORKER))
    monkeypatch.delenv("FWB_DEPTH", raising=False)
    monkeypatch.delenv("FWB_MAX_DEPTH", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("FAKE_OUTCOME", raising=False)
    monkeypatch.setenv("FWB_TEST_PYTHON", sys.executable)
    monkeypatch.setattr(supervisor, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(supervisor, "TERM_GRACE_S", 2)
    (tmp_path / "work").mkdir()


def make_task(tmp_path, monkeypatch, scenario, structured=True, **limit_overrides):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    return tasks.create_task(
        worker="claude", prompt="do the thing", cwd=str(tmp_path / "work"),
        limits=Limits(**limit_overrides), structured=structured,
    )


def test_success(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "success")
    assert supervisor.run(str(paths.dir)) == "succeeded"
    state = read_json(paths.state)
    assert state["usage"]["api_calls"] == 3
    assert state["usage"]["output"] == 150  # from the authoritative result event, not the stream
    assert state["usage"]["cost_usd"] == pytest.approx(0.3)
    assert state["exit_code"] == 0
    assert state["outcome"] == "completed"
    assert read_json(paths.result_json)["files_changed"] == ["a.txt"]
    assert paths.result.read_text().startswith("outcome: completed\n\nall done\n")
    assert state["notes"] == []
    assert len(paths.usage.read_text().splitlines()) == 3


def test_worker_gets_prompt_and_depth(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "echo_env")
    supervisor.run(str(paths.dir))
    events = paths.events.read_text()
    assert "depth=1 argv=" in events and "prompt=You are executing a task" in events
    assert r"Task:\ndo the thing" in events  # newlines are JSON-escaped in the event file


def test_raw_result_keeps_plain_text(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OUTCOME", "none")
    paths = make_task(tmp_path, monkeypatch, "success", structured=False)
    assert supervisor.run(str(paths.dir)) == "succeeded"
    state = read_json(paths.state)
    assert state["outcome"] is None and state["notes"] == []
    assert paths.result.read_text() == "all done"
    assert not paths.result_json.exists()


def test_missing_structured_result_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OUTCOME", "none")
    paths = make_task(tmp_path, monkeypatch, "success")
    assert supervisor.run(str(paths.dir)) == "succeeded"
    state = read_json(paths.state)
    assert state["outcome"] is None
    assert any("without a structured result" in note for note in state["notes"])


def test_partial_outcome_continues_in_a_new_round(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "partial_then_done")
    paths = tasks.create_task(worker="claude", prompt="do the thing", cwd=str(tmp_path / "work"), limits=Limits(), max_rounds=3)
    assert supervisor.run(str(paths.dir)) == "succeeded"
    state = read_json(paths.state)
    assert state["outcome"] == "completed"
    assert [r["outcome"] for r in state["rounds"]] == ["partial", "completed"]
    # Usage and cost add up over both worker processes.
    assert state["usage"]["api_calls"] == 6
    assert state["usage"]["output"] == 300
    assert state["usage"]["cost_usd"] == pytest.approx(0.6)
    # The second process was given the first one's report.
    assert "<previous_report>\noutcome: partial" in paths.worker_input.read_text()


def test_partial_outcome_without_rounds_left_is_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OUTCOME", "partial")
    paths = make_task(tmp_path, monkeypatch, "success")
    assert supervisor.run(str(paths.dir)) == "succeeded"
    state = read_json(paths.state)
    assert state["outcome"] == "partial" and len(state["rounds"]) == 1


def test_autocompact_resolution(tmp_path, monkeypatch):
    work = str(tmp_path / "work")
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    default = tasks.create_task(worker="claude", prompt="x", cwd=work, limits=Limits(), allow_concurrent=True)
    assert read_json(default.spec)["autocompact_tokens"] == tasks.DEFAULT_AUTOCOMPACT_TOKENS
    # A hard context limit below the default threshold switches the default off.
    tight = tasks.create_task(worker="claude", prompt="x", cwd=work, limits=Limits(max_context_tokens=50_000), allow_concurrent=True)
    assert read_json(tight.spec)["autocompact_tokens"] is None
    with pytest.raises(TaskError, match="must be below"):
        tasks.create_task(worker="claude", prompt="x", cwd=work, limits=Limits(), autocompact_tokens=250_000, allow_concurrent=True)
    with pytest.raises(TaskError, match="between 100000 and 1000000"):
        tasks.create_task(worker="claude", prompt="x", cwd=work, limits=Limits(), autocompact_tokens=50_000, allow_concurrent=True)


def test_context_limit_kills_growing_worker(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "grow", max_context_tokens=50_000)
    assert supervisor.run(str(paths.dir)) == "killed"
    state = read_json(paths.state)
    assert state["kill_reason"].startswith("context_tokens limit reached")
    assert state["usage"]["max_context"] >= 50_000
    assert not is_alive(state["worker_pid"], state["worker_start_ticks"])


def test_notify_command_on_warning_and_end(tmp_path, monkeypatch):
    log = tmp_path / "notify.log"
    monkeypatch.setenv("FAKE_SCENARIO", "grow")
    paths = tasks.create_task(
        # Context doubles per call: 64k lands in the warning band of a 70k limit, 128k breaches it.
        worker="claude", prompt="x", cwd=str(tmp_path / "work"), limits=Limits(max_context_tokens=70_000),
        notify_command=f'echo "$FWB_EVENT|$FWB_STATUS|$FWB_MESSAGE" >> {log}',
    )
    assert supervisor.run(str(paths.dir)) == "killed"
    events = log.read_text().splitlines()
    assert events[0].startswith("warning|running|") and "context_tokens at" in events[0]
    assert events[-1].startswith("finished|killed|") and "context_tokens limit reached" in events[-1]
    assert len(events) == 2  # each warning is sent once


def test_failing_notify_command_is_noted(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    paths = tasks.create_task(worker="claude", prompt="x", cwd=str(tmp_path / "work"), limits=Limits(), notify_command="exit 7")
    assert supervisor.run(str(paths.dir)) == "succeeded"
    assert any("notify command exited with 7" in n for n in read_json(paths.state)["notes"])


def test_stall_timeout(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "stall", stall_timeout_s=0.5)
    started = time.time()
    assert supervisor.run(str(paths.dir)) == "killed"
    assert read_json(paths.state)["kill_reason"].startswith("stall_s limit reached")
    assert time.time() - started < 10


def test_crash_is_reported_as_failure(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "crash")
    assert supervisor.run(str(paths.dir)) == "failed"
    state = read_json(paths.state)
    assert state["exit_code"] == 3
    assert "without reporting success" in state["error"]
    assert not paths.result.exists()


def test_nested_delegation_is_refused_without_env_marker(tmp_path, monkeypatch):
    paths = make_task(tmp_path, monkeypatch, "delegate_again")
    # Detection looks for a supervisor among the ancestors, so run it the way fwb does.
    subprocess.run([sys.executable, "-m", "fwbridge.supervisor", str(paths.dir)], check=True, timeout=60)
    events = paths.events.read_text()
    assert "nested exit=2" in events and "refusing to delegate" in events
    assert len(list((tmp_path / "home" / "tasks").iterdir())) == 1


def test_follow_up_resumes_the_worker_session(tmp_path, monkeypatch):
    first = make_task(tmp_path, monkeypatch, "success")
    assert supervisor.run(str(first.dir)) == "succeeded"

    monkeypatch.setenv("FAKE_SCENARIO", "echo_env")
    follow = tasks.create_task(worker=None, prompt="now fix it", cwd=None, limits=Limits(), resume_task=first.task_id)
    spec = read_json(follow.spec)
    assert (spec["worker"], spec["cwd"], spec["resume_session"]) == ("claude", read_json(first.spec)["cwd"], "fake-session")
    assert spec["resume_baseline"]["output"] == 150

    assert supervisor.run(str(follow.dir)) == "succeeded"
    events = follow.events.read_text()
    assert "'--resume', 'fake-session'" in events
    assert "prompt=Follow-up from the agent" in events and "now fix it" in events
    # The resumed session reports cumulative usage; only this task's share is kept.
    usage = read_json(follow.state)["usage"]
    assert usage["output"] == 50 and usage["cache_read"] == 890
    assert usage["cost_usd"] == pytest.approx(0.1)


def test_follow_up_rules(tmp_path, monkeypatch):
    first = make_task(tmp_path, monkeypatch, "success")
    with pytest.raises(TaskError, match="still queued"):
        tasks.create_task(worker=None, prompt="x", cwd=None, limits=Limits(), resume_task=first.task_id)
    supervisor.run(str(first.dir))
    with pytest.raises(TaskError, match="same worker"):
        tasks.create_task(worker="codex", prompt="x", cwd=None, limits=Limits(), resume_task=first.task_id)
    with pytest.raises(TaskError, match="cannot be combined"):
        tasks.create_task(worker=None, prompt="x", cwd=None, limits=Limits(), resume_task=first.task_id, max_rounds=2)


def test_nested_delegation_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("FWB_DEPTH", "1")
    with pytest.raises(TaskError, match="refusing to delegate"):
        make_task(tmp_path, monkeypatch, "success")


def test_cost_limit_rejected_for_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("FWB_CODEX_BIN", str(FAKE_WORKER))
    with pytest.raises(TaskError, match="does not report cost"):
        tasks.create_task(worker="codex", prompt="x", cwd=str(tmp_path / "work"), limits=Limits(max_cost_usd=1.0))


def fwb(*args):
    return subprocess.run([sys.executable, "-m", "fwbridge.cli", *args], capture_output=True, text=True, timeout=60)


def test_cli_background_run_lock_and_kill(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "stall")
    work = str(tmp_path / "work")
    started = fwb("run", "-w", "claude", "--cwd", work, "-p", "hang")
    assert started.returncode == 0, started.stderr
    task_id = started.stdout.split()[2]

    deadline = time.time() + 10
    while "[running]" not in fwb("status", task_id).stdout:
        assert time.time() < deadline
        time.sleep(0.1)

    second = fwb("run", "-w", "claude", "--cwd", work, "-p", "second")
    assert second.returncode == 2
    assert "already running" in second.stderr

    killed = fwb("kill", task_id)
    assert killed.returncode == 0, killed.stdout + killed.stderr
    assert "[killed]" in killed.stdout and "cancelled by user" in killed.stdout
    assert fwb("wait", task_id).returncode == 1


def test_cli_run_wait_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    done = fwb("run", "-w", "claude", "--cwd", str(tmp_path / "work"), "-p", "go", "--wait")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[succeeded] worker=claude outcome=completed" in done.stdout
    assert "all done" in fwb("result", "last").stdout
    assert '"outcome": "completed"' in fwb("result", "last", "--json").stdout
    assert "assistant: working" in fwb("log", "last").stdout


def test_cli_exit_code_reflects_worker_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_OUTCOME", "blocked")
    done = fwb("run", "-w", "claude", "--cwd", str(tmp_path / "work"), "-p", "go", "--wait")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "[succeeded] worker=claude outcome=blocked" in done.stdout
