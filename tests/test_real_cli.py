"""Checks against the real claude and codex CLIs.

Slow (minutes) and spends tokens, so they are skipped unless FWB_REAL_CLI=1. Run them
after upgrading either CLI. Codex runs as a worker with danger-full-access because its
sandbox cannot start on hosts that restrict unprivileged user namespaces.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("FWB_REAL_CLI") != "1", reason="set FWB_REAL_CLI=1 to run against the real CLIs")

FWB = [sys.executable, "-m", "fwbridge.cli"]
CLAUDE_MODEL = os.environ.get("FWB_REAL_CLAUDE_MODEL", "haiku")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FWB_HOME", str(tmp_path / "home"))
    for name in ("FWB_DEPTH", "FWB_CLAUDE_BIN", "FWB_CODEX_BIN", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    return dict(os.environ)


def fwb(*args, timeout=600):
    done = subprocess.run([*FWB, *args], capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout + done.stderr


def run_task(*args) -> str:
    code, out = fwb("run", *args)
    assert code == 0, out
    return out.split()[2]


def state(task_id) -> dict:
    return json.loads(fwb("status", task_id, "--json")[1])


# Codex starts MCP servers with a filtered environment; credentials kept in environment
# variables must be passed on by name. FWB_UNSET_TEST_VAR checks that unset names are tolerated.
PASS_ENV = [name for name in os.environ if name.startswith("ANTHROPIC_")] + ["FWB_UNSET_TEST_VAR"]


def mcp_config(home: Path, wait_s: int) -> list[str]:
    server_env = f'{{FWB_HOME="{home}", FWB_MCP_WAIT_MAX_S="{wait_s}"}}'
    return [
        "-c", f"mcp_servers.fwb.env_vars={json.dumps(PASS_ENV)}",
        "-c", f'mcp_servers.fwb.command="{sys.executable}"',
        "-c", 'mcp_servers.fwb.args=["-m", "fwbridge.cli", "mcp"]',
        "-c", f"mcp_servers.fwb.tool_timeout_sec={wait_s + 60}",
        # `codex exec` cannot ask for approval and rejects unapproved MCP calls.
        "-c", 'mcp_servers.fwb.default_tools_approval_mode="approve"',
        "-c", f"mcp_servers.fwb.env={server_env}",
    ]


@pytest.mark.parametrize("worker", ["claude", "codex"])
def test_follow_up_keeps_the_session(env, tmp_path, worker):
    work = tmp_path / "work"
    work.mkdir()
    opts = ["--model", CLAUDE_MODEL] if worker == "claude" else ["--sandbox", "danger-full-access"]
    first = run_task("-w", worker, "--cwd", str(work), *opts, "-p",
                     "Remember the code word FALCON-7. Create a.txt containing the single line 1.")
    code, out = fwb("wait", first, "--timeout", "300")
    assert code == 0, out

    second = run_task("--resume", first, *opts, "-p",
                      "Create b.txt containing only the code word I asked you to remember.")
    code, out = fwb("wait", second, "--timeout", "300")
    assert code == 0, out
    assert "FALCON-7" in (work / "b.txt").read_text()

    before, after = state(first), state(second)
    assert after["notes"] == [], after["notes"]
    # The follow-up re-reads the session but must not re-count the first task's calls.
    assert after["usage"]["api_calls"] <= before["usage"]["api_calls"] + 3
    print(f"\n{worker}: first {before['usage']}\n{worker}: follow-up {after['usage']}")


def test_codex_host_delegates_over_mcp_from_its_sandbox(env, tmp_path):
    """Codex in its default sandbox (no shell access to fwb) delegates to Claude through MCP."""
    host, work = tmp_path / "host", tmp_path / "work"
    host.mkdir()
    work.mkdir()
    prompt = (
        "Use the fwb MCP tools, not the shell. 1) Call delegate with worker='claude', "
        f"model='{CLAUDE_MODEL}', cwd='{work}', prompt='Create made_by_claude.txt containing ok.' "
        "2) Call task_wait with that task id until the task is no longer running. "
        "3) Call task_result. Reply with the task id and the outcome."
    )
    done = subprocess.run(
        ["codex", "exec", "--json", "--skip-git-repo-check", "-C", str(host), *mcp_config(tmp_path / "home", 240), prompt],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    tasks = json.loads(fwb("status", "--json")[1])
    assert [t["status"] for t in tasks] == ["succeeded"], fwb("status", "last")[1]
    assert (work / "made_by_claude.txt").read_text().strip() == "ok"
    # fwb found the host Codex session through the MCP server's parent process.
    task = json.loads(fwb("status", "last", "--json")[1])
    thread = json.loads(done.stdout.splitlines()[0])["thread_id"]
    spec = json.loads((tmp_path / "home" / "tasks" / task["id"] / "task.json").read_text())
    assert spec["host"]["kind"] == "codex" and spec["host"]["session_id"] == thread
    assert task["host_usage"]["api_calls"] >= 1
    print(f"\nhost codex usage while the task ran: {task['host_usage']}")


def test_codex_worker_cannot_delegate_over_mcp(env, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    worker_args = [f"--worker-arg={option}" for option in mcp_config(tmp_path / "home", 30)]
    task = run_task(
        "-w", "codex", "--sandbox", "danger-full-access", "--cwd", str(work), *worker_args, "-p",
        f"Call the fwb MCP tool delegate exactly once with worker='claude', cwd='{work}', prompt='say hi'. "
        "Do not retry. Report the tool's exact response text.",
    )
    code, out = fwb("wait", task, "--timeout", "300")
    assert code in (0, 3), out
    tasks = json.loads(fwb("status", "--json")[1])
    assert len(tasks) == 1, "the nested delegate call must not create a task"
    assert "refusing to delegate" in fwb("log", task, "-n", "200")[1] + fwb("result", task)[1]
