#!/usr/bin/env python3
"""Stand-in for the claude CLI in tests. Emits stream-json events per FAKE_SCENARIO."""

import json
import os
import sys
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def assistant(index, context, text="working"):
    emit({
        "type": "assistant",
        "message": {
            "id": f"msg_{index}",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": context - 110,
                "cache_creation_input_tokens": 100,
                "output_tokens": 20,
            },
        },
    })


def result(calls):
    outcome = os.environ.get("FAKE_OUTCOME", "completed")
    structured = {} if outcome == "none" else {"structured_output": {
        "outcome": outcome, "summary": "all done", "files_changed": ["a.txt"],
        "verification": "none", "remaining": [],
    }}
    emit({
        **structured,
        "type": "result", "subtype": "success", "is_error": False, "result": "all done",
        "total_cost_usd": 0.25, "num_turns": calls, "permission_denials": [],
        "modelUsage": {"fake-model": {
            "inputTokens": 10 * calls, "cacheReadInputTokens": 890 * calls,
            "cacheCreationInputTokens": 100 * calls, "outputTokens": 50 * calls,
        }},
    })


scenario = os.environ.get("FAKE_SCENARIO", "success")
prompt = sys.stdin.read()
emit({"type": "system", "subtype": "init", "session_id": "fake-session", "model": "fake-model"})

if scenario == "partial_then_done":  # reports partial until it is run as a continuation
    os.environ["FAKE_OUTCOME"] = "completed" if "continuation round 2" in prompt else "partial"
    scenario = "success"

if scenario == "success":
    for i in range(3):
        assistant(i, 1000)
    result(3)
elif scenario == "echo_env":
    assistant(0, 1000, text=f"depth={os.environ.get('FWB_DEPTH')} prompt={prompt.strip()}")
    result(1)
elif scenario == "grow":  # context doubles on every call and never finishes
    for i in range(1000):
        assistant(i, 1000 * 2 ** min(i, 30))
        time.sleep(0.05)
elif scenario == "stall":
    time.sleep(600)
elif scenario == "crash":
    assistant(0, 1000)
    sys.exit(3)
else:
    sys.exit(f"unknown FAKE_SCENARIO {scenario!r}")
