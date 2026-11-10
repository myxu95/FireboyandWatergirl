# fwbridge

[![test](https://github.com/myxu95/FireboyandWatergirl/actions/workflows/test.yml/badge.svg)](https://github.com/myxu95/FireboyandWatergirl/actions/workflows/test.yml)

Let **Claude Code** and **Codex** hand tasks to each other, with hard limits on what the delegated work, and the waiting for it, can cost.

One agent (the *host*) starts a task with `fwb`; the other agent (the *worker*) runs it in the background under a supervisor. The supervisor counts every model call, stops the worker when a limit is reached, and hands back a structured report. The repository is named after *Fireboy and Watergirl*, the two-player game in which neither character can finish a level alone.

## Why

A long task delegated from one agent to the other can get expensive in two places:

- **The worker.** Every model call re-reads the whole conversation, so the cumulative cost of a long session grows roughly with the square of its length. With context windows of up to a million tokens, nothing stops it early.
- **The host.** Every time the host checks on the task, it makes a model call that re-reads *its own* context. A host that polls once a minute pays for that through the whole task.

fwbridge puts limits and accounting on both sides.

## Features

- **Two entry points:** the `fwb` command line, and an MCP server (`fwb mcp`) for agents whose shell is sandboxed.
- **Token accounting per model call** for the worker, plus the host's own usage while the task runs.
- **Hard limits** on context per call, total tokens, number of calls, cost, inactivity and wall time. A breach stops the worker's whole process group.
- **Context control** for long tasks: sets the worker's auto-compaction threshold.
- **Structured results:** outcome, summary, files changed, verification and remaining work.
- **Continuation and follow-ups:** retry a partial result in a fresh session, or send a follow-up into a finished task's session.
- **Safety guards:** no delegation from inside a delegated task, one running task per directory, no permission bypass by default.
- **Notifications** through a shell command of your choice.
- **No daemon.** Each task is a directory of plain files plus one detached supervisor process. Pure standard library.

## Requirements

- Linux (process tracking uses `/proc`)
- Python 3.10 or newer
- `claude` (Claude Code, tested with 2.1.322) and/or `codex` (Codex CLI, tested with 0.167.0), installed and logged in

## Install

```bash
pip install git+https://github.com/myxu95/FireboyandWatergirl
```

For development, use the conda/mamba environment in the repository:

```bash
git clone https://github.com/myxu95/FireboyandWatergirl && cd FireboyandWatergirl
mamba env create -f environment.yml    # creates the environment "fwbridge"
mamba run -n fwbridge python -m pytest -q
```

When another agent calls `fwb`, give it the absolute path (`which fwb`), since an agent's shell may not activate your environment.

## Quick start

```bash
# Start a task in the background; prints the task id immediately.
fwb run -w claude --cwd ~/src/myrepo -p "Make the failing test in tests/test_io.py pass."

fwb status last            # tokens, context size, limits, last activity
fwb wait last --timeout 600
fwb result last            # the worker's report
```

`fwb wait` (and `fwb run --wait`) exit with:

| Code | Meaning |
|---|---|
| 0 | the worker finished and reports the task completed |
| 3 | the worker finished but reports `partial`, `blocked` or `failed` |
| 1 | the worker failed, or was stopped by a limit or by `fwb kill` |
| 124 | the wait timed out; the task is still running |
| 2 | usage error: unknown task, conflicting options, or refused by a guard |

## Commands

```bash
fwb run -w {claude,codex} [--cwd DIR] (-p PROMPT | --prompt-file FILE | stdin) [options]
fwb run --resume TASK -p PROMPT      # follow-up into a finished task's worker session
fwb status [TASK] [--json]           # one task, or the recent tasks with a summary line
fwb wait TASK [--timeout SECONDS]
fwb log TASK [-n LINES] [-f]         # tool calls, messages, context compactions
fwb result TASK [--json]
fwb kill TASK
fwb mcp                              # serve the same operations over MCP (stdio)
```

`TASK` is a full id, a unique prefix or suffix of one (for example its last four characters), or `last`. Run `fwb run --help` for all options.

## Using fwbridge from an agent

### Claude Code as the host

Claude Code calls `fwb` from its Bash tool. To wait without spending tokens, run `fwb wait <id>` as a background command: Claude Code is notified when it exits and makes no model calls in between. A foreground `fwb wait <id> --timeout 540` also works; the Bash tool allows at most 10 minutes per call.

Measured: while Claude Code waited this way, it made zero model calls for the whole task.

### Codex as the host (MCP)

Codex runs shell commands in a sandbox that blocks network access and writes outside the workspace, so `fwb run` from its shell cannot start a worker. MCP servers run outside that sandbox, so Codex should use the MCP server. Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.fwb]
command = "/absolute/path/to/fwb"
args = ["mcp"]
# `codex exec` cannot ask for approval and rejects MCP calls unless they are approved.
# "approve" lets Codex start workers without asking; use "prompt" for interactive sessions.
default_tools_approval_mode = "approve"
# Codex passes only a few variables (HOME, PATH, USER, ...) to MCP servers. List any
# credentials kept in environment variables, or the worker fails with "Not logged in".
env_vars = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY"]
# Must exceed the longest single wait below (Codex's default is 300 s).
tool_timeout_sec = 1000

[mcp_servers.fwb.env]
FWB_MCP_WAIT_MAX_S = "900"
```

Tools: `delegate` (start a task, or a follow-up with `resume`), `task_wait`, `task_status`, `task_result`, `task_log` and `task_kill`. Their arguments mirror the command line; limits go in the `limits` object of `delegate`, and `cwd` must be absolute.

Measured: with its shell sandbox completely unusable, Codex still delegated to Claude Code through MCP.

Claude Code can register the same server (`claude mcp add --scope user fwb -- /absolute/path/to/fwb mcp`), but the command line with a background wait is enough there.

### What waiting costs the host

In codex-cli 0.167, MCP tools are called from a JavaScript `exec` cell. The cell hands control back to the model after 30 seconds by default, and the model then polls with a built-in `wait` roughly every minute. Each hand-back is a full model call over the host's context: one five-minute wait caused seven of them.

fwbridge counters this in two ways:

- `task_wait` has no timeout argument. It always blocks for the maximum: 280 s by default, below Codex's default tool timeout, or `FWB_MCP_WAIT_MAX_S`. Models left to choose picked 50-second waits. Use `task_status` for an immediate look.
- The responses of `delegate` and the description of `task_wait` tell Codex to start the cell with `// @exec: {"yield_time_ms": ...}`, which keeps the cell blocked for the whole wait. Codex follows it.

Host model calls for one delegation (the worker was a stand-in, so only the host's calls count):

| Setup | Task length | Host calls in total | Wake-ups while waiting |
|---|---|---|---|
| Model chooses the wait (before) | 4 min | 11 | about every 50 s |
| Default (280 s per wait) | 4 min | 6 | 0 |
| Recommended config (900 s per wait) | 10 min | 5 | 0 |

"In total" includes starting the task, reading the result and the final reply. A task longer than one wait wakes the host once per wait.

### Host token monitoring

`fwb status <id>` shows a `host:` line with the model calls and tokens of the session that started the task, counted from the task's start to its end (`host_usage` in `status --json`). It covers everything the host did in that time, not only waiting.

The host is identified from `CLAUDE_CODE_SESSION_ID` (Claude Code) or from the session file that the parent Codex process has open under `~/.codex/sessions`. Tasks started from a plain terminal have no host line.

## Keeping cost in check

### Limits

When a limit is reached, the supervisor stops the worker's process group and marks the task `killed`, with the reason. At 80% of a limit, `status` shows a warning. Pass `0` to disable a limit.

| Option | Default | Limit |
|---|---|---|
| `--max-context-tokens` | 200,000 | prompt size of a single model call, cached or not |
| `--max-total-tokens` | 10,000,000 | input and output tokens summed over all calls |
| `--max-api-calls` | 200 | number of model calls |
| `--max-cost-usd` | none | cost in USD (Claude worker only) |
| `--stall-timeout` | 1,200 s | time without any worker event |
| `--wall-timeout` | 14,400 s | total run time |

### Context compaction

Rather than kill long tasks, fwbridge sets the worker's own compaction threshold: when the context reaches it, the worker summarizes its history and continues. `--autocompact TOKENS` sets it for both workers:

- Claude Code: `claude --autocompact`, which accepts 100,000 to 1,000,000. Set to 100,000, it triggered at 61,000 to 114,000 tokens of context in our runs.
- Codex: `-c model_auto_compact_token_limit=`, with no lower bound.
- Default: 120,000, or unset when `--max-context-tokens` is not above that. `0` keeps the worker's own default.

In a measured run, a task of 50 sequential calls compacted 6 times and never exceeded 70,000 tokens of context; uncompacted, it would have grown to an estimated 250,000.

Compaction loses detail, so long tasks should keep their progress in files. When single steps read too much at once, the context refills right after each compaction; Claude Code detects this ("Autocompact is thrashing") and stops, and the task is marked `failed`. `--max-context-tokens` remains the hard backstop.

### Continuation rounds

A worker sometimes stops early and reports `partial`. With `--max-rounds N`, the supervisor then starts a fresh session with the original task and the previous report, and asks it to check the working directory and finish the rest, up to N sessions in total (default 1). Only `partial` triggers another round; `blocked` and `failed` go back to the host. All limits apply to the rounds together.

### Follow-ups

`fwb run --resume <id> -p "..."` sends a new instruction into the worker session of a finished task, so the worker keeps what it learned ("now fix the issue you reported"). It creates a new task linked to the old one, with the same worker and directory.

Both CLIs report a resumed session's usage and cost cumulatively, including everything before the resume. fwbridge subtracts that baseline, so the follow-up's totals and limits cover only its own calls. If no baseline is available (for example, the earlier task was killed before it reported usage), the totals include the earlier usage and `status` says so.

A resumed session brings back its whole context, so a follow-up to a long session costs more than a fresh task. It cannot be combined with `--max-rounds`.

## Structured results

By default the worker must end with a report that follows `src/fwbridge/result_schema.json`:

| Field | Content |
|---|---|
| `outcome` | `completed`, `partial`, `blocked` or `failed`: the worker's own verdict |
| `summary` | what was done and found |
| `files_changed` | files created, modified or deleted |
| `verification` | what was actually run to check the work, or `none` |
| `remaining` | open work, or why the task did not complete |

A task has two layers of status: `status` describes the process (`succeeded` means the worker ended normally), and `outcome` is the worker's verdict. For example, a Codex worker with a broken sandbox ended normally and reported `blocked`. The outcome is the worker's claim, so check important results yourself. `--raw-result` turns the report off and returns free text.

The prompt is prefixed with fixed rules for the worker: work autonomously, do not delegate further, stop and say so when blocked, and report only what was verified. The exact input is saved as `worker_input.txt` in the task directory.

## Notifications

`--notify COMMAND`, or the environment variable `FWB_NOTIFY_CMD`, names a shell command that the supervisor runs when a task ends and the first time it nears each limit. The command sees `FWB_EVENT` (`finished` or `warning`), `FWB_MESSAGE`, `FWB_TASK_ID`, `FWB_TASK_NAME`, `FWB_STATUS` and `FWB_OUTCOME`. A failing command never affects the task; it is noted in `status`.

```bash
export FWB_NOTIFY_CMD='echo "$(date +%T) $FWB_MESSAGE" >> ~/fwb-notify.log'
```

This option is deliberately not offered over MCP, because it would let a sandboxed agent run commands outside its sandbox.

## Permissions and guards

- **Claude worker:** `--permission-mode acceptEdits` by default. In headless mode an unapproved tool call is denied instead of hanging, and `status` counts the denials. Pre-approve commands with `--allowed-tools`, for example `"Bash(pytest:*)"`.
- **Codex worker:** sandbox `workspace-write` by default; change it with `--sandbox`.
- No mode that skips permission checks is ever used by default. Choose one explicitly with `--permission-mode`, `--sandbox` or `--worker-arg` (write `--worker-arg=-c` for values that start with `-`).
- **No nested delegation:** a delegation from inside a worker, by command line or MCP, is refused, so the two agents cannot hand a task back and forth. Detection looks for an fwbridge supervisor among the ancestor processes as well as at `FWB_DEPTH`, because agent CLIs may filter the environment of the programs they start. `FWB_MAX_DEPTH` allows deeper nesting.
- **One task per directory:** a second running task in the same directory is refused, since two agents editing one tree conflict. `--allow-concurrent` overrides this.

## How it works

There is no daemon. Each task is a directory under `~/.local/state/fwbridge/tasks/<id>/` (or `$FWB_HOME/tasks/`) and one detached supervisor process that runs the worker, parses its event stream and keeps `state.json` current:

```
task.json         the request (never modified)
prompt.txt        the prompt as given
worker_input.txt  what the worker received: fixed rules plus the prompt
state.json        live state written by the supervisor
events.jsonl      the worker's raw event stream
usage.jsonl       one line per model call
stderr.log        the worker's standard error
result.md         the report, readable
result.json       the report, structured
```

If the supervisor dies, the task shows as `orphaned`, and `fwb kill` stops the leftover worker.

### Token accounting

- Every call is recorded as `input_uncached`, `cache_read`, `cache_write` and `output`. `context` is the sum of the first three: what the model read for that call.
- **Claude:** input tokens are exact while the task runs. Streamed output counts run low and omit the compaction calls, so the final `result` event's totals replace them when the task ends. A killed task never gets that event; its totals stay at the live count and its cost is unknown.
- **Codex:** `codex exec --json` reports usage only when the turn ends, so live per-call usage is read from the session file Codex writes under `$CODEX_HOME/sessions`. Without that file only the end-of-turn total is known, and the context size is recorded as not observed rather than zero.
- **Cost** is reported by Claude only, at list price; under a subscription it is nominal. For Codex, `status` shows how much of the plan's usage window is used.

### Environment variables

| Variable | Purpose |
|---|---|
| `FWB_HOME` | where task directories live (default `~/.local/state/fwbridge`) |
| `FWB_NOTIFY_CMD` | default notification command |
| `FWB_MCP_WAIT_MAX_S` | longest single `task_wait` over MCP, in seconds (default 280) |
| `FWB_MAX_DEPTH` | allowed delegation depth (default 1: no nesting) |
| `FWB_CLAUDE_BIN`, `FWB_CODEX_BIN` | worker executables (default `claude`, `codex` on `PATH`) |
| `CODEX_HOME` | Codex's data directory, for reading its session files (default `~/.codex`) |

## Tested scenarios

Run against the real CLIs on 2026-11-10 (Claude Code 2.1.322, codex-cli 0.167.0):

- Claude Code or a terminal delegates to Claude Code.
- A terminal delegates to Codex (`--sandbox danger-full-access`); the live token count matched Codex's own end-of-turn total.
- Codex with an unrestricted shell delegates to Claude Code through `fwb run`, `fwb wait` and `fwb result`.
- Codex in its default sandbox, with no usable shell, delegates to Claude Code over MCP; its session is identified as the host and its usage counted.
- A Codex worker that tries to delegate again is refused, both through `fwb run` (exit code 2) and through MCP (no task created).
- Follow-ups: both workers remembered the earlier session, and the follow-up counted only its own tokens.
- Structured results: Claude reported `completed` (exit code 0); Codex with a broken sandbox reported `blocked` (exit code 3).
- Compaction: Codex at a 30,000-token threshold compacted twice and finished; Claude results are given above.
- Continuation: Codex reported `partial`, and a second session finished the task, with totals across rounds matching Codex's own.

The opt-in tests in `tests/test_real_cli.py` repeat the main scenarios; see [Development](#development).

## Known issues

On systems that restrict unprivileged user namespaces (for example Ubuntu with `kernel.apparmor_restrict_unprivileged_userns=1`), Codex's bubblewrap sandbox cannot start. In the default, `read-only` and `workspace-write` modes Codex then cannot run any command or change any file, and only `danger-full-access` works. On such systems:

- run Codex workers with `--sandbox danger-full-access`;
- let a Codex host use the MCP server, which is not affected. Calling `fwb` from its shell would require starting Codex with `-s danger-full-access`.

Restoring the sandbox needs an AppArmor exception for `bwrap`, which requires root.

## Limitations

- The host side is monitored, not limited: fwbridge cannot stop a host session that spends too much.
- Claude Code as the host over MCP, rather than the command line, has not been tested against the real CLI; its MCP tool timeout and environment handling are unverified.
- Linux only.

## Development

```bash
mamba run -n fwbridge python -m pytest -q    # offline tests; CI runs these on every push

# Against the real CLIs: takes a few minutes and spends a small amount of tokens.
# Run after upgrading claude or codex, whose event formats change often.
FWB_REAL_CLI=1 mamba run -n fwbridge python -m pytest tests/test_real_cli.py -v -s
```

The offline tests replay event streams recorded from the real CLIs (`tests/fixtures/`) and drive the supervisor with a stand-in worker (`tests/fake_worker.py`).

## Related projects

- [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc): a Claude Code plugin that calls Codex through the Codex app server.
- [sendbird/cc-plugin-codex](https://github.com/sendbird/cc-plugin-codex): the reverse, a Codex plugin that calls Claude Code with `claude -p`, `--json-schema` and `--resume`.

fwbridge follows them for structured results and concentrates on what they leave out: token monitoring on both sides, hard limits, and context control for long tasks.
