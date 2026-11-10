# fwbridge (FireboyandWatergirl)

让 Claude Code 和 Codex 互相委派任务的本地工具：一方（host）通过 `fwb` 命令把任务交给另一方（worker）在后台执行，并对长任务和 token 消耗做监控与硬性限额。

当前状态：命令行与 MCP 两种入口、worker 与 host 两侧的 token 监控、限额、结构化结果、上下文自动压缩、自动续跑、追加指令、通知均已实现，并用两边真实 CLI 验证。

## 环境

```bash
mamba env create -f environment.yml   # 环境名 fwbridge，仅依赖标准库 + pytest
mamba run -n fwbridge python -m pytest -q

# 对真实 CLI 的检查（几分钟，消耗少量 token；CLI 升级后运行）
FWB_REAL_CLI=1 mamba run -n fwbridge python -m pytest tests/test_real_cli.py -v -s
```

命令入口：激活环境后的 `fwb`，或直接用环境内的绝对路径（如 `~/miniforge3/envs/fwbridge/bin/fwb`），后者便于从另一个 agent 调用。

已验证的 CLI 版本：Claude Code 2.1.322、codex-cli 0.167.0。两者的事件格式变化较快，升级后先跑全部测试，包括上面对真实 CLI 的检查。

## 用法

```bash
# 启动后台任务，立即返回任务 id
fwb run -w claude --cwd /path/to/repo -p "任务描述"
fwb run -w codex  --cwd /path/to/repo --prompt-file task.md

fwb status            # 最近任务列表和汇总
fwb status <id>       # 单个任务：状态、worker 与 host 的 token、上下文、成本、最近动作
fwb wait <id> --timeout 600   # 阻塞等待，退出码见下
fwb log <id> [-f]     # worker 做过的事（工具调用、回复、上下文压缩）
fwb result <id> [--json]   # worker 的最终报告
fwb kill <id>         # 终止任务

# 对已结束的任务追加指令（沿用原任务的 worker、目录和会话）
fwb run --resume <id> -p "追加的要求"
```

`<id>` 可以是完整 id、唯一的前缀或后缀（如末尾 4 位），或 `last`。

`fwb wait` 和 `fwb run --wait` 的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | worker 正常结束，并报告任务已完成 |
| 3 | worker 正常结束，但报告部分完成、受阻或失败 |
| 1 | worker 出错，或被限额 / 手动终止 |
| 124 | 等待超时，任务仍在运行 |
| 2 | 用法错误（未知任务、参数冲突、被保护机制拒绝） |

## 接入 agent

host 每查询一次任务状态就是一次模型调用，要重读 host 自己的整个上下文。所以等待任务应当用阻塞的 `wait`，单次等得越久越省，而不是反复查询 `status`。

### host 的 token 监控

`fwb status <id>` 中的 `host:` 一行显示发起任务的 agent 会话在任务运行期间的模型调用次数和 token，与 worker 的消耗分开列出（`status --json` 中为 `host_usage`）。这个数包括 host 在这段时间内做的所有事，不只是等待。

识别 host 的方式：

- Claude Code：读取它传给子进程的 `CLAUDE_CODE_SESSION_ID`，对应 `~/.claude/projects/` 下的会话记录；
- Codex：沿父进程查找 Codex 正在写入的会话文件（`~/.codex/sessions/` 下的 rollout）。

从普通终端运行 `fwb` 时没有 host，不显示这一行。

### Claude Code 作为 host：命令行

Claude Code 直接在 Bash 里调用 `fwb`（用环境内的绝对路径）。等待有两种方式：

- 以后台方式运行 `fwb wait <id>`，任务结束时 Claude Code 会收到通知，等待期间不产生任何模型调用；
- 前台运行 `fwb wait <id> --timeout 540`。Bash 工具单次最长 10 分钟，超时后再次调用即可。

实测：Claude Code 用阻塞等待委派一个任务，任务期间 host 的模型调用为 0 次。

### Codex 作为 host：MCP

Codex 的 shell 运行在沙箱里，网络和工作区外的写入都受限，沙箱内的 `fwb run` 无法启动能联网的 worker。MCP 服务进程由 Codex 在沙箱外启动，因此 Codex 应通过 MCP 使用本工具（实测：Codex 在沙箱完全不可用的情况下仍能经 MCP 委派成功）。在 `~/.codex/config.toml` 中加入：

```toml
[mcp_servers.fwb]
command = "/path/to/envs/fwbridge/bin/fwb"
args = ["mcp"]
# `codex exec` 无法弹出审批，不设置时 MCP 调用会被直接拒绝。
# 设为 "approve" 后 Codex 可以不经确认就启动 worker；交互使用时可改为 "prompt"。
default_tools_approval_mode = "approve"
# Codex 只把少数环境变量（HOME、PATH、USER 等）传给 MCP 服务。
# 凭据放在环境变量里时，必须在这里列出变量名，否则 worker 会报 "Not logged in"。
env_vars = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY"]
# 单次 task_wait 的时长须小于工具超时（Codex 默认 300 秒），见下一节。
tool_timeout_sec = 1000

[mcp_servers.fwb.env]
FWB_MCP_WAIT_MAX_S = "900"
```

提供的工具：`delegate`（启动任务，或带 `resume` 追加指令）、`task_wait`、`task_status`、`task_result`、`task_log`、`task_kill`。参数与命令行一致，限额放在 `delegate` 的 `limits` 对象里。`cwd` 必须是绝对路径。

Codex 等待 MCP 调用的机制会直接影响 host 的消耗，详见下一节。

### Codex 等待时的 token 消耗

Codex 0.167 在一个 JavaScript 代码单元里调用 MCP 工具。代码单元默认 30 秒后就把控制权交还模型，模型再用内置的 `wait` 每隔约一分钟查看一次。每次交还都是一次完整的模型调用，要重读 host 的整个上下文。实测一次 5 分钟的阻塞等待引发了 7 次模型调用。

本工具的对策：

- MCP 的 `task_wait` 没有超时参数，每次固定等到上限：默认 280 秒，低于 Codex 默认的 300 秒工具超时；按上面的配置为 900 秒。实测中模型会自行选择 50 秒这样的短等待，所以不把这个选择交给它。需要立即查看时用 `task_status`。
- `delegate` 的返回文字和 `task_wait` 的说明都会提示 Codex 在代码单元第一行写 `// @exec: {"yield_time_ms": ...}`，让代码单元在整个等待期间不交还控制权。实测 Codex 会照做。

实测数据：worker 由测试替身扮演，只统计 host 的调用；“整个委派”包括启动、等待、读取结果和最后的回复。

| 设置 | 任务时长 | 整个委派的 host 模型调用 | 等待期间被唤醒 |
|---|---|---|---|
| 允许模型自选等待时长（改进前） | 4 分钟 | 11 次 | 约每 50 秒一次 |
| 默认（单次 280 秒） | 4 分钟 | 6 次 | 0 次 |
| 推荐配置（单次 900 秒） | 10 分钟 | 5 次 | 0 次 |

任务比单次等待更长时，host 每过一个等待周期醒来一次。

Claude Code 也可以注册同一个服务（`claude mcp add --scope user fwb -- /path/to/envs/fwbridge/bin/fwb mcp`），但命令行配合后台等待已经足够。

### 结构化结果

默认要求 worker 按固定格式给出最终报告（定义在 `src/fwbridge/result_schema.json`）：

- `outcome`：`completed` / `partial` / `blocked` / `failed`，worker 对任务是否达成的自我判断；
- `summary`：做了什么、发现了什么；
- `files_changed`：改动的文件；
- `verification`：实际运行过什么来确认结果，没有验证则为 `none`；
- `remaining`：未完成的事项或受阻原因。

任务状态分两层：`status` 是进程层面的（`succeeded` 表示 worker 正常结束），`outcome` 是 worker 的自我判断。例如沙箱故障时 Codex 正常结束但报告 `blocked`。`outcome` 来自 worker 自述，重要的结论仍应由 host 自行核对。`--raw-result` 关闭结构化要求，此时只有自由文本，`outcome` 为空。

发给 worker 的提示词前会加一段固定说明（自主执行、不得再委派、受阻就停下并如实报告），完整输入保存在任务目录的 `worker_input.txt`。

### 上下文自动压缩

长任务不靠终止重开，而是让 worker 用自带的压缩功能在上下文达到阈值时总结历史并继续。`--autocompact TOKENS` 统一设置这个阈值：

- claude worker 对应 `claude --autocompact`，取值范围 10 万到 100 万；实际在略低于该值时触发（实测设 10 万时在 7.7 万到 11 万之间触发）。
- codex worker 对应 `-c model_auto_compact_token_limit=`，无下限。
- 默认 12 万；若 `--max-context-tokens` 不高于 12 万，则默认不设置。传 `0` 表示沿用 worker 自身的默认行为。
- 压缩次数显示在 `status` 的 `context compactions` 中。

压缩有两个限制。压缩会丢失细节，任务本身应把进度写入文件而不是只留在对话里。单步读入的内容过大时，压缩后会立刻再次填满，Claude Code 会检测到并以 "Autocompact is thrashing" 报错结束，任务状态为 `failed`。

`--max-context-tokens` 仍是硬性兜底：压缩没能把上下文控制住时终止任务。

### 自动续跑

worker 有时会在任务做完之前停下并报告 `partial`。`--max-rounds N` 让监督进程在这种情况下自动开一个新会话继续，最多共 N 个会话（默认 1，即不续跑）：

- 新会话收到原始任务加上一轮的结构化报告，被要求先检查工作目录的现状，再完成剩余部分；
- 只有 `partial` 会触发续跑，`blocked` 和 `failed` 直接返回给 host 判断；
- 所有限额（token、调用次数、成本、总时长）跨轮累计；
- `status` 显示当前轮次和各轮的结论，最终结果取最后一轮的报告。

### 追加指令

`fwb run --resume <id> -p "..."` 把新指令发进一个已结束任务的 worker 会话，worker 保留之前的上下文，适合“按你报告的问题继续修”这类后续请求。它会新建一个任务，`status` 中注明来自哪个任务；worker 和工作目录沿用原任务，不能更改。

两边 CLI 恢复会话后上报的 token 和成本都是整个会话的累计值，包含恢复之前的部分。本工具会扣除恢复前的基线，新任务只计自己的消耗，限额也只针对这部分。基线取不到时（例如原任务被强制终止，没有留下用量报告），总数会包含之前的消耗，`status` 中会注明。

恢复会话会把之前的整个上下文带回来，所以长会话的追加指令比新开任务贵。它不能与 `--max-rounds` 同时使用。

### 通知

`--notify COMMAND`（或环境变量 `FWB_NOTIFY_CMD`）指定一条 shell 命令，在任务结束、以及首次接近某项限额（达到 80%）时由监督进程执行。命令可用的环境变量：`FWB_EVENT`（`finished` 或 `warning`）、`FWB_MESSAGE`、`FWB_TASK_ID`、`FWB_TASK_NAME`、`FWB_STATUS`、`FWB_OUTCOME`。命令失败不影响任务，只在 `status` 中注明。

例如在 `~/.bashrc` 中设置 `export FWB_NOTIFY_CMD='echo "$(date +%T) $FWB_MESSAGE" >> ~/fwb-notify.log'`，或换成发消息的命令。

出于安全考虑，这个选项不通过 MCP 提供：否则沙箱里的 agent 可以借它在沙箱外执行命令。

### 限额

任一限额触发时，监督进程终止 worker 的整个进程组，任务状态记为 `killed` 并写明原因。达到限额的 80% 时在 `status` 中给出提示。传 `0` 关闭某项限额。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--max-context-tokens` | 200000 | 单次模型调用的输入（含缓存读写）上限 |
| `--max-total-tokens` | 10000000 | 所有调用的输入 + 输出 token 累计上限 |
| `--max-api-calls` | 200 | 模型调用次数上限 |
| `--max-cost-usd` | 无 | 成本上限（美元，仅 claude worker） |
| `--stall-timeout` | 1200 | 无任何事件的最长秒数 |
| `--wall-timeout` | 14400 | 任务总时长上限（秒） |

长任务成本失控的原因是每次调用都要重读全部历史，累计消耗随调用次数近似平方增长。自动压缩把单次调用的上下文维持在阈值以下，`--max-context-tokens` 作为兜底；两者的默认值（12 万和 20 万）都远低于 Claude Code 当前 100 万的上下文窗口。

### 权限

- claude worker 默认 `--permission-mode acceptEdits`。无头模式下未获授权的工具调用会被拒绝而不是挂起，被拒次数显示在 `status` 的 `permission denials` 中。用 `--allowed-tools` 预先授权需要的命令。
- codex worker 默认沙箱 `workspace-write`，可用 `--sandbox` 调整。
- 不会默认使用任何跳过权限检查的模式；需要时通过 `--permission-mode` / `--sandbox` / `--worker-arg` 显式指定。

### 保护机制

- 禁止嵌套委派：在 worker 内部再发起委派（无论通过命令行还是 MCP）会被拒绝，防止两个 agent 互相委派形成循环。检测依据是祖先进程中是否有本工具的监督进程，以及 `FWB_DEPTH` 环境变量；前者不依赖环境变量能否传到下层，因为 agent CLI 启动工具和 MCP 服务时可能会过滤环境变量。`FWB_MAX_DEPTH` 可放宽层数。
- 工作目录互斥：同一目录已有运行中的任务时拒绝启动新任务（`--allow-concurrent` 可覆盖）。

## 工作方式

没有常驻服务。每个任务对应一个目录（默认 `~/.local/state/fwbridge/tasks/<id>/`，可用环境变量 `FWB_HOME` 改位置）和一个脱离终端的监督进程：

```
task.json       任务请求（不可变）
prompt.txt      提示词
worker_input.txt  实际发给 worker 的输入（固定说明 + 提示词）
state.json      监督进程维护的实时状态
events.jsonl    worker 的原始事件流
usage.jsonl     每次模型调用一行的 token 记录
stderr.log      worker 的标准错误
result.md       最终报告（可读形式）
result.json     结构化结果
```

监督进程崩溃时任务显示为 `orphaned`，`fwb kill` 会清理残留的 worker。

### token 口径

- `input_uncached` / `cache_read` / `cache_write` / `output` 四项分开记录；`context` 为前三项之和，即单次调用模型实际读入的量。
- Claude：运行中输入侧 token 是精确值，输出 token 偏低（流式事件只带部分输出计数），压缩本身的那次调用也不在流里；任务正常结束后以 result 事件的权威总数覆盖。被终止的任务没有 result 事件，总数保持运行中的口径，成本为空。
- Codex：`codex exec --json` 只在整个 turn 结束时报告一次用量，实时数据来自 Codex 写在 `$CODEX_HOME/sessions` 下的会话文件。找不到该文件时只有结束时的总量，上下文大小记为未观测（不是 0），`status` 中会有说明。
- 成本只有 Claude 上报，按标价计算；订阅套餐下是名义值。Codex 侧显示订阅额度窗口的已用百分比。

## 已验证的跨平台场景（2026-11-10）

- Claude Code 或终端发起，Claude Code 执行：通过。
- 终端发起，Codex 执行（`--sandbox danger-full-access`）：通过，实时 token 记录与 Codex 结束时报告的总数一致。
- Codex 发起（`codex exec -s danger-full-access`），经 `fwb run / wait / result` 委派给 Claude Code：通过。
- Codex 在默认沙箱下（shell 不可用）经 MCP 委派给 Claude Code：通过，host 会话被识别并统计。
- Codex 作为 worker 时再调用 `fwb run`：被嵌套委派保护拒绝，退出码 2；经 MCP 调用 `delegate` 同样被拒绝，没有创建任务。
- 追加指令：两边 worker 都记得上一轮会话中的内容，新任务只计本轮 token。
- 结构化结果：Claude 完成任务报告 `completed`（退出码 0）；Codex 在沙箱故障下报告 `blocked`（退出码 3）。
- 自动压缩：Codex 阈值 3 万，压缩 2 次后完成任务。Claude 阈值 10 万，一个需要 50 次调用的顺序读取任务压缩 6 次后完成，单次调用上下文始终低于 7 万；不压缩时会增长到约 25 万。
- 自动压缩的失败情形：单步并行读入过多内容时，Claude Code 报 "Autocompact is thrashing"，任务状态为 `failed`。
- 自动续跑：Codex 第一轮报告 `partial`，第二轮新会话完成任务，token 跨轮累计并与 Codex 自报总数一致。

## 已知问题

在禁用了非特权用户命名空间的系统上（如 Ubuntu 默认的 `kernel.apparmor_restrict_unprivileged_userns=1`，开发机即如此），Codex 的 bubblewrap 沙箱无法启动。默认模式、`read-only` 和 `workspace-write` 下 Codex 执行不了任何命令和文件修改，只有 `danger-full-access`（无沙箱）可用。在这类系统上：

- Codex 作为 worker 需要加 `--sandbox danger-full-access`；
- Codex 作为 host 应通过 MCP 调用（见“接入 agent”），这条路径不受沙箱影响；若改用 shell 调用 `fwb`，需要以 `-s danger-full-access` 启动。

要恢复沙箱，需为 bwrap 配置 AppArmor 例外（需要 sudo）。

## 未覆盖的部分

- host 一侧只有监控，没有限额：host 的消耗由 host 自己的会话决定，本工具无法中止它。
- Claude Code 经 MCP（而不是命令行）作为 host 的路径未做真实测试，包括其 MCP 工具超时和环境变量传递。
- 只支持 Linux：进程检测和 host 识别依赖 `/proc`。

## 参考

- openai/codex-plugin-cc：Claude Code 调 Codex，经 Codex app-server 通信。
- sendbird/cc-plugin-codex：反方向，Codex 调 Claude Code，用 `claude -p` 加 `--json-schema` / `--resume`。

本工具的结构化结果做法参考了它们；重点放在它们之外的部分：两侧的 token 监控、限额和长任务的上下文控制。
