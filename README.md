# fwbridge (FireboyandWatergirl)

让 Claude Code 和 Codex 互相委派任务的本地工具：一方（host）通过 `fwb` 命令把任务交给另一方（worker）在后台执行，并对长任务和 token 消耗做监控与硬性限额。

当前状态：任务运行器、命令行、token 限额、结构化结果、上下文自动压缩、自动续跑已实现并用两边真实 CLI 验证。MCP 前端尚未实现，见文末路线图。

## 环境

```bash
mamba env create -f environment.yml   # 环境名 fwbridge，仅依赖标准库 + pytest
mamba run -n fwbridge python -m pytest -q
```

命令入口：激活环境后的 `fwb`，或直接用环境内的绝对路径（如 `~/miniforge3/envs/fwbridge/bin/fwb`），后者便于从另一个 agent 调用。

已验证的 CLI 版本：Claude Code 2.1.322、codex-cli 0.167.0。两者的事件格式变化较快，升级后先跑测试，再用真实任务核对一次。

## 用法

```bash
# 启动后台任务，立即返回任务 id
fwb run -w claude --cwd /path/to/repo -p "任务描述"
fwb run -w codex  --cwd /path/to/repo --prompt-file task.md

fwb status            # 最近任务列表
fwb status <id>       # 单个任务：状态、token、上下文、成本、最近动作
fwb wait <id> --timeout 600   # 阻塞等待，退出码见下
fwb log <id> [-f]     # worker 做过的事（工具调用、回复、上下文压缩）
fwb result <id> [--json]   # worker 的最终报告
fwb kill <id>         # 终止任务
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

host 是 agent 时推荐的调用方式：`fwb run` 之后用 `fwb wait --timeout` 阻塞等待，而不是反复轮询 `status`。等待期间 host 不产生模型调用，输出也只有几行，不会撑大 host 的上下文。

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

- 禁止嵌套委派：worker 进程带有 `FWB_DEPTH` 标记，在其中再调用 `fwb run` 会被拒绝（`FWB_MAX_DEPTH` 可调整），防止两个 agent 互相委派形成循环。
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
- Codex 作为 worker 时再调用 `fwb run`：被嵌套委派保护拒绝，退出码 2。
- 结构化结果：Claude 完成任务报告 `completed`（退出码 0）；Codex 在沙箱故障下报告 `blocked`（退出码 3）。
- 自动压缩：Codex 阈值 3 万，压缩 2 次后完成任务。Claude 阈值 10 万，一个需要 50 次调用的顺序读取任务压缩 6 次后完成，单次调用上下文始终低于 7 万；不压缩时会增长到约 25 万。
- 自动压缩的失败情形：单步并行读入过多内容时，Claude Code 报 "Autocompact is thrashing"，任务状态为 `failed`。
- 自动续跑：Codex 第一轮报告 `partial`，第二轮新会话完成任务，token 跨轮累计并与 Codex 自报总数一致。

## 已知问题

在禁用了非特权用户命名空间的系统上（如 Ubuntu 默认的 `kernel.apparmor_restrict_unprivileged_userns=1`，开发机即如此），Codex 的 bubblewrap 沙箱无法启动。默认模式、`read-only` 和 `workspace-write` 下 Codex 执行不了任何命令和文件修改，只有 `danger-full-access`（无沙箱）可用。在这类系统上：

- Codex 作为 worker 需要加 `--sandbox danger-full-access`；
- Codex 作为 host 需要以 `-s danger-full-access` 启动才能调用 `fwb`。

要恢复沙箱，需为 bwrap 配置 AppArmor 例外（需要 sudo）。

## 路线图

- MCP 服务前端：Codex 在沙箱模式下 shell 无法启动带网络的 `claude` 进程，需要经 MCP 发起委派。无沙箱模式下直接调用 `fwb` 已可用，所以只有恢复沙箱后才需要。
- 续跑：对已结束的任务追加指令（两边 CLI 都支持按会话 id 恢复）。
- 阈值通知、多任务总览视图。

## 参考

- openai/codex-plugin-cc：Claude Code 调 Codex，经 Codex app-server 通信。
- sendbird/cc-plugin-codex：反方向，Codex 调 Claude Code，用 `claude -p` 加 `--json-schema` / `--resume`。

两者都提供任务跟踪和结构化的审查结果，都没有 token 记账和限额；本工具的结构化结果做法与它们一致，监控部分是新增的。
