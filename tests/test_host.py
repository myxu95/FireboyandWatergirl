"""Identifying the host session and reading its usage."""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fwbridge.host import detect_host, host_usage

FIXTURES = Path(__file__).parent / "fixtures"


def claude_entry(msg_id, ts, context, output, model="claude-x"):
    return {
        "type": "assistant", "timestamp": ts,
        "message": {"id": msg_id, "model": model, "usage": {
            "input_tokens": 1, "cache_read_input_tokens": context - 1, "cache_creation_input_tokens": 0, "output_tokens": output,
        }},
    }


def test_claude_host_from_session_env(tmp_path, monkeypatch):
    session = "11111111-2222-3333-4444-555555555555"
    transcript = tmp_path / "projects" / "-some-project" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [
        claude_entry("m1", "2026-11-10T10:00:00Z", 1000, 5),  # before the window
        claude_entry("m2", "2026-11-10T10:05:00Z", 2000, 3),
        claude_entry("m2", "2026-11-10T10:05:00Z", 2000, 7),  # same message, later copy
        claude_entry("m3", "2026-11-10T10:06:00Z", 0, 0, model="<synthetic>"),
        claude_entry("m4", "2026-11-10T10:07:00Z", 3000, 9),
        {"type": "user", "timestamp": "2026-11-10T10:07:30Z"},
    ]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)

    host = detect_host()
    assert host == {"kind": "claude", "session_id": session, "transcript": str(transcript)}
    start = datetime(2026, 11, 10, 10, 4, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 11, 10, 10, 10, tzinfo=timezone.utc).timestamp()
    usage = host_usage(host, start, end)
    assert usage["api_calls"] == 2
    assert usage["output"] == 16 and usage["max_context"] == 3000


def test_codex_host_from_parent_process_rollout(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    thread = "01a1c56d-eafe-7df3-b896-66fc7a41482f"
    rollout = tmp_path / "sessions" / "2026" / "11" / "10" / f"rollout-2026-11-10T20-28-00-{thread}.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_rollout_compact.jsonl", rollout)
    # A parent that holds the rollout open, as a running Codex does, starts the detector.
    parent = (
        "import subprocess, sys\n"
        f"handle = open({str(rollout)!r}, 'a')\n"
        "child = 'import json; from fwbridge.host import detect_host; print(json.dumps(detect_host()))'\n"
        "print(subprocess.run([sys.executable, '-c', child], capture_output=True, text=True).stdout)\n"
    )
    out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=30).stdout
    host = json.loads(out)
    assert host["kind"] == "codex" and host["session_id"] == thread

    usage = host_usage(host, 0, 4e9)
    assert usage["api_calls"] == 5  # the zero records after compactions are not calls
    assert usage["total_tokens"] == 124213
    assert host_usage(host, 0, 1) ["api_calls"] == 0


def test_no_host_outside_an_agent(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert detect_host() is None
