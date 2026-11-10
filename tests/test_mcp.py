"""The MCP server spoken to over stdio, as an agent CLI would."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fwbridge.limits import Limits

FAKE_WORKER = Path(__file__).parent / "fake_worker.py"


class Client:
    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fwbridge.cli", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env=env, text=True,
        )
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id
        return reply

    def call(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def client(tmp_path):
    import os
    env = dict(os.environ, FWB_HOME=str(tmp_path / "home"), FWB_CLAUDE_BIN=str(FAKE_WORKER), FAKE_SCENARIO="success")
    env.pop("FWB_DEPTH", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    c = Client(env)
    yield c
    c.close()


def test_handshake_and_tool_list(client):
    init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "fwb"
    client.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    names = {tool["name"] for tool in client.request("tools/list")["result"]["tools"]}
    assert names == {"delegate", "task_wait", "task_status", "task_result", "task_log", "task_kill"}
    assert client.request("no/such/method")["error"]["code"] == -32601


def test_delegate_wait_result(client, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    text, is_error = client.call("delegate", worker="claude", prompt="go", cwd=str(work), limits={"max_api_calls": 50})
    assert not is_error, text
    task_id = text.split()[2].rstrip(".")

    text, _ = client.call("task_wait", task=task_id)
    assert "[succeeded] worker=claude outcome=completed" in text and "api calls 3 / limit 50" in text
    text, _ = client.call("task_result", task=task_id)
    assert text.startswith("outcome: completed")
    assert "assistant: working" in client.call("task_log", task=task_id)[0]
    assert task_id in client.call("task_status")[0]


def test_errors_are_tool_errors(client, tmp_path):
    text, is_error = client.call("delegate", worker="claude", prompt="go", cwd="relative/dir")
    assert is_error and "absolute" in text
    text, is_error = client.call("task_wait")
    assert is_error and "needs a task id" in text
    text, is_error = client.call("delegate", worker="claude", cwd=str(tmp_path))
    assert is_error and "prompt is empty" in text
    text, is_error = client.call("task_result", task="nope")
    assert is_error and "no task matches" in text
    text, is_error = client.call("delegate", worker="claude", prompt="go", cwd=str(tmp_path), limits={"max_api_calls": -1})
    assert is_error and ">= 0" in text


def test_limit_overrides():
    limits = Limits.from_overrides({"max_api_calls": 0, "max_context_tokens": 5})
    assert limits.max_api_calls is None and limits.max_context_tokens == 5
    assert limits.max_total_tokens == Limits().max_total_tokens
    with pytest.raises(ValueError, match="unknown"):
        Limits.from_overrides({"bogus": 1})


def test_follow_up_over_mcp(client, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    first = client.call("delegate", worker="claude", prompt="go", cwd=str(work))[0].split()[2].rstrip(".")
    client.call("task_wait", task=first)
    text, is_error = client.call("delegate", prompt="and then fix it", resume=first)
    assert not is_error, text
    second = text.split()[2].rstrip(".")
    text, _ = client.call("task_wait", task=second)
    assert f"follow-up of task {first} (resumed session fake-session)" in text
    assert "[succeeded]" in text
