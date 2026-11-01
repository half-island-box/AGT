# agt CLI 命令参考

agt 命令行基于 [Click](https://click.palletsprojects.com/) 实现。

## 安装

```bash
# uv（推荐）
uv sync
uv run agt --help

# 或装成全局命令
uv tool install -e .
agt --help
```

---

## `agt probe`

探测本机已安装的 AI agent CLI，以表格形式展示。纯本地操作，不需要任何外部进程。

### 参数

| 参数          | 必填 | 默认值    | 说明                   |
| ------------- | ---- | --------- | ---------------------- |
| `--refresh` | 否   | `false` | 强制重新扫描，忽略缓存 |

### 示例

```bash
$ agt probe
🔍 Scanning for AI agent CLIs on this machine...

  Provider   Command        Path                                     Version      Status
  ──────────────────────────────────────────────────────────────────────────────────────────
  claude     claude         /home/user/.local/bin/claude             2.1.76       online

✅ Found 1 agent(s) on this machine.
```

### 探测策略

三层 fallback：

1. `shutil.which()` — 当前进程 PATH 查找
2. 环境变量 `AGT_<COMMAND>_PATH` — 显式覆盖路径
3. 登录 shell fallback（`bash -l -c 'echo $PATH'`，5 秒超时）

---

## `agt run`

直接本地 spawn agent 子进程，执行一次 prompt 后退出。**不依赖 Server 或 Daemon**，不保留 session。

### 参数

| 参数           | 简写   | 必填         | 默认值     | 说明                                                                                                           |
| -------------- | ------ | ------------ | ---------- | -------------------------------------------------------------------------------------------------------------- |
| `--prompt`   | `-p` | **是** | —         | 发送给 agent 的提示词                                                                                          |
| `--provider` | —     | 否           | `claude` | 指定 agent provider                                                                                            |
| `--identity` | —     | 否           | —         | identity DID。**要求 `--daemon`**（运行时解析在 Server）；binding 可选，不限制 identity 使用其他 provider |
| `--daemon`   | —     | 否           | 本地       | 通过 Server 定向到指定 daemon（远程执行）                                                                      |

### 示例

```bash
# 默认 provider (claude) — 本地流式输出（Phase 5）
$ agt run -p "say hi"
🚀 Executing with claude...
Hi there, friend!      # ← 实时逐 token 打印，不再等执行完一次性吐
✅ Task completed in 2.9s (exit_code=0, session_id=8beeaed9-fc72-...)

# 指定 provider（codex 通过 app-server 同样流式输出 thinking / 工具调用 / 用量）
$ agt run --provider codex -p "..."

# OpenCode：run --format json，文本按消息段输出，thinking / 工具调用 / 用量同样上报
$ AGT_OPENCODE_MODEL=deepseek/deepseek-v4-flash agt run --provider opencode -p "..."

# 带 identity 远程执行（binding 可选，按所选 provider 查找 live runtime）
$ agt run --identity did:agt:reviewer --daemon d1 -p "review this diff"

# identity 没有 codex binding 也可以运行，只要目标 runtime live
$ agt run --identity did:agt:reviewer --provider codex --daemon d1 -p "..."
```

> **`--identity` 语义**：binding 是可选的运行时路由偏好。匹配的 live binding 优先；没有匹配 binding 时，Server 会从 live runtime 注册表选择显式指定的 `(daemon, provider)`。binding 不会限制 identity 可用于哪些 provider。
> **本地限制**：不带 `--daemon` 时 `--identity` 会被拒绝，因为 identity 的 runtime 解析和注入内容都由 Server 提供。
> **本地流式（Phase 5）**：Claude Code 默认启用 `--output-format stream-json --verbose`；codex 通过 `codex app-server`（JSON-RPC over stdio）驱动同一事件通道；OpenCode 通过 `opencode run --standalone --format json --thinking` 输出 JSON-Lines 事件。thinking / 工具调用 / token 用量都会实时上报，`TaskResult.stdout` 仍存拼接后的纯文本，下游展示 / 落库链路零改动。OpenCode 的文本按完整消息段到达（不是逐 token），相邻段之间自动空行。旧版 codex 没有 `app-server` 时自动回退到 `codex exec` pipe 文本；可用环境变量 `AGT_CODEX_TRANSPORT=exec|app-server` 强制选择。OpenCode 用 `AGT_OPENCODE_MODEL=<provider/model>` 指定模型（headless 环境没有 UI 选模型）。
> **远程流式（Phase 5 remote）**：远程 `--daemon` / `--identity` 路径同样实时流式——daemon 每 ~0.5s 采样一次累计 stdout 前缀，经 WS/HTTP 上报到 Server，Server 再以 SSE 推给 CLI 逐字打印（`snapshot → delta* → done`）。读超时自动重连一次，仍无进展则降级为 `wait_task` 轮询兜底（兜底只补打未打印的尾部，不整段重打）。

### 退出码

| 退出码 | 含义                                            |
| ------ | ----------------------------------------------- |
| `0`  | 任务成功完成                                    |
| `1`  | probe 未找到指定 provider                       |
| `2`  | 任务失败（非零 exit code、超时、resume 被拒绝） |

---

## `agt chat`

交互式对话模式。每轮输入独立 spawn 一个 agent 子进程，同一 `task_id` 下的 session 自动续接，实现跨轮上下文连续性。**不依赖 Server 或 Daemon**（除非指定 `--daemon`）。

Session 分两种存储：

- **本机直连 chat（不带 `--daemon`）**：这是唯一使用 `<cwd>/.agt/sessions.json` 的路径。每个 `agt chat` 会话是文件里的一个独立条目，带 `provider` / identity / 首条 prompt / 轮数等本地索引信息；`--list`、`--resume` 和多轮续聊都由它支持。
- **Server 管理的 chat（`--daemon`，包括指向本机 daemon）**：SQLite 是唯一会话信息源，不读取或写入任何 `sessions.json`。SessionModel 保存 `(job_id, identity, provider) → session_id`；查看和恢复分别通过 Server 的 run/session API，daemon 只执行任务和读取 provider 原生历史。

远程 chat 现在同样流式输出——agent 文本经 SSE 增量打印到终端（见上「远程流式」）。

**注意**：bare `agt chat` **不会**在 `<cwd>/.agt/tasks/` 下创建 `chat-xxx/` 子目录。Agent 子进程直接跑在用户 cwd；仅本机直连 chat 使用 `<cwd>/.agt/sessions.json`。需要显式 meta 目录时再加 `--work-dir`。

### 参数

| 参数              | 简写 | 必填 | 默认值                    | 说明                                                                                |
| ----------------- | ---- | ---- | ------------------------- | ----------------------------------------------------------------------------------- |
| `--provider`    | —   | 否   | 自动探测                  | 指定 provider（省略时自动选第一个可用）；配合`--list` 时按 agent 过滤 session     |
| `--daemon`      | —   | 否   | 本地                      | 通过 Server 定向到指定 daemon（远程 chat）                                          |
| `--server-url`  | —   | 否   | `http://127.0.0.1:8420` | Server 地址（配合`--daemon`）                                                     |
| `--work-dir`    | —   | 否   | —                        | 在指定路径创建 AGT 元数据目录（bare chat 默认不创建）；不改 agent 的 cwd            |
| `--no-work-dir` | —   | 否   | —                        | 使用 legacy 布局`~/.agt/workspaces/<provider>/<id>/`                              |
| `--identity`    | —   | 否   | —                        | identity DID；每轮 turn 都携带。**要求 `--daemon`**（运行时解析在 Server；binding 可选） |
| `--list`        | —   | 否   | —                        | 列出 session 并退出。不带 `--daemon`：当前 cwd 的本地 session（含 `PROVIDER` 列，可配 `--provider` 过滤）；带 `--daemon=X`：按 daemon 侧 cwd 过滤的远程 session（含 `DIR` 列）。本机 daemon 自动用当前 cwd；远端 daemon 需同时传 `--dir` |
| `--resume`      | —   | 否   | —                        | 按 session id 恢复会话（打印历史 + 子进程`--resume` 续接）。本地从 `<cwd>/.agt/sessions.json` 取 provider/identity；远程（`--daemon`）经 Server 反查。不传 `--provider`/`--identity` 时自动用记录值；显式传 `--identity` 且与记录不一致时报错（记录为空则接受） |
| `--no-history`  | —   | 否   | —                        | 配合`--resume`：跳过打印历史，仅恢复                                              |
| `--dir`         | —   | 否   | 见下文                   | agent 子进程的 cwd（IDENTITY.md + 标记块的注入位置）。本机 daemon 自动用当前目录；远端 daemon 必须显式传一个 daemon 能访问的绝对路径（远程 `--resume` 会自动复用记录里的 dir） |
| `--file`        | —   | 否   | —                        | 给**第一轮**对话附加文件（可重复，最多 10 个）。带 `--daemon` 时先上传到 Server，再以 `agt-upload://` 引用随消息下发；本地直连模式直接把路径交给 agent。daemon 侧落到 `<agent_cwd>/.agt/inputs/<原文件名>` |
| `--show-tools`  | —   | 否   | 关闭                      | 流式过程中把工具调用打印成单行注解：`🔧 <工具名>: <关键参数>`，结果行以 `↳`（成功）或 `✗`（失败）开头。本地直连与远程（`--daemon`）均生效 |
| `--show-thinking` | — | 否   | 关闭                      | 流式打印模型的 thinking 内容（`💭` 前缀）。本地直连与远程（`--daemon`）均生效 |
| `--usage`       | —   | 否   | 关闭                      | 每轮结束后打印 token / 缓存命中 / 费用页脚，例如 `[usage] claude-sonnet-4-5 · in 12,400 · out 860 · cache hit 91% · $0.0021`（`in` 为计费输入 = 输入 + 缓存读 + 缓存写）。codex 只上报 token 用量、不上报费用，页脚省略 `$` 段。旧 Server 只在终帧下发用量，CLI 会自动兜底且只打印一次 |

### 示例

```
$ agt chat
🚀 agt chat mode (provider=claude, task=chat-797dfdf6). Type 'exit' or 'quit' to leave.

agt> 帮我看看 main.py
... (Claude 响应，session 被持久化)

agt> 再加个 --debug 参数
... (通过 --resume 延续上一轮的上下文)

agt> exit
👋 Bye!
```

给出第一轮对话附加文件（`--file` 只作用于第一轮；daemon 侧落到 `<agent_cwd>/.agt/inputs/<原文件名>`）：

```
$ agt chat --daemon=daemon-cloud-01 --dir=/srv/app-a \
    --file ./logs/app.log --file ./docs/需求.md
🚀 agt chat mode (provider=claude, daemon=daemon-cloud-01, dir=/srv/app-a, task=chat-…). ...

agt> 分析这两个文件，指出所有 5xx 的原因
```

查看 agent 的思考过程、工具调用与每轮用量（三个开关都可单独使用）：

```
$ agt chat --show-thinking --show-tools --usage
🚀 agt chat mode (provider=claude, task=chat-…). Type 'exit' or 'quit' to leave.

agt> 跑一下测试看看哪些挂了
💭 先确认测试入口 …
🔧 Bash: command=pytest -q
   ↳ 3 failed, 120 passed
🔧 Read: file_path=agt/cli/main.py
   ↳ 8 lines …
...
[usage] claude-sonnet-4-5 · in 12,400 · out 860 · cache hit 91% · $0.0021
```

`--show-tools` 只打印单行摘要（工具名 + `command` / `file_path` 等关键参数 + 结果首行），完整输入不会刷屏；结果首行超过 120 字符会被截断，失败结果以 `✗` 标记。

列出当前路径下的历史会话（含 provider 标记），再按 id 恢复其中一个：

```
$ agt chat --list
2 session(s) in /home/user/project-x/.agt/

  SESSION                 PROVIDER  LAST USED   TURNS  FIRST PROMPT
  ────────────────────────────────────────────────────────────────
  abc-1234…               claude    5 min ago   3     帮我看看 main.py
  def-5678…               codex     2 days ago  1     加个日志

$ agt chat --list --provider codex
1 session(s) in /home/user/project-x/.agt/

  SESSION                 PROVIDER  LAST USED   TURNS  FIRST PROMPT
  ────────────────────────────────────────────────────────────────
  def-5678…               codex     2 days ago  1     加个日志
```

查看本机 daemon 上当前 cwd 的会话（按 cwd 过滤，带 `DIR` 列）：

```
$ agt chat --list --daemon=my-local-daemon
2 session(s) on daemon 'my-local-daemon' in /home/user/project-x

  SESSION                 PROVIDER  DIR                       UPDATED     FIRST PROMPT
  ────────────────────────────────────────────────────────────────────────────────
  abc-1234…               claude    /home/user/project-x      5 min ago   帮我看看 main.py
  def-5678…               codex     /home/user/project-x      2 days ago  加个日志
```

远端 daemon 必须带 `--dir` 指定 daemon 侧目录：

```
$ agt chat --list --daemon=daemon-cloud-01 --dir=/srv/app-a
1 session(s) on daemon 'daemon-cloud-01' in /srv/app-a

  SESSION                 PROVIDER  DIR          UPDATED     FIRST PROMPT
  ─────────────────────────────────────────────────────────────────────
  abc-1234…               claude    /srv/app-a   5 min ago   帮我看看 main.py
```

恢复一台远程 daemon 上的会话（provider / identity / dir 由 Server 反查，task_id 复用；历史经 Server 从 daemon 拉取后回显）：

```
$ agt chat --resume abc-1234… --daemon=daemon-cloud-01
─── history of abc-1234… (6 message(s)) ───
> user: 帮我看看 main.py
· assistant: ...
─── end history ───

🚀 agt chat mode (provider=claude, daemon=daemon-cloud-01, resume=abc-1234…, task=chat-…). ...
agt>
```

```
$ agt chat --resume abc-1234…
─── history of abc-1234… (6 message(s)) ───
> user: 帮我看看 main.py
· assistant: ...
─── end history ───

🚀 agt chat mode (provider=claude, task=chat-…, resume=abc-1234…). ...
agt>
```

### `--dir` 与 daemon 跨机

`--dir` 控制 agent 子进程的 cwd（身份注入的目标目录）。带 `--identity` 时注入两个产物：`IDENTITY.md`（身份全文副本）+ 标记块（一行简述 + 指向 IDENTITY.md）。**标记块写入的文件按 provider 区分**：`claude` 写 `CLAUDE.md`（Claude Code 原生只读 CLAUDE.md），其他 provider 写 `AGENTS.md`。任务结束/崩溃时全部回收。三种情形：

| 情形 | `--dir` | `--daemon` | 实际 agent_cwd |
|---|---|---|---|
| 本机模式（无 `--daemon`） | 不传 | 不传 | 用户 cwd（不变） |
| 本机 daemon | 不传 | 本机 daemon id | 自动用用户 cwd |
| 远端 daemon | **必须**显式传 | 远端 id | `--dir` 路径（daemon 必须能访问） |
| 远端 daemon | 不传 | 远端 id | **报错退出**，提示传 `--dir` |

本机判定逻辑（两步）：

1. **探活（链 A）**：读 `~/.agt/daemon.pid`，`os.kill(pid, 0)` 确认进程存活，且其 `/proc/<pid>/cmdline` 仍是 agt daemon（防 pid 复用）——与 `agt daemon status` 用同一套证据。
2. **身份匹配（链 B）**：从 `~/.agt/daemons` 找本机 entry（端口匹配 `AGT_DAEMON_WS_PORT`，默认 8422，**且 host 是本机**——`0.0.0.0`/`127.0.0.1`/`localhost` 或本机网卡 IP），`uuid5(NAMESPACE, url)` 算出 daemon id，与 `--daemon` 比对一致才算本机。

`agt daemon start` 启动时会自动向 `~/.agt/daemons` 追加本机 entry（`local ws://0.0.0.0:8422`，幂等）。所以只要 daemon 是通过 `agt daemon start` 起的，链 B 就能命中。注意 `~/.agt/daemon_id` 文件机制已移除（它几乎从不被写，之前的判定因此失效）。

**远端 daemon 必须传 `--dir`** —— 远端机器看不到用户的本机路径，传一个 daemon 能访问的绝对路径（典型场景：通过 SSHFS / NFS 挂载，或路径语义一致）。唯一例外是远程 `--resume <sid> --daemon=X`：此时 dir 由 Server 反查该 session 当初的 `agent_cwd` 自动复用，无需再传 `--dir`。

**安全约束**：daemon 侧独立校验 `agent_cwd` 是否在 `AGT_AGENT_CWD_ALLOWLIST` 之内（默认 `[Path.home()]`，允许多个冒号分隔根，`/` 表示任意）。CLI 校验通过不等于 daemon 放行。

### 退出方式

- 输入 `exit` / `quit` / `:q`
- `Ctrl+D`（EOF）
- `Ctrl+C`（KeyboardInterrupt）

---

## `agt server start`

在 `127.0.0.1` 上启动 FastAPI Server。Server 是 agent registry、task queue、session DB 的统一 hub，SQLite 持久化。

Server 同时在同源端口提供浏览器控制台：

```text
http://127.0.0.1:8420/
```

控制台先进入 Projects 页面。用户可以添加、重命名、移除目录项目，并从项目进入 Chat、SOP 和 Goal；添加项目时“浏览”会由 loopback Server 打开本机系统目录选择器，也可以手动输入绝对路径。Settings 负责 Identity 与运行时偏好。页面复用与 VS Code 客户端相同的 HTTP/SSE API，生产运行不需要单独启动 Node 前端进程。控制台默认只随 loopback Server 开放；绑定非 loopback 地址时默认禁用。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGT_CONSOLE_ENABLED` | `1` | 是否允许托管浏览器控制台 |
| `AGT_CONSOLE_WORKSPACE_ROOTS` | 用户 home | 使用系统 path separator 分隔，控制可浏览/编辑的 SOP 工作区根 |
| `AGT_CONSOLE_ASSETS` | Python 包内 `agt/web_console/assets` | 控制台构建产物目录 |
| `AGT_CONSOLE_ALLOW_REMOTE` | `0` | 非 loopback 监听时允许控制台；仅应在带认证的反向代理后开启 |

启动时自动初始化数据库并创建 `runtimes`、`tasks`、`sessions` 等表。Server 不再读取或写入 `sessions.json`；旧的 `~/.agt/sessions.json` 仅保留为历史迁移输入。

**MCP 工具（运行时目录 + Goal spawn）**：Server 启动时在同一端口挂载 Streamable HTTP 端点 `http://127.0.0.1:8420/mcp/`。`list_runtimes` 用于查询在线 daemon/provider；`spawn_subagents` 只允许运行中的 Goal Step 调用，服务端通过 `Authorization: Bearer ${AGT_TASK_TOKEN}` 反查当前 Step 和 Job，不接受 agent 自报 parent job。每次调用会立即创建子 Job，并通过 `(parent_step_id, idempotency_key)` 防止重试重复创建。远程 daemon 的 loopback 代理会把工具调用和 token 经 reverse-dial 转发到 Server。Claude Code 使用项目 `.mcp.json`，Codex 使用全局 `~/.codex/config.toml` 的 `[mcp_servers.agt-catalog]` 和 `bearer_token_env_var = "AGT_TASK_TOKEN"`，OpenCode 使用项目 `opencode.json` 的 `mcp.servers.agt-catalog`（`type: "remote"`，header 里写 `Bearer {env:AGT_TASK_TOKEN}`）；未接 MCP 的 provider 继续使用 `AGT-SPAWN`。给新 provider 接 MCP 见 [provider-registration](provider-registration.md)。

**Phase 3 起，Server 与 Daemon 必须使用同一个 `AGT_SERVER_SECRET`**。`agt server start` 首次运行会自动生成并写入 `~/.agt/server.secret`（`0600`），Server 和 Daemon 都按“环境变量 > `~/.agt/server.secret` > 空”加载，因此正常启动时无需 export：

```bash
$ agt server start
Generated server secret at /home/user/.agt/server.secret
INFO:     Uvicorn running on http://127.0.0.1:8420 (Press CTRL+C to quit)
```

想显式生成或轮换 secret 时用 `agt server init`（`--force` 覆盖已有值）。

远程机器接入流程见 [`agt daemon invite`](#agt-daemon-invite--agt-daemon-init)。

### 参数

| 参数                 | 简写 | 必填 | 默认值                | 说明                                        |
| -------------------- | ---- | ---- | --------------------- | ------------------------------------------- |
| `--host`           | —   | 否   | `127.0.0.1`         | 绑定地址                                    |
| `--port`           | —   | 否   | `8420`              | TCP 端口                                    |
| `--db-path`        | —   | 否   | `~/.agt/agt.db`     | SQLite 数据库文件路径                       |
| `-d`, `--detach` | —   | 否   | 关闭                  | 后台运行：父进程立即返回，server 转到子进程 |
| `--log-file`       | —   | 否   | `~/.agt/server.log` | detach 模式下的日志文件路径                 |
| `--pid-file`       | —   | 否   | `~/.agt/server.pid` | detach 模式下的 pid 文件路径                |

**默认模式（前台）**：父进程阻塞，Ctrl-C 停，日志打到当前 tty。

```bash
$ agt server start
INFO:     Uvicorn running on http://127.0.0.1:8420 (Press CTRL+C to quit)

$ agt server start --port 9000 --db-path /tmp/agt-test.db
```

**`--detach` 模式**：父进程 fork 子进程后立即 exit 0，终端立刻可用；子进程写
`~/.agt/server.pid`（detach 模式下才写），日志落 `~/.agt/server.log`。关闭用
`agt server stop`。

**自动替换旧进程**：`--detach` 启动时会先检查 pid 文件——若已有正在运行的
server（例如仍加载旧版本代码的进程），会先发送 SIGTERM 并等其退出，再启动
新进程。这解决了"改代码后必须手动 stop 再 start"的痛点：直接 `agt server
start -d` 即可热替换。

```bash
$ agt server start -d
Server started in background (pid=48201, log=/home/user/.agt/server.log)

# 终端立刻可用，继续做别的
$ agt daemon start -d          # 起 daemon 连到这个 server
$ agt task submit -p "hello"

# 查看 / 停止
$ agt server status
Server is running, pid=48201, pidfile=/home/user/.agt/server.pid
Console: http://127.0.0.1:8420/
$ agt server stop
Sent SIGTERM to server pid 48201
```

> `agt server status` / `agt server stop` 默认读 `~/.agt/server.pid`；也可用
> `--pid-file <path>` 指定（或读 `AGT_SERVER_PID_FILE` 环境变量），用于非默认
> 路径的 detach server。

### 浏览器控制台（Web console）

Server 启动后即可用浏览器访问 `http://127.0.0.1:8420/console/`（项目、Chat、
SOP、Goal、设置）。页面 bundle 随 Python 包一起发布，**安装版不需要 `npm`**。

从包含 `vscode/` 源码的 checkout 启动时，若 `agt/web_console/assets` 里的
bundle 缺失（新克隆、切分支），`agt server start` 会在启动前自动在 `vscode/`
工作区执行一次 `npm run build:console`，失败也不阻塞 Server 启动（页面返回
503，日志里给出原因）。相关环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGT_CONSOLE_ASSETS` | 包内 `agt/web_console/assets` | 覆盖 bundle 目录 |
| `AGT_CONSOLE_AUTOBUILD` | 开启 | 设为 `0` / `false` / `no` / `off` 关闭自动构建 |
| `AGT_CONSOLE_SOURCE_DIR` | 自动向上查找 `vscode/` | 指定可构建控制台的 `vscode/` 工作区 |

修改 webview 源码后仍需手动重新构建并提交 bundle。

### Daemon 接入方式（Phase 3.2+）

- **本地 daemon**（与 Server 同一台机器）继续走 HTTP，不需要 WS。
- **远程 daemon**（跨机器）通过 WebSocket 接入：

  ```
  ws://<server-host>:8420/ws/daemon
  Header: X-Daemon-Token: <signed token>
  ```

  握手成功后会立刻收到一帧 `{"type":"heartbeat_ack","rpc_v1":true}` 作为能力协商；
  之后所有调用都是单连接多路复用的 JSON-RPC（按 `request_id` 区分）。
  WS 路径是为远程 daemon 准备的——主要目的就是 `WSRPCUncertain` 的安全窗口
  （server timeout + grace），防止瞬时断连导致的重复 claim。
  本地 daemon 不需要这条路径。

### API 文档

Server 运行时访问 [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs) 查看 FastAPI 自动生成的 OpenAPI 文档。

---

## `agt daemon`

后台 Daemon 生命周期管理。Daemon 常驻运行，连接 Server、注册本机 agent、循环 claim 任务、spawn agent 子进程执行、report 结果。

### `agt daemon start`

启动后台 Daemon。

| 参数                 | 简写 | 必填 | 默认值                                                      | 说明                                        |
| -------------------- | ---- | ---- | ----------------------------------------------------------- | ------------------------------------------- |
| `--server-url`     | —   | 否   | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址                                 |
| `-d`, `--detach` | —   | 否   | 关闭                                                        | 后台运行：父进程立即返回，daemon 转到子进程 |
| `--log-file`       | —   | 否   | `~/.agt/daemon.log`                                       | detach 模式下的日志文件路径                 |

**默认模式（前台）**：父进程阻塞，Ctrl-C 杀进程，输出打到当前 tty。

```bash
$ agt daemon start
2026-08-09 ... Registered runtimes with Server
# Ctrl-C 退出
```

**`--detach` 模式**：父进程 fork 子进程后立即 exit 0，终端立刻可用；daemon 在后台常跑，子进程写 `~/.agt/daemon.pid`。关闭 daemon 用 `agt daemon stop`。

**自动替换旧进程**：`--detach` 启动时会先检查 pid 文件——若已有正在运行的 daemon，
会先发送 SIGTERM 并等其退出，再启动新进程（否则子进程的 pidfile 守卫会直接拒绝启动）。

```bash
$ agt daemon start -d
Daemon started in background (pid=48201, log=/home/user/.agt/daemon.log)

# 终端立刻可用，继续做别的
$ agt task submit -p "hello"

# 查看 / 停止
$ agt daemon status
Daemon is running, pid=48201, pidfile=/home/user/.agt/daemon.pid
$ agt daemon stop
Sent SIGTERM to daemon pid 48201
```

> **提示**：detach 模式下 daemon 注册到 server 有几百 ms 延迟；立刻提交任务可能需等一会儿，或用 `agt daemon status` 确认在跑。`--log-file` 默认 `~/.agt/daemon.log`，可指定其他路径供集中收集。

### `agt daemon stop`

向运行中的 Daemon 发送 SIGTERM，等待其清理 runtime 注册信息后退出。

```bash
$ agt daemon stop
Sent SIGTERM to daemon pid 3799202
```

### `agt daemon status`

检查 Daemon 是否在运行。

```bash
$ agt daemon status
Daemon is running, pid=3799202, pidfile=/home/user/.agt/daemon.pid
```

### `agt daemon invite` / `agt daemon init`

把「远程机器接入 reverse-dial」压缩成**复制一行命令**。`invite` 在 Server 机器上生成邀请码，
`init` 在 daemon 机器上消费它；中间不需要手抄 secret、也不需要对齐两份 `~/.agt/daemons` 的 URL 拼写。

邀请码格式为 `agt1.<base64url>`，内含 `{v, secret, url, id, name, ws_port, server_url?}`。
**它等同于 SSH 私钥**——拿到的人就能以该 daemon 身份接入你的 Server。

#### `agt daemon invite`

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--name <NAME>` | 是（或 `--existing`） | daemon 标签，如 `cloud-01`；只允许 `[A-Za-z0-9_.-]` |
| `--url <WS_URL>` | 是（或 `--existing`） | daemon 的公网地址，`ws://` 或 `wss://` |
| `--existing <NAME>` | 否 | 复用 `~/.agt/daemons` 里已有的条目，省掉 `--url` |
| `--daemon-id <UUID>` | 否 | 显式指定 id；默认沿用条目里的 `id=`，否则生成新的 uuid4 |
| `--add` | 否 | 同时把目标写入本机 `~/.agt/daemons`（带 `id=`） |
| `--force` | 否 | 允许覆盖「同名但 URL 不同」的已有条目 |
| `--server-url <URL>` | 否 | 写进邀请码的 Server 地址（`AGT_USE_WS_CLAIM=1` 的 HTTP 模式 daemon 才需要） |

```bash
$ agt daemon invite --name cloud-01 --url ws://1.2.3.4:8422 --add
Added cloud-01 ws://1.2.3.4:8422 (id=8f3c1d0e-…) in /home/user/.agt/daemons

Run this on the daemon machine:

    agt daemon init agt1.eyJ2IjoxLCJpZCI6Ii4uLn0

The code embeds your server secret. Treat it like an SSH private key.
```

- 需要已配置 server secret（`AGT_SERVER_SECRET` 或 `~/.agt/server.secret`），否则退出码 1。
- 不加 `--add` 时**只打印**邀请码，Server 端不会被配置去拨这台机器。
- `--add` 只写 `~/.agt/daemons`，不会立刻拨号。让运行中的 Server 立即读取新目标：
  重启 Server，或调用 `POST /api/daemons/refresh`（设置页的刷新按钮走的就是这个端点）。

#### `agt daemon init`

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `INVITE_CODE` | 否 | `agt daemon invite` 打印的整行 `agt1.<…>` |
| `--token <SECRET>` | 无邀请码时必填 | Server secret，等同于邀请码里的 `secret` |
| `--url <WS_URL>` | 无邀请码时必填 | 本机公网地址 |
| `--name <NAME>` | 否 | daemon 标签；默认取邀请码里的名字，其次主机名 |
| `--daemon-id <ID>` | 否 | 显式 id（必须是 UUID）；默认取邀请码里的 id，其次 `uuid5(url)` |
| `--ws-port <PORT>` | 否 | 监听端口；默认取邀请码里的值，其次 URL 里的端口，否则 8422 |
| `--force` | 否 | 允许覆盖本机已有的、内容不同的 server secret |
| `--start` | 否 | 配置完成后立即 `agt daemon start --detach` |

```bash
$ agt daemon init agt1.eyJ2IjoxLCJpZCI6Ii4uLn0
Wrote server secret to /home/user/.agt/server.secret
Configured cloud-01 (ws://1.2.3.4:8422, id=8f3c1d0e-…) in /home/user/.agt/daemons

Next: agt daemon start --detach
Make sure this machine's port 8422 is reachable from the Server.
```

写入的三个文件：

| 文件 | 内容 |
| --- | --- |
| `~/.agt/server.secret` | 与 Server 相同的 HMAC secret，权限 `0600` |
| `~/.agt/daemon.json` | `daemon_id`、`daemon_url`、`listen_mode: true`、`ws_port`（有则 `server_url`） |
| `~/.agt/daemons` | 本机条目 `name url id=<uuid>`，供 `agt daemon status` 等本地命令解析自身 |

- 已有 secret 且与新值不同时拒绝覆盖（退出码 1），避免把已配好的机器改坏；`--force` 才替换。
- 退出码：`0` 成功；`1` 文件写入/冲突错误；`2` 邀请码非法或缺参数。

#### 等价的手工方式

```bash
# daemon 机器
$ export AGT_SERVER_SECRET=xxx
$ export AGT_LISTEN_MODE=1
$ export AGT_DAEMON_WS_PORT=8422
$ agt daemon start
```

或持久化到 `~/.agt/daemon.json`（重启后仍生效）：

```bash
$ agt daemon config set --listen-mode --ws-port 8422
```

Server 侧把目标写进 `~/.agt/daemons`（见 [quick_start §3](quick_start.md#3-反向连接nat-穿透远程-daemon-推荐)）。

#### `agt daemon remove`

撤下本机登记的一台远程 daemon：从 `~/.agt/daemons` 删除对应条目（其余行与注释原样保留）。

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `TARGET` | 是 | 条目名称（如 `cloud-01`），或该条目的 `ws://` 地址 |

```bash
$ agt daemon remove cloud-01
Removed cloud-01 ws://1.2.3.4:8422 from /home/user/.agt/daemons
Restart the server (`agt server start`), or POST /api/daemons/refresh, for the change to take effect.
```

- 找不到匹配条目时退出码 1，文件不变。
- 运行中的 Server 在下一次 `POST /api/daemons/refresh`（或重启）时停止拨号并把它从列表里剔除。
  通过设置页 / `DELETE /api/pairing/daemons` 移除时，Server 会**立即**关闭该目标的
  DaemonLink，不再向它派发任务。

#### Server API（前端设置页用）

设置页的「连接与配对」区块调用这几个端点，等价于上面的 CLI：

| 端点 | 作用 |
| --- | --- |
| `GET /api/pairing/state` | 已配置的 daemon 列表（含 `id`、`connected`）+ secret 是否存在 |
| `GET /api/pairing/secret?reveal=true` | 取 secret；默认只返回掩码 `masked` |
| `POST /api/pairing/daemons` | 登记新目标并返回邀请码（body：`name`、`url`、可选 `capabilities`） |
| `DELETE /api/pairing/daemons?name=&url=` | 从 daemons 文件移除目标并停止其 DaemonLink；返回 `disconnected` / `restart_required` |
| `GET /api/pairing/invite?name=&url=` | 为**已登记**的目标生成邀请码；未登记返回 409 |

整个 `/api/pairing/*` 前缀**强制 loopback-only**：来源 IP 不是 `127.0.0.1` / `::1` 一律 403，
即使设置了 `AGT_CONSOLE_ALLOW_REMOTE=1` 也不放行（这些接口会返回主 secret）。
`POST /api/pairing/daemons` 只写文件，响应里的 `restart_required` 标明“还需让运行中的 Server 重新读一次”；
随后调用 `POST /api/daemons/refresh` 即可让新目标立刻开始拨号（无需重启）。
`DELETE /api/pairing/daemons` 会立即断开该目标的 DaemonLink（`disconnected: true`），
但它仍会以离线状态出现在 `/api/daemons` 列表里，直到刷新或重启 Server（`restart_required: true`）。

#### 刷新 daemon 目标（`POST /api/daemons/refresh`）

重新读取 `~/.agt/daemons` 并就地调和运行中的反向连接，无需重启 Server：

```http
POST /api/daemons/refresh[?daemon_id=<id>]
```

| 字段 | 说明 |
| --- | --- |
| `daemons` | 每个已登记目标一行：`{daemon_id, action, connected}` |
| `daemons_path` | 本次读取的文件路径 |

`action` 取值：`added`（新目标，已开始拨号）、`replaced`（URL 变了，旧链路停止后重建）、
`removed`（文件里已删除，链路已停止）、`redialled`（原本离线，已立即重连，不再等退避）、
`unchanged`（链路健康，未触碰——不会打断进行中的 claim/report）。

- 可选的 `daemon_id` 只把**强制重连**收窄到该目标；文件本身是全局的，登记表总是整体重读。
- 接口最多等待 1.5 秒让重连结果落地，因此返回的 `connected` 是真实状态而不是“刚开始尝试”。
- `~/.agt/daemons` 不存在时**保留**当前链路（路径写错、家目录未挂载都不该把所有 daemon 摘掉）。
- 服务端没有启动 daemon 链路池时返回 503（`detail.type = daemon_pool_unavailable`）。

设置页「Daemon」列表每行右侧的刷新按钮调用该端点（带 `daemon_id`），
VS Code 侧栏服务器视图标题栏的刷新按钮调用无参数的版本（重新扫描全部目标）。

### `agt daemon config`

管理 Daemon 的执行资源持久化配置。配置保存在 `~/.agt/daemon.json`（权限 `0o600`），
仅在 **Daemon 下次启动时**生效；正在运行的 Daemon 不会热重载，需要重启。

优先级（由低到高）：

1. 内置默认值
2. `~/.agt/daemon.json`
3. `AGT_*` 环境变量

也就是说，当前 shell 里已 `export` 的环境变量会覆盖配置文件里的值。

#### `agt daemon config show`

显示下一次启动时真正生效的值、来源、已保存值和默认值。

```bash
$ agt daemon config show
Configuration file: /home/user/.agt/daemon.json
max concurrent tasks: 4  source: file
agent timeout (seconds): 1800  source: file
work directory: /home/user/.agt/workspaces  source: default
agent cwd allowlist: /home/user/projects:/home/user/experiments  source: env AGT_AGENT_CWD_ALLOWLIST  saved: ["/home/user/work"]
listen mode (reverse-dial): yes  source: file  default: no
ws listen port: 8422  source: file  default: 8422
daemon id: 8f3c1d0e-…  source: file
daemon url: ws://1.2.3.4:8422  source: file
```

- `source: default` 表示使用内置默认值；
- `source: file` 表示来自 `~/.agt/daemon.json`；
- `source: env <VAR>` 表示当前环境变量覆盖；此时还会显示 `saved:`，让你知道文件里存的是什么。
- `daemon id` / `daemon url` / `server url` 三项未配置时**整行省略**（它们通常由 `agt daemon init` 写入，未接入远程时为空）。

#### `agt daemon config set`

持久化一个或多个执行资源设置。

```bash
$ agt daemon config set --max-concurrent 4 --agent-timeout 1800
Saved configuration to /home/user/.agt/daemon.json

$ agt daemon config set --work-dir /home/user/agt-work
Saved configuration to /home/user/.agt/daemon.json

# allowlist 可重复传，最终存为 JSON 数组
$ agt daemon config set --agent-cwd-allowlist /home/user/projects --agent-cwd-allowlist /home/user/experiments
Saved configuration to /home/user/.agt/daemon.json

# 反向拨号：监听而不是拨出
$ agt daemon config set --listen-mode --ws-port 8422
Saved configuration to /home/user/.agt/daemon.json
```

可用选项：

| 选项 | 说明 | 校验规则 |
| --- | --- | --- |
| `--max-concurrent <N>` | 最大并发 agent 子进程数 | 正整数（≥1） |
| `--agent-timeout <S>` | 单个 task 超时秒数 | 正整数（≥1） |
| `--work-dir <PATH>` | 任务工作目录根 | 绝对路径；`~` 会展开；不需要已存在 |
| `--agent-cwd-allowlist <PATH>` | 允许的 agent cwd 根（可重复） | 每个都必须是绝对路径；`/` 被接受但会触发安全警告 |
| `--listen-mode` / `--no-listen-mode` | 是否进入反向拨号监听模式 | 布尔开关 |
| `--ws-port <N>` | 反向拨号监听端口 | 1–65535 |

约束：

- 路径必须绝对，`~` 会展开为用户 home，尾 `/` 会被规范化去掉（根目录 `/` 除外）。
- `agent_cwd_allowlist` 至少需要一个元素；重复路径会自动去重。
- 配置文件写入是**原子替换**，并设置 `0o600` 权限。
- 如果现有 `~/.agt/daemon.json` 是损坏的 JSON，`set` 会拒绝覆盖并提示你先修复或删除。
- 保存后，如果当前环境变量会覆盖你刚写入的值，CLI 会给出 `Warning`；
  如果本地 Daemon 正在运行（按 `~/.agt/daemon.pid` 判断），CLI 会提示重启才能生效。

#### `agt daemon config unset`

从配置文件里移除指定键，让它们回落到环境变量或默认值。

```bash
$ agt daemon config unset --max-concurrent --agent-timeout
Removed settings; /home/user/.agt/daemon.json was deleted because no managed values remain.
```

可移除的键：`--max-concurrent`、`--agent-timeout`、`--work-dir`、`--agent-cwd-allowlist`、
`--listen-mode`、`--ws-port`、`--daemon-id`、`--daemon-url`、`--server-url`。

#### 配置文件 JSON 格式

```json
{
  "version": 1,
  "max_concurrent": 4,
  "agent_timeout": 1800,
  "work_dir": "/home/user/agt-work",
  "agent_cwd_allowlist": ["/home/user/projects", "/home/user/experiments"],
  "listen_mode": true,
  "ws_port": 8422,
  "daemon_id": "8f3c1d0e-…",
  "daemon_url": "ws://1.2.3.4:8422",
  "server_url": "http://10.0.0.2:8420"
}
```

- `version` 是当前 schema 标记，必须是 `1`。
- 文件中 `agent_cwd_allowlist` 是 JSON 数组；环境变量 `AGT_AGENT_CWD_ALLOWLIST` 则用**冒号分隔**（例如 `/a:/b`）。
- `listen_mode` / `ws_port` / `daemon_id` / `daemon_url` / `server_url` 都是**可选**键，老文件（只有前四项）
  依然有效；它们由 `agt daemon init` 写入，也可以用 `agt daemon config set/unset` 精修。
- 未知顶层键会被拒绝，防止拼写错误被静默忽略。

> **远程机器边界**：`daemon.json` 只影响**本机** Daemon 的下一次启动，不能通过 Server 或 reverse-dial 链路远程管理其他 Daemon。
>
> **Server API 等价接口**：CLI 的 `show` / `set` 逻辑也封装在 FastAPI 端点里，供扩展或其他客户端调用：
> - `GET /api/daemon/config` —— 返回 effective next-start 配置与来源（同 `agt daemon config show`）。
> - `POST /api/daemon/config` —— 持久化一个或多个设置（同 `agt daemon config set`），请求体字段与 CLI option 同名：`max_concurrent`、`agent_timeout`、`work_dir`、`agent_cwd_allowlist`。
>
> 这两个端点操作的是 **Server 进程所在机器** 的 `~/.agt/daemon.json`，不是远程 daemon 的配置。

### `agt daemon list`

> **Server 视角** —— 查询 Server 注册表，列出**所有**已知 daemon（含离线者）。注意与 `agt daemon status`（查**本机** PID 文件）的区别。

daemon 列表由 Server 从 `runtimes` 表按 `daemon_id` 聚合得出；`ONLINE` 基于最后一次心跳（TTL ~90s），离线 daemon 仍列出并显示最后心跳。

**`URL` / `CAPABILITIES` 列（phase A）**：来自 `~/.agt/daemons` 静态配置（`name url [cap1,cap2,...]`，第三段逗号分隔的能力标签，可选）。仅在 daemons 文件里有对应 entry 的 daemon 才会显示这两列；只出现在 runtimes 表、无 reverse-dial 配置的 daemon（如本机走 HTTP long-poll 的 daemon）这两列为 `—`。

**`daemon_id` 的稳定性**：daemon 的身份由它登记的 URL 派生（`uuid5(AGT_DAEMON_NAMESPACE, url)`）——同机重启稳定；**改 `~/.agt/daemons` 里的 name 不影响 id，改 url 才变**（地址搬了就理应换身份）。无 url 时兜底用 `~/.agt/daemon_id` 持久化文件；`AGT_DAEMON_ID` 环境变量可显式覆盖。`daemon_name` 是展示名（来自 daemons 文件 name 或设备名）。

| 参数             | 必填 | 默认值                                                      | 说明        |
| ---------------- | ---- | ----------------------------------------------------------- | ----------- |
| `--server-url` | 否   | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址 |

```bash
$ agt daemon list
🖥️  2 daemon(s) registered on the Server

  NAME              DAEMON_ID        URL                     DEVICE              RUNTIME  PROVIDERS          CAPABILITIES         ONLINE   LAST SEEN
  ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  daemon-cloud-01   1d00e2c4-8865-5 ws://1.2.3.4:8422       demo-workstation 2        claude,codex       containers,logs      yes      2026-08-20 10:30:00
  old-box           9f2c1a3e-b0d7-4 ws://5.6.7.8:8422       old-box             1        codex              —                    no       2026-08-19 03:00:00
```

无 daemon → 提示 "No daemons registered on the Server"，退出码 0。

### `agt daemon show <DAEMON_ID>`

查看单个 daemon 的详情：设备名、在线状态、最后心跳、每个注册 provider 的版本/状态/心跳。

| 参数             | 必填         | 默认值                                                      | 说明                    |
| ---------------- | ------------ | ----------------------------------------------------------- | ----------------------- |
| `DAEMON_ID`    | **是** | —                                                          | daemon 标识（位置参数） |
| `--server-url` | 否           | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址             |

```bash
$ agt daemon show d-abc1234
daemon_id    : d-abc1234
device_name  : my-laptop
online       : yes
last_seen    : 2026-08-20 10:30:00
runtime_count: 2

  PROVIDER     VERSION      STATUS     LAST SEEN
  ────────────────────────────────────────────────────────
  claude       2.1.0        online     2026-08-20 10:30:00
  codex        1.0.0        online     2026-08-20 10:29:58
```

daemon 不存在 → 退出码 `2`。

---

## `agt task`

通过 HTTP 与 Server 通信，提交任务和查询状态。

### `agt task submit`

提交任务到 Server。Server 写入任务队列（`status=pending`），后台 Daemon 周期性 claim 并执行。

除 `-p` 外所有参数可选。

| 参数             | 简写   | 必填         | 默认值                                                      | 说明                                             |
| ---------------- | ------ | ------------ | ----------------------------------------------------------- | ------------------------------------------------ |
| `--prompt`     | `-p` | **是** | —                                                          | 发送给 agent 的提示词                            |
| `--provider`   | —     | 否           | `claude`                                                  | 指定 agent provider                              |
| `--task-id`    | —     | 否           | 自动生成                                                    | 指定 task_id，复用同名 session（实现跨任务续接） |
| `--server-url` | —     | 否           | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址                                      |

```bash
# 基本用法
$ agt task submit -p "explain this function"
task_id=abc12345
  query status with: agt task status abc12345

# 复用 task_id（续接历史 session）
$ agt task submit -p "continue working on auth" --task-id auth-refactor
```

### 取消与动态路由行为

- 取消 Job（`POST /api/jobs/{id}/cancel`）会级联取消其全部后代：pending Step 直接置
  cancelled（不可再被 claim），running Step 通知所属 daemon 终止子进程（反向拨号走
  `tasks.cancel` RPC，HTTP daemon 随下一次 claim 响应收到 `cancelled_task_ids`）；
  迟到的完成报告不会覆写 cancelled 状态。
- 带 identity 的子 Job 由 server 按 capability 动态路由（亲和/负载/绑定偏好）落位到
  任意在线 daemon；跨 daemon 落位时不继承父机器的工作目录；chat 型会话保持钉住。

### `agt task status`

查询任务当前状态和结果。`TASK_ID` 为位置参数。
状态响应中还包含该 Step 声明的产物列表（`artifacts`，含 `agt-artifact://<id>` 引用、
mime、摘要与状态）。**用户侧请用 `agt artifact`**（`list` / `get`）查看与下载产物；
底层仍是 Server API `GET /api/artifacts/<id>`（元数据）与
`GET /api/artifacts/<id>/content`（内容）。

Agent 的交付物约定：spawn 前会在工作目录预创建 `.agt/outputs/`，并在 AGENTS.md
注入（identity 任务）或 prompt 后缀（无 identity 任务）中告知 agent 把最终交付文件
放入该目录；可选写 `.agt/outputs/manifest.json` 声明
`[{"name", "mime_type", "summary", "status"}]` 元数据。只有该目录中的新增/变更文件
会被登记为产物，普通代码修改不算。

| 参数             | 必填         | 默认值                                                      | 说明                |
| ---------------- | ------------ | ----------------------------------------------------------- | ------------------- |
| `TASK_ID`      | **是** | —                                                          | 任务 ID（位置参数） |
| `--server-url` | 否           | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址         |

```bash
$ agt task status abc12345
task_id   : abc12345
provider  : claude
status    : completed
session_id: 695852fa-b891-4847-9fbe-f79d53fd675f
exit_code : 0
duration  : 5.08s

--- stdout ---
agent 输出内容...
--- end stdout ---
```

### `agt task list`

列出 Server 上已知的任务（按更新时间倒序）。底层是 `GET /api/tasks`。

不需任何位置参数。

| 参数             | 简写 | 必填 | 默认值                                                      | 说明                                                                                                                           |
| ---------------- | ---- | ---- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `--status`     | —   | 否   | 所有状态                                                    | 按状态过滤，可重复（`completed` / `failed` / `timeout` / `running` / `pending` / `cancelled`）。不传则列出所有状态 |
| `--limit`      | —   | 否   | `50`                                                      | 返回任务数上限（1-1000）                                                                                                       |
| `--server-url` | —   | 否   | `http://127.0.0.1:8420`（或 `AGT_SERVER_URL` 环境变量） | Server 地址                                                                                                                    |

```bash
$ agt task list
📋 3 task(s) on the Server

  TASK_ID     PROVIDER   STATUS      EXIT   DURATION  UPDATED
  ──────────────────────────────────────────────────────────────────────
  abc12345    claude     completed   0      5.08s     2026-08-19 12:34:56
  def67890    codex      failed      1      12.30s    2026-08-19 12:30:00
  ghi11111    claude     running     —      —         2026-08-19 12:25:11

# 只看失败 / 已完成的
$ agt task list --status completed --status failed --limit 20
```

退出码：

| 退出码 | 含义                 |
| ------ | -------------------- |
| `0`  | 成功（无论是否为空） |
| `1`  | Server 不可达 / 报错 |

---

## `agt sop`

静态 DAG 流水线（SOP，Standard Operating Procedure）：把确定性的多 agent 流程写成一
份 YAML——每个节点是一个 agent 任务，`depends_on` 声明依赖，`input` 里的
`${<node>.<output>}` 引用上游产物。提交后 Server 将其展开为一组共享 root 的 Job：
无依赖节点立即可执行，其余节点 `blocked`，等上游全部终态后自动激活；末端节点
（无下游）的产物即最终结果，不做隐式合成（synthesis）。

支持通过顶层 `loops:` 声明循环块：块内节点可以首尾相连形成环，Server 会按
`max_iterations` 逐次物化迭代任务（如 `review#1` → `fix#1` → `review#2` →
`fix#2` …）。循环内占位符自动解析到**同一迭代**的上游产物。

```bash
agt sop validate pipeline.yaml     # 本地解析 + 静态校验，不连 Server
agt sop submit pipeline.yaml       # 校验 + 展开 + 提交，打印 root_job_id
agt sop run pipeline.yaml          # 提交并轮询跟随，直到全部节点终态
agt sop runs                       # 列出当前 cwd 下的 SOP 运行记录
agt sop runs --daemon=d1           # 可按 daemon 进一步过滤
agt sop status <root_job_id>       # 查看各节点状态/依赖/provider/session_id/产物数
agt sop log <root_job_id> <node>   # 按节点名查看完整 stdout/stderr
```

除 `validate` 外，子命令都与 Server 通信并支持 `--server-url`（默认
`http://127.0.0.1:8420`）。注意：`sop` 子命令当前**不读取** `AGT_SERVER_URL`
环境变量，自定义地址需显式传 `--server-url`。

### `agt sop validate`

只做本地解析 + 静态校验，不连接 Server。

| 参数   | 必填 | 说明                                   |
| ------ | ---- | -------------------------------------- |
| `FILE` | **是** | SOP YAML/JSON 文件路径（需存在） |

```bash
$ agt sop validate pipeline.yaml
ok: 3 node(s), 3 edge(s), root candidates=['review']

$ agt sop validate bad.yaml
invalid: task 'verify' depends on unknown task 'revie'
```

退出码：`0` 有效 / `1` 无效。

### `agt sop submit`

校验 + 展开 + 提交，打印 root_job_id 与每个节点的 job_id。

| 参数           | 必填 | 默认值                    | 说明        |
| -------------- | ---- | ------------------------- | ----------- |
| `FILE`       | **是** | —                       | SOP 文件路径 |
| `--server-url` | 否   | `http://127.0.0.1:8420` | Server 地址  |

```bash
$ agt sop submit pipeline.yaml
root_job_id: 54c04250abcdef0123456789abcdef01
  review: 76d21c9a…
  fix:    9f03b7e2…
  verify: c1184d60…
```

### `agt sop run`

提交 + 轮询跟随，直到全部节点终态；结束时打印 `— terminal artifacts —`，列出每个
done 节点的产物 URI（`agt-artifact://<id>`）。

| 参数           | 必填 | 默认值                    | 说明              |
| -------------- | ---- | ------------------------- | ----------------- |
| `FILE`       | **是** | —                       | SOP 文件路径       |
| `--server-url` | 否   | `http://127.0.0.1:8420` | Server 地址        |
| `--interval` | 否   | `2.0`                   | 轮询间隔（秒）     |

```bash
$ agt sop run pipeline.yaml --interval 1
root_job_id: 54c04250abcdef0123456789abcdef01
review               running      claude     —
fix                  blocked      claude     —  (← review)
verify               blocked      codex      —  (← review,fix)
…
review               done         claude     be9944c2-…  [1 artifact(s)]
fix                  done         claude     93b57066-…  (← review)  [1 artifact(s)]
verify               done         codex      04ab3f21-…  (← review,fix)  [1 artifact(s)]
— terminal artifacts —
  review: agt-artifact://a1b2c3d4…
  fix: agt-artifact://d4e5f6a7…
  verify: agt-artifact://e7f8a9b0…

# 含循环块时，节点名后会带 #迭代 与循环名
$ agt sop run review-fix-loop.yaml --interval 1
review#1 (refine)    done         claude     be9944c2-…  (← fix#3)
fix#1 (refine)       done         claude     93b57066-…  (← review#1)
review#2 (refine)    done         claude     …           (← fix#1)
fix#2 (refine)       done         claude     …           (← review#2)
review#3 (refine)    done         claude     …           (← fix#2)
fix#3 (refine)       done         claude     …           (← review#3)
```

退出码：`0` 全部节点 `done` / `1` 存在 `failed` 或 `cancelled` 节点。

### `agt sop runs`

列出当前 cwd 下的 SOP 运行记录（统一查询入口 `GET /api/runs?mode=sop&cwd=...`）。默认按当前工作目录过滤，避免不同项目的记录混在一起；可加 `--daemon` 进一步限定到某台 daemon，也支持 `--limit`/`--offset` 分页。

```bash
$ agt sop runs
🧩 2/2 SOP run(s) for /home/user/project-x

  ROOT                STATUS    PROVIDER  DAEMON   NAME          UPDATED
  ─────────────────────────────────────────────────────────────────────────────
  54c04250…           done      claude    d1       code-review   2026-10-20 12:34:56
```

### `agt sop status`

查看已提交 DAG 的各节点状态（一次性快照，不跟随）。

| 参数            | 必填 | 默认值                    | 说明                    |
| --------------- | ---- | ------------------------- | ----------------------- |
| `ROOT_JOB_ID` | **是** | —                       | submit/run 打印的根 job id |
| `--server-url` | 否   | `http://127.0.0.1:8420` | Server 地址              |

```bash
$ agt sop status 54c04250
review               done         claude     be9944c2-197e-4db6-b4e6-31e06bc2422b  [1 artifact(s)]
fix                  done         claude     93b57066-14fc-4668-aeb6-8b58173e2f52  (← review)  [1 artifact(s)]

# 循环节点会展示每次迭代
$ agt sop status 54c04250
review#1 (refine)    done         claude     be9944c2-…  (← fix#3)
fix#1 (refine)       done         claude     93b57066-…  (← review#1)  [1 artifact(s)]
review#2 (refine)    done         claude     …           (← fix#1)
fix#2 (refine)       done         claude     …           (← review#2)  [1 artifact(s)]
```

每行依次展示：节点名（循环节点会附加 `#迭代` 与循环名）、状态、provider、session_id、
依赖（`(← …)`）、产物数。循环节点的依赖也带迭代限定。

**查看产物端点**：`status` 只显示产物**数量**。要看具体 `agt-artifact://` 端点，
用行内 session_id 配合 [`agt artifact`](#agt-artifact)：

```bash
# 列出某节点会话的全部产物端点
$ agt artifact list --session be9944c2-197e-4db6-b4e6-31e06bc2422b
# 下载
$ agt artifact get agt-artifact://<id> -o ./report.md
```

（远程 daemon 上的会话需加 `--daemon <id>`。）

### `agt sop log`

按节点名（非 job_id）查看一个节点的完整执行记录：每个 Step 的 `task_id`、
`status`、`session_id`、`exit_code` 与 stdout/stderr 全文。适合在 `sop status`
扫到异常节点后深入排查。

| 参数            | 必填 | 说明                        |
| --------------- | ---- | --------------------------- |
| `ROOT_JOB_ID` | **是** | 根 job id                 |
| `NODE_NAME`   | **是** | 节点名（YAML `tasks[].name`） |
| `--server-url` | 否   | Server 地址（默认 `http://127.0.0.1:8420`） |

```bash
$ agt sop log 54c04250 fix
node      : fix
job_id    : 93b57066-…
provider  : claude
status    : done

--- step 9f03b7e2-… (done) ---
session_id: 93b57066-…
--- stdout ---
…
--- end stdout ---

# 循环节点会按迭代分组
$ agt sop log 54c04250 fix
node      : fix
job_id    : 93b57066-…
provider  : claude
status    : done

=== iteration 1 ===

--- step 9f03b7e2-… (done) ---
session_id: 93b57066-…
--- stdout ---
…
--- end stdout ---

=== iteration 2 ===

--- step a1b2c3d4-… (done) ---
session_id: …
--- stdout ---
…
--- end stdout ---
```

节点未激活（无 Step）时给出友好提示；节点名不存在时 Server 返回 404。循环节点会按
迭代打印 `=== iteration N ===` 分隔。

#### SOP 文件格式

```yaml
name: review-fix

identities:
  reviewer: {did: did:agt:reviewer}         # 指向真实 identity；binding 可选
  tester:   {provider: codex}               # 别名 → 硬 provider 约束

tasks:
  - name: review
    identity: reviewer
    input: 审查当前 diff，输出审查报告
    output: review-report
  - name: fix
    identity: reviewer
    input: 按 ${review.review-report} 修复问题
    depends_on: [review]
    output: fix-summary
  - name: verify
    identity: tester
    input: 用 ${review.review-report} 复核 ${fix.fix-summary}
    depends_on: [review, fix]
```

顶层字段：

| 字段         | 必填   | 说明                          |
| ------------ | ------ | ----------------------------- |
| `name`       | **是** | 非空字符串，流水线名称        |
| `identities` | 否     | 别名 → 路由信号映射（见下表） |
| `tasks`      | **是** | 非空节点列表，最多 64 个      |
| `loops`      | 否     | 循环块列表，见下表            |

循环块（`loops[]`）字段：

| 字段             | 必填   | 说明                                                                                      |
| ---------------- | ------ | ----------------------------------------------------------------------------------------- |
| `name`           | **是** | 循环名，同一 SOP 内唯一                                                                   |
| `nodes`          | **是** | 参与循环的节点名列表；每个节点须存在于 `tasks` 且只能属于一个循环                         |
| `max_iterations` | **是** | 最大迭代次数，正整数，上限 100                                                            |
| `entry`          | 否     | 每次迭代入口节点，默认取 `nodes` 第一个；须属于 `nodes`                                   |

循环块示例：

```yaml
name: review-fix-loop
loops:
  - name: refine
    nodes: [review, fix]
    max_iterations: 5

tasks:
  - name: review
    provider: claude
    input: 审查当前代码，输出审查报告
    output: review-report
  - name: fix
    provider: claude
    input: 按 ${review.review-report} 修复问题
    depends_on: [review]
    output: fix-result
```

执行顺序：`review#1` → `fix#1` → `review#2` → `fix#2` … 直到 `max_iterations`。
`fix#K` 中的 `${review.review-report}` 自动解析为同一次迭代 `review#K` 的产物。

节点（`tasks[]`）字段：

| 字段          | 必填 | 说明                                                       |
| ------------- | ---- | ---------------------------------------------------------- |
| `name`      | **是** | 唯一节点名；`status` / `log` 按它寻址                  |
| `identity`  | 否   | 引用 `identities` 中定义的别名                             |
| `depends_on` | 否   | 上游节点名列表                                             |
| `input`     | 否   | 发给 agent 的提示词，可含 `${node.output}` 占位符         |
| `output`    | 否   | 声明的交付物名（写入 `.agt/outputs/<output>`），全局唯一  |
| `provider`  | 否   | 显式 provider，覆盖 identity 推导                          |
| `daemon`    | 否   | 把节点钉死（pin）到指定 daemon                             |
| `dir`       | 否   | agent 工作目录，远程节点必填（见下文「工作目录」）         |

`identities` 条目字段（别名至少给 `provider` / `did` 之一，可同时给）：

| 字段       | 说明                                                                 |
| ---------- | -------------------------------------------------------------------- |
| `did`    | 指向 Server 上真实 identity；提交时校验存在且 `active`。binding 可选，用于提供路由偏好 |
| `provider` | 未给 `did` 时的硬 provider 约束；提交时校验有存活 runtime 提供      |

#### 校验规则

静态校验（`validate` / `submit` / `run` 在本地都会做，Server 端重复校验）：

- 文档必须是 mapping；`name` 非空
- `tasks` 非空且 ≤64 节点；节点名唯一
- `depends_on` / `identity` / `${node.output}` 引用必须存在
- 不允许自依赖；未声明在 `loops:` 内的依赖环会被拒绝
- `loops:` 块内允许环（实现循环），但每个循环节点必须存在、只能属于一个循环、
  必须提供 `max_iterations`（正整数，≤100），可选 `entry` 必须属于该循环的 `nodes`
- `output` 名全局唯一；占位符必须指向目标节点**已声明**的 output，且目标在 `depends_on` 中
- 孤立节点（无依赖也无下游）仅告警，不报错

`submit` 阶段额外校验（不满足直接报错，而非提交后静默挂起）：身份 `did` 必须存在
且 `active`；binding 可选。节点的 `provider`（显式、identity 别名或已有 binding）
以及 `daemon` pin 必须有存活的 runtime 提供；若 identity 既无匹配 binding 又未声明
provider，则需要显式提供 provider，避免在多个 provider 间静默猜测。

#### 运行语义

- 提交展开为共享 `root_job_id` 的 Job 集：入口节点立即可执行，依赖节点 `blocked`
- 上游全部终态后下游自动激活；`input` 中的 `${node.output}` 解析为上游已登记
  产物的 `agt-artifact://` 引用
- 循环块在提交时只创建第 1 次迭代的 Job；当前迭代全部终态后，Server 自动物化
  下一次迭代（`current < max_iterations`），或将循环哨兵置为 `done` 以唤醒外部
  下游节点
- 循环内占位符优先解析到**同一迭代**的上游产物；跨循环引用默认取最新终态迭代
- 循环中任一节点失败/取消 → 整个循环失败，哨兵进入 `failed`，外部下游链式传播失败
- 上游失败或缺少产物 → 下游以失败激活并链式传播
- 声明了 `output` 的节点，激活时自动向 prompt 追加「将最终交付物写入
  `.agt/outputs/<output>`」的指令——否则 agent 无法得知下游 `${...}` 引用所需的
  精确文件名，只能把结果打到 stdout，导致下游激活时找不到产物而失败
- 节点终态：`done` / `failed` / `cancelled`

#### 工作目录（`dir`）

每个 `task` 的工作目录（`dir`）语义与 `agt chat --dir` 一致：

| 情形               | `dir`   | 实际 agent_cwd                          |
| ------------------ | ------- | --------------------------------------- |
| 节点落到本机 daemon | 不写    | 自动用提交者 pwd                        |
| 节点落到本机 daemon | 写了    | `dir` 路径                              |
| 节点落到远端 daemon | **必须**写 | `dir` 路径（daemon 必须能访问的绝对路径） |
| 节点落到远端 daemon | 不写    | **激活失败**，提示补 `dir`（或把 `daemon:` pin 回本机） |

本机/远端在节点激活时才定（未显式 `daemon:` 钉死时由路由决定），因此远程节点
缺 `dir` 会在激活时报错而非静默退回 daemon 沙箱。示例：

```yaml
tasks:
  - name: review
    identity: reviewer
    dir: /home/bob/repos/proj          # 本机节点，可选
  - name: deploy
    daemon: cloud-01
    provider: codex
    dir: /workspace/proj               # 远端节点，必须
```

---

## `agt goal`

> **动态编排（goal mode）** —— 把一句开放式需求交给一个 root 编排者 agent，由它理解意图、自主拆解、按预算递归 spawn 子 agent、全部子终态后 synthesis 综合交付。与 `agt sop`（确定性静态 DAG）互补：goal 面向「先拆后做」的探索性任务，SOP 面向「已知流程」的确定性复用。运行在现有 Job 树引擎之上，`agt goal` 是入口 + 观测层。

```bash
agt goal submit "为这个仓库写一份部署文档"     # 提交 goal 根 Job，打印 root_job_id
agt goal run "为这个仓库写一份部署文档"        # 提交 + 轮询跟随到终态，打印最终答案与产物
agt goal runs                                 # 列出当前 cwd 下的 goal 运行记录
agt goal runs --daemon=d1                     # 可按 daemon 进一步过滤
agt goal status <root_job_id>                 # 树形渲染整个 Job 子树（符号 ○◐◑⏸●✕⊘▣）
agt goal status <root_job_id> --flat          # 平面输出（每节点一行，机器友好）
agt goal log <root_job_id> [<job_id>]         # 查看某个节点（默认 root）的完整 stdout/stderr
agt goal input <job_id> "你的回答"            # 回答一个 waiting_input 节点 → 续跑回 running
agt goal cancel <root_job_id>                 # 级联取消整个子树
agt goal artifacts <root_job_id> --out-dir ./out   # 下载树中全部产物
agt goal export <root_job_id>                 # 把这次运行蒸馏成 SOP YAML 草稿（stdout 或 -o）
```

除 `status`/`log`/`input`/`cancel`/`artifacts`/`export` 外，`submit`/`run` 都支持
`--server-url`（默认 `http://127.0.0.1:8420`）。与 `sop` 一样，`goal` 子命令**不读取**
`AGT_SERVER_URL` 环境变量，自定义地址需显式传 `--server-url`。

Goal 运行历史按提交命令所在的本地 cwd 分组；`--dir` 只指定 Daemon 侧 agent 的执行目录，
不会改变 `agt goal runs` 的历史分组。远程执行时可同时使用本地项目 cwd 和 daemon 可访问的 `--dir`。

### `agt goal submit`

提交一个 goal 根 Job（`POST /api/goal/submit`），打印 `root_job_id`，不跟随。提交预检
（不满足直接 400，不会产生永远 pending 的根任务）：`--identity` 须存在且 `active`，
binding 可选；显式 `--provider` / `--daemon` 须有 live runtime；**无 identity 且未钉
daemon 时，解析出的 provider 须有 ≥1 个 live runtime**。

| 参数               | 必填 | 默认值                     | 说明                                                   |
| ------------------ | ---- | -------------------------- | ------------------------------------------------------ |
| `GOAL_TEXT`      | **是** | —                        | 一句话需求（位置参数）                                 |
| `--identity`     | 否   | —                         | 根编排者的 identity DID（带 identity 时不注入内置策略模板） |
| `--provider`     | 否   | —                         | 无 identity 时的 provider                               |
| `--daemon`       | 否   | —                         | 把 root 钉到指定 daemon                                |
| `--dir`          | 否   | 当前 cwd                  | root 的工作目录；远程 daemon 建议传 daemon 侧绝对路径（语义同 `chat --dir`） |
| `--max-depth`    | 否   | `5`                     | 树深预算（root=1）                                      |
| `--max-descendants` | 否 | `64`                    | root 下全部非根后代总数预算                             |
| `--server-url`   | 否   | `http://127.0.0.1:8420` | Server 地址                                             |

```bash
$ agt goal submit "把 repo 里 TODO 全部补上实现"
root_job_id: 54c04250abcdef0123456789abcdef01
```

### `agt goal run`

`submit` + 轮询跟随（默认每 2s 拉一次 Job 树重画），直到 root 终态；结束时打印 root 的
最终答案与树中所有 done 节点的产物端点。退出码由 **root 终态**决定：`0` = `done`，
`1` = `failed`/`cancelled`/`timeout`，`2` = Server 不可达/预检拒绝。

| 参数               | 必填 | 默认值                     | 说明                                              |
| ------------------ | ---- | -------------------------- | ------------------------------------------------- |
| `GOAL_TEXT`      | **是** | —                        | 一句话需求（位置参数）                            |
| `--interval`     | 否   | `2.0`                   | 轮询间隔（秒）                                    |
| `--timeout`      | 否   | 关闭                       | 墙钟 watchdog（秒）；到时 cancel 整棵树并退出码 1 |
| （其余同 `submit`） |      |                            |                                                   |

运行中一旦某个节点进入 `waiting_input`（agent 通过 `AGT-ASK` 提问），`goal run` 会打印
该节点需要输入及 `agt goal input <job_id> "…"` 用法，**但不阻塞**——用户可另开终端回答，
本终端继续跟随。示例：

```bash
$ agt goal run "帮我审查这次 diff 并修复" --interval 1
root_job_id: 54c04250abcdef0123456789abcdef01
goal                 ◐ running        claude@d1      3s   …
├─ 审查 diff           ◐ running        claude@d1      1s   …
└─ 修复                ○ queued         claude@d1      —    …

[goal] 54c04250… needs input — answer with: agt goal input 54c04250… "你的回答"
```

### `agt goal runs`

列出当前 cwd 下的 goal 运行记录（统一查询入口 `GET /api/runs?mode=goal&cwd=...`）。默认按当前工作目录过滤，避免不同项目的记录混在一起；可加 `--daemon` 进一步限定到某台 daemon，也支持 `--limit`/`--offset` 分页。

```bash
$ agt goal runs
🎯 2/2 goal run(s) for /home/user/project-x

  ROOT                STATUS    PROVIDER  DAEMON   GOAL                                UPDATED
  ─────────────────────────────────────────────────────────────────────────────────────────────────
  54c04250…           done      claude    d1       为仓库写部署文档                     2026-10-20 12:34:56
```

### `agt goal status`

渲染 Job 子树快照（一次性，不跟随）。每节点显示状态符号、`provider@daemon`、耗时、
label，缩进表层级；`--flat` 改为平面输出（`depth  job_id  status  provider@daemon
duration  label [error]`）。底层是 `GET /api/jobs/{id}/tree`。

```bash
$ agt goal status 54c04250
goal                 done           claude@d1      12s  为这个仓库写文档
├─ 调研               done           codex@d1       5s  调研现状
└─ 撰写               done           claude@d2       8s  撰写文档
```

### `agt goal log`

按 job_id（默认 root）打印完整执行记录：每个 Step 的 `task_id`/`status`/`session_id`/
stdout/stderr 全文。适合 `status` 扫到异常节点后深入排查。

### `agt goal input`

回答一个 `waiting_input` 节点：复用 `POST /api/jobs/{id}/input`，把答案作为下一个 Step
追加，节点转回 `running`。目标节点非 `waiting_input` 时 Server 返回 `409`（透出）。

```bash
$ agt goal input 54c04250… "请用中文，并且按模块拆分"
job_id: 54c04250…
status: running
```

### `agt goal cancel`

复用级联取消端点（`POST /api/jobs/{id}/cancel`），取消整棵子树：pending Step 直接置
cancelled，running Step 通知所属 daemon 终止子进程。CLI 打印取消范围（子树节点数）。

```bash
$ agt goal cancel 54c04250
cancelled 54c04250 (subtree: 5 jobs)
```

### `agt goal artifacts`

下载树中所有产物到 `--out-dir`（默认当前目录）。遍历 Job 树收集 artifact 端点，逐个
`GET /api/artifacts/{id}/content` 下载；同名产物自动加 label 前缀去重；单条失败打印
错误继续其余（best-effort）。

```bash
$ agt goal artifacts 54c04250 --out-dir ./out
./out/部署文档.md  (1423 bytes)
./out/架构调研.md  (890 bytes)
```

### `agt goal context`

查看一次 Goal 运行中的共享上下文条目。默认只显示 `active`，可查看历史状态。

```bash
agt goal context <root_job_id>
agt goal context <root_job_id> --scope shared --kind decision
agt goal context <root_job_id> --status all --json-output
```

| 参数 | 说明 |
|---|---|
| `--scope` | `shared` 或 `job` |
| `--kind` | `decision` / `constraint` / `fact` / `note` |
| `--status` | `active`、`superseded`、`retracted` 或 `all` |
| `--json-output` | 输出原始 JSON |

### `agt goal export`

把一次 goal 运行的 **Step 事件图**蒸馏成 SOP YAML 草稿（`GET /api/goal/{root}/export`）。
输出到 stdout 或 `-o`；末尾提示用 `agt sop validate` 校验、`agt sop run` 复跑。所有
变换无法直接表达的项都以 warnings 外显到 stderr。

| 参数               | 必填 | 默认值 | 说明                                      |
| ------------------ | ---- | ------ | ----------------------------------------- |
| `ROOT_JOB_ID`    | **是** | —    | 目标 goal 根 job id                        |
| `-o`, `--out`    | 否   | stdout | 写入 YAML 的文件路径（否则打印到 stdout） |
| `--include-failed` | 否   | 关闭   | 一并导出失败的 retry 波                    |
| `--keep-planning`  | 否   | 关闭   | 保留纯规划轮（无下游消费的孤儿 task）      |

#### 变换规则（参考）

- **phase-split 按「最终 Step」划分，而非固定两阶段**：叶子 Job（从未 spawn）→ 1 个
  task；spawn 过的 Job → 2 个 task：`<n>`（全部非终 Step 的坍缩：规划轮、重试
  synthesis、needs_input 回答轮）+ `<n>-synth`（最终 Step）。
- **纯规划轮默认丢弃**：产物无人消费的 plan task 删掉（`--keep-planning` 保留为孤儿）。
- **失败波排除**：一个 failed Job 及其后代子树（synthesis 重试所放弃的波次）默认排除
  （`--include-failed` 包含）。
- **数据边**：最终/中间消息里的 `agt-artifact://<id>` 经 `ArtifactModel.step_id` 反查
  产属顶点，重写为 `${node.output}` 并加 `depends_on`；synthesis 消息携带的子中间产物
  边是**忠实依赖**（保证中间产物不被误删），非噪声。
- **结构边**：`<n>-synth` 依赖其每个子 Job 的代表顶点。
- **output 绑定**：被消费的 `ArtifactModel.name` 作为该 task 的 `output`。
- **identities 段**：按 `identity_did` 聚合为 `did-<slug>` 别名，否则按 `provider`；不导出
  daemon/dir。
- **emit 前冲突消解**（保证产物必过 `parse_sop`，否则导出以内部错误返回而非吐非法
  YAML）：同 producer 多产物 → 主产物为 `output`、其余引用降级文字附注 + warning；跨
  producer 同名产物 → 自动改名 `<task>/<name>` 并重写全部引用 + warning。

### 预算语义与并行约束

- **`--max-depth`（默认 5）**：树深预算（root=1），超过即拒绝继续 spawn。
- **`--max-descendants`（默认 64）**：root 下**全部非根后代**总数（含孙层与 retry 产生的
  后代），耗尽即拒绝，防无限循环。synthesis 重试 spawn 出的新子也计入该预算。
- **`max_batch=8`**：单次扇出的子任务数上限；MCP `spawn_subagents` 和 `AGT-SPAWN` 共用同一预算。
- **⚠️ 并行约束**：预算只约束「能派多少子任务」，**实际并发受 daemon 的
  `AGT_MAX_CONCURRENT`（默认 `1`）限制**——即使一次扇出 8 个子，单个 daemon 默认仍是
  串行执行。要真正并行，需提高 daemon 的 `AGT_MAX_CONCURRENT`。

> **边界语义**：子失败 ≠ 根失败（由 root 的 synthesis 决定是否重试）；root 进入
> `waiting_input` 时用 `goal input` 续跑；`--timeout` 是客户端 watchdog，到时触发 cancel。

---

## `agt context`

供 Agent 子进程读取当前任务可见的共享上下文。命令依赖环境变量
`AGT_SERVER_URL`、`AGT_TASK_TOKEN` 和 `AGT_TASK_ID`，不能用于任意 Job 越权读取。

```bash
agt context read
agt context read --key shared/api-schema
agt context read --key shared/api-schema --no-json
```

对应端点：`GET /api/agent/context`。返回结果按当前 Job 的 `shared` 和 lineage
可见性过滤，单次响应有 4 KB 预算。

---

## `agt identity`

> **Phase 4 Step 1** —— Agent 身份管理。一个 identity 是跨任务持久化的 agent 人设（名字、描述、能力标签），用 DID 标识：`did:agt:<slug>`。创建时会落盘一份 `AGENTS.md`（见下文"文件位置"）。

所有子命令都支持 `--server-url`（默认 `http://127.0.0.1:8420` 或 `AGT_SERVER_URL` 环境变量），下表不再重复。

### `agt identity list`

列出 Server 上的身份，表格显示 DID / NAME / STATUS / CAPABILITIES。

| 参数         | 必填 | 默认值 | 说明                                                    |
| ------------ | ---- | ------ | ------------------------------------------------------- |
| `--daemon` | 否   | 所有   | 按 daemon_id 过滤                                       |
| `--status` | 否   | 所有   | 按状态过滤（`active` / `deprecated` / `revoked`） |

```bash
$ agt identity list
🪪 2 identit(y/ies) on the Server

  DID                                            NAME                 STATUS       CAPABILITIES
  ─────────────────────────────────────────────────────────────────────────────────────
  did:agt:code-reviewer                    Code Reviewer        active       code-review,doc-style-audit
  did:agt:doc-translator                  Doc Translator       deprecated   translation
```

### `agt identity show <DID>`

显示一个身份的完整详情。找不到时退出码 `2`。

```bash
$ agt identity show did:agt:code-reviewer
did         : did:agt:code-reviewer
daemon_id   : local
name        : Code Reviewer
description : Python code review
capabilities: code-review, doc-style-audit
status      : active
...
```

### `agt identity create`

创建身份（DID 自动生成，同时写 `AGENTS.md`）。

| 参数              | 必填         | 默认值       | 说明                                                      |
| ----------------- | ------------ | ------------ | --------------------------------------------------------- |
| `--name`        | **是** | —           | 人设名称；slug 未指定时从 name 派生                       |
| `--slug`        | 否           | 从 name 派生 | 机器名（2-64 位小写字母数字连字符，两端不能是连字符）     |
| `--daemon-id`   | 否           | `local`    | 归属/审计字段，不参与 DID                                 |
| `--description` | 否           | 空           | 描述                                                      |
| `--capability`  | 否           | 空           | 能力标签，可重复                                          |
| `--visibility`  | 否           | `private`  | `private` / `public`（跨 daemon 可见性，Step 2 生效） |

```bash
$ agt identity create --name "Code Reviewer" \
    --capability code-review --capability doc-style-audit \
    --description "Python code review"
✅ Created identity: did:agt:code-reviewer
  AGENTS.md: ~/.agt/identities/<slug>/AGENTS.md (opt-in consumption)
```

同一 Server 下 slug 重复 → 服务端返回 `409`，CLI 退出码 `1`。

### `agt identity update <DID>`

部分更新（只传要改的字段）。**不会重写 `AGENTS.md`** —— 手动编辑的内容保留；需要重新生成时用 `sync-files`。

| 参数                    | 必填 | 默认值 | 说明                 |
| ----------------------- | ---- | ------ | -------------------- |
| `--name`              | 否   | 不变   | 新名称               |
| `--description`       | 否   | 不变   | 新描述               |
| `--add-capability`    | 否   | 不变   | 添加能力标签，可重复 |
| `--remove-capability` | 否   | 不变   | 移除能力标签，可重复 |

什么都不传 → 退出码 `2`（`Nothing to update`）。

### `agt identity deprecate <DID>` / `agt identity revoke <DID>`

状态迁移，均可重复执行（幂等）：

- `deprecate`：软删除（`status=deprecated`），历史任务引用保持有效
- `revoke`：硬吊销（`status=revoked`）

### `agt identity delete <DID>`

物理删除身份记录。**若有任务仍引用该身份（`tasks.identity_did`），服务端返回 `409` 拒绝删除**，并提示引用它的 task_id。已废弃/已吊销但仍有引用的身份只能保留记录。

### `agt identity sync-files <DID>`

用数据库当前状态**强制重新生成** `AGENTS.md`（覆盖手动编辑）。`update` 之后想让落盘文件与数据库重新对齐时使用。

### `agt identity bind <DID>`

将一个 runtime（daemon + provider）记录为 identity 的路由偏好。**binding 是可选的**：
绑定的 runtime 会获得 default/priority 优先级；没有 binding 或候选 binding 不匹配时，
identity 仍可使用任意匹配的 live provider/runtime。

| 参数           | 必填         | 默认值 | 说明                                                 |
| -------------- | ------------ | ------ | ---------------------------------------------------- |
| `--daemon`   | **是** | —     | daemon 命名空间                                      |
| `--runtime`  | **是** | —     | provider（claude / codex / ...）                     |
| `--priority` | 否           | `0`  | 偏好权重；无 default 时选最高者                      |
| `--default`  | 否           | 关闭   | 设为该 identity 的 default（原子地清除其他 default） |

```bash
$ agt identity bind did:agt:reviewer --daemon local --runtime claude --default
🔗 Bound did:agt:reviewer → local/claude (default)

# 给同一 identity 再绑一个 runtime
$ agt identity bind did:agt:reviewer --daemon local --runtime codex --priority 10
```

同一三元组重复 bind → 服务端 409，CLI 退出码 1。

### `agt identity unbind <DID>`

移除一个 binding（按 `--daemon` + `--runtime` 定位）。

```bash
$ agt identity unbind did:agt:reviewer --daemon local --runtime codex
🔓 Unbound local/codex from did:agt:reviewer
```

目标不存在 → 退出码 `2`（`No binding ...`）。

### `agt identity bindings <DID>`

列出 identity 的所有 binding，表格显示 DAEMON / RUNTIME / PRIORITY / DEFAULT。

```bash
$ agt identity bindings did:agt:reviewer
🔗 2 binding(s) for did:agt:reviewer

  DAEMON           RUNTIME      PRIORITY   DEFAULT
  ────────────────────────────────────────────────
  local            claude       0          yes
  local            codex        10         —
```

支持 `--daemon <id>` 过滤。无 binding → 友好提示，退出码 0。

### `agt daemon bindings <DAEMON_ID>`

反向视图：列出某个 daemon 下**所有 identity** 的 binding 记录（跨身份），表格显示 IDENTITY / RUNTIME / PRIORITY / DEFAULT。

```bash
$ agt daemon bindings local
🔗 2 binding(s) on daemon local

  IDENTITY                                 RUNTIME      PRIORITY   DEFAULT
  ──────────────────────────────────────────────────────────────────
  did:agt:code-reviewer                    claude       0          —
  did:agt:reviewer                         codex        10         yes
```

无 binding → 友好提示，退出码 0。与 `agt identity bindings <DID>`（按 identity 看它绑了哪些 runtime）互补。

### `agt identity default <DID>`

原子地把某个 binding 设为 identity 的 default（自动清除其他 binding 的 default 标记）。

```bash
$ agt identity default did:agt:reviewer --daemon local --runtime codex
⭐ Default for did:agt:reviewer → local/codex
```

目标不存在 → 退出码 `2`。

> **选择语义**：`POST /api/identities/resolve` 优先使用 live binding，其次按
> `default` / `priority` 排序；没有可用 binding 时回退到 live runtime registry。
> 因此删除全部 binding 不会使 identity 失效。

### AGENTS.md 的位置与语义

| 内容                  | 路径                                   |
| --------------------- | -------------------------------------- |
| 身份文件（AGENTS.md） | `~/.agt/identities/<slug>/AGENTS.md` |

- **写入一次**：`create` 时生成；`update` 不覆盖（保护手动编辑）
- **强制重写**：只有 `sync-files` 会用数据库状态覆盖
- **消费是 opt-in**：当前版本 runtime（`agt run` / `agt chat`）**不会自动读取** AGENTS.md；它是一份给人看、给后续步骤（Step 2 身份绑定与注入）用的声明文件

---

## `agt token issue`

> ⚠️ **开发 / 调试用** —— 日常使用不需要此命令。Daemon 在 spawn agent 子进程时会自动签发 token 并通过 `AGT_TASK_TOKEN` 环境变量注入；只有当你想手工测试 Server 的 `/api/agent/*` 端点（curl、Notebook 等）时才需要这条命令。

签发一个 task-scoped 的 HMAC 令牌，Server 会在收到带 `X-Task-Token: <token>` 与 `X-Task-Id: <task-id>` 的请求时校验通过。

需要 `AGT_SERVER_SECRET` 与 Server 端使用的密钥一致，否则 Server 会返回 `401 invalid token`。`agt token issue` 会优先读环境变量，未设置时回退到 `~/.agt/server.secret`。

### 参数

| 参数            | 必填         | 默认值                 | 说明                                             |
| --------------- | ------------ | ---------------------- | ------------------------------------------------ |
| `--task-id`   | **是** | —                     | token 绑定的 task_id（必须与`X-Task-Id` 一致） |
| `--provider`  | 否           | `claude`             | 嵌入 token`sub` claim 的 provider              |
| `--workspace` | 否           | `/tmp/agt/<task-id>` | 嵌入 token 的 workspace_id                       |
| `--ttl-hours` | 否           | `24`                 | 有效期（小时）                                   |
| `--agent-id`  | 否           | `cli-issue`          | 嵌入 token`sub` claim 的 agent 标识            |

### 示例

```bash
# 签发一个绑定到 task-abc 的 token
# 如果已运行过 `agt server init`，则自动使用 ~/.agt/server.secret
$ TOKEN=$(agt token issue --task-id task-abc)
$ echo $TOKEN
eyJzdWIi...<payload>.<hex_sig>

# 用 curl 测试一个 Server 端点（示例）
$ curl -X POST http://127.0.0.1:8420/api/agent/callback \
    -H "X-Task-Token: $TOKEN" \
    -H "X-Task-Id: task-abc" \
    -H "Content-Type: application/json" \
    -d '{...}'
```

### 退出码

| 退出码 | 含义                               |
| ------ | ---------------------------------- |
| `0`  | 成功，token 打到 stdout            |
| `1`  | 缺少`AGT_SERVER_SECRET` 环境变量 |
| `2`  | Click 参数缺失（如`--task-id`）  |

---

## `agt artifact`

> **Phase 2/3 打通链路** —— 查看和下载 agent 声明的交付物（artifact）。产物由 agent 放入 `.agt/outputs/`（可选 `manifest.json` 描述），Server 登记 lineage / mime / 摘要 / 状态，公开引用为 `agt-artifact://<id>`。此命令让本地用户能拿到**任意会话**（含远程 daemon 的会话）的产物。

所有子命令都支持 `--server-url`（默认 `http://127.0.0.1:8420` 或 `AGT_SERVER_URL` 环境变量），下表不再重复。

### `agt artifact list`

两种模式：

- **带 `--session`**：列出该会话下登记的所有产物。**会话必须属于给定 daemon**，否则 Server 返回 `403`（绝不跨 daemon 泄漏产物）。
- **不带 `--session`**：列出目标 daemon 下的**所有**产物（跨会话），多一列 `SESSION` 声明每个产物所属的会话 id。

`--daemon` 默认取**本机 daemon**（`_local_daemon_id()`，与 `agt chat --list --daemon` 的本机判定一致）；本机找不到 daemon 时报错退出，需显式 `--daemon <id>`。

| 参数         | 必填         | 默认值       | 说明                                             |
| ------------ | ------------ | ------------ | ------------------------------------------------ |
| `--session` | 否           | —           | 会话 id；不传则列出 daemon 下全部产物（带 `SESSION` 列） |
| `--daemon`  | 否           | 本机 daemon  | 目标 daemon；带 `--session` 时校验归属，不匹配 `403` |

```bash
# 单会话：列出某个会话的产物（daemon 默认本机）
$ agt artifact list --session abc-1234…
📦 2 artifact(s) for session abc-1234…

  NAME             STATUS   SIZE       MIME                     URI
  ──────────────────────────────────────────────────────────────────────────────────────
  report.md        ready    12         text/markdown            agt-artifact://a1b2c3…
  sub/out.bin      ready    4096       application/octet-stream agt-artifact://d4e5f6…

# 跨会话：列出目标 daemon 下的全部产物（带 SESSION 列）
$ agt artifact list --daemon=daemon-cloud-01
📦 3 artifact(s) on daemon 'daemon-cloud-01'

  SESSION                  NAME             STATUS   SIZE   URI
  ───────────────────────────────────────────────────────────────
  abc-1234…                report.md        ready    12     agt-artifact://a1b2c3…
  abc-1234…                sub/out.bin      ready    4096   agt-artifact://d4e5f6…
  def-5678…                notes.md         ready    99     agt-artifact://e7f8a9…
```

会话不存在 → `404`；`--session` 与 `--daemon` 归属不符 → `403`，CLI 均退出码 `1` 并打印原因；本机无 daemon 且未传 `--daemon` → 退出码 `2`。

### `agt artifact get`

下载产物字节。两种模式：

**单条下载**（位置参数 + 可选 `-o`）：

```bash
$ agt artifact get agt-artifact://a1b2c3…            # 按 name 的 basename 写到当前目录
$ agt artifact get a1b2c3… -o /tmp/report.md         # 显式指定输出路径
```

| 参数                    | 必填         | 默认值           | 说明                                        |
| ----------------------- | ------------ | ---------------- | ------------------------------------------- |
| `REF`                 | **是** | —               | 产物 id 或 `agt-artifact://<id>` URI      |
| `-o`, `--output`     | 否           | name 的 basename | 写出路径                                    |

**批量下载**（按会话一次下全）：

```bash
$ agt artifact get --session abc-1234… --daemon=daemon-cloud-01 --out-dir ./dl
📥 agt-artifact://a1b2c3… → dl/report.md (12 bytes)
📥 agt-artifact://d4e5f6… → dl/sub/out.bin (4096 bytes)
✅ 2 artifact(s) → dl
```

| 参数          | 必填         | 默认值 | 说明                                          |
| ------------- | ------------ | ------ | --------------------------------------------- |
| `--session` | **是** | —     | 会话 id                                        |
| `--daemon`  | **是** | —     | 拥有该会话的 daemon（归属校验）                |
| `--out-dir` | **是** | —     | 产物写入目录（保留产物内部的子目录结构）       |

批量模式是 best-effort：单个产物下载失败（如 producer 离线、文件丢失）会打印 `⚠️` 继续下其余的，最终退出码 `1`；全部成功退出码 `0`。

产物字节经 Server 解析：内联 / Server 磁盘内容直接返回；仅存在于 producer daemon 本地时，Server 通过 reverse-dial 链路向 origin daemon **懒拉取**（sha256 校验后落地），producer 离线或文件丢失会返回可重试错误。

---

## 端到端快速启动

典型的三终端工作流：

```bash
# 一次性：生成 Server 与 Daemon 共享的 HMAC 密钥（Phase 3 起必须）
$ agt server init
# ↑ 未配置 secret 时 `agt server start` 会提示运行该命令；
# ↑ 也可以手动 export AGT_SERVER_SECRET=...，优先级高于文件。
# ↑ Daemon 不设也能跑，只是不注入 AGT_TASK_TOKEN（回调链路不通）

# 终端 1：启动 Server
$ agt server start

# 终端 2：启动 Daemon（自动继承 shell 里的 AGT_SERVER_SECRET）
$ agt daemon start

# 终端 3：提交任务、查询状态
$ agt task submit -p "say hi"
$ agt task status <task_id>

# 关闭
$ agt daemon stop
# Ctrl-C 关闭 Server
```

---

## 环境变量

| 变量                       | 默认值                    | 说明                                                                                                                                                                |
| -------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGT_SERVER_URL`         | `http://127.0.0.1:8420` | Server 地址                                                                                                                                                         |
| `AGT_SERVER_HOST`        | `127.0.0.1`             | Server 绑定地址                                                                                                                                                     |
| `AGT_SERVER_PORT`        | `8420`                  | Server TCP 端口                                                                                                                                                     |
| `AGT_DB_PATH`            | `~/.agt/agt.db`         | SQLite 数据库文件                                                                                                                                                   |
| `AGT_DAEMON_PORT`        | `8421`                  | Daemon 本地资源服务端口                                                                                                                                             |
| `AGT_DAEMON_ID`          | `daemon.json` → `daemons` 的 `id=` → `uuid5(URL)` | Daemon 标识。一般由 `agt daemon init` 写入，无需手工设置；显式设置时优先级最高 |
| `AGT_LISTEN_MODE`        | `0`                     | 设为 `1` 时 daemon 监听固定端口等 Server 拨入（reverse-dial）。可持久化到 `~/.agt/daemon.json` |
| `AGT_DAEMON_WS_PORT`     | `8422`                  | reverse-dial 监听端口；也可写入 `~/.agt/daemon.json` 的 `ws_port` |
| `AGT_DAEMONS_FILE`       | `~/.agt/daemons`        | 反向拨号目标列表文件路径（Server 与 daemon 共用解析逻辑） |
| `AGT_SERVER_SECRET`      | —                        | **Phase 3 起必须**。Server 与 Daemon 共享的 HMAC 密钥，用于签发 `AGT_TASK_TOKEN`。优先从环境变量读取，未设置则回退到 `~/.agt/server.secret`。`agt server start` 首次运行会自动生成 |
| `AGT_DAEMON_LOCAL_TOKEN` | 随机（每启动一次轮换）    | 可选。固定后所有 spawn 出去的子进程使用同一 token。随机时每次 daemon 重启自动轮换                                                                                   |
| `AGT_USE_WS_CLAIM`       | `0`                     | **Phase 3.3 起 opt-in**。设为 `1` 时远程 daemon 通过 WS（`ws://<server>/ws/daemon`）走 WS-first claim，HTTP 仅作 fallback；默认 `0` 维持 HTTP-only 行为 |
| `AGT_DEVICE_NAME`        | hostname                  | 设备显示名                                                                                                                                                          |
| `AGT_RUNTIME_NAME`       | —                        | 覆盖 Runtime 名称                                                                                                                                                   |
| `AGT_MAX_CONCURRENT` | `1` | 最大并发 agent 子进程数；优先级高于 `~/.agt/daemon.json` |
| `AGT_AGENT_TIMEOUT` | `600` | Agent 超时秒数；优先级高于 `~/.agt/daemon.json` |
| `AGT_WORK_DIR` | `~/.agt/workspaces` | 任务工作目录根；优先级高于 `~/.agt/daemon.json` |
| `AGT_AGENT_CWD_ALLOWLIST` | `~`（即 `Path.home()`） | 允许的 agent cwd 根目录，冒号分隔；优先级高于 `~/.agt/daemon.json` |
| `AGT_CLAUDE_PATH`        | —                        | 覆盖`claude` 二进制路径                                                                                                                                           |
| `AGT_EXTRA_AGENTS`       | —                        | 自定义 agent，格式`cmd:provider:Display`，逗号分隔                                                                                                                |

---

## 文件位置

| 内容                    | 路径                                                                                          |
| ----------------------- | --------------------------------------------------------------------------------------------- |
| Server 数据库（SQLite） | `~/.agt/agt.db`                                                                             |
| Server HMAC secret      | `~/.agt/server.secret`（`agt server start` 首次运行自动生成，`agt server init` 可显式重建；权限 `0o600`） |
| 反向拨号目标列表        | `~/.agt/daemons`（`name url [id=<uuid>] [capabilities]`；`AGT_DAEMONS_FILE` 可覆盖）        |
| 本机直连 Chat session 索引 | `<cwd>/.agt/sessions.json`（仅 bare `agt chat` 使用）                                    |
| Server 运行 / 会话记录  | `~/.agt/agt.db`（chat / sop / goal 统一以 SQLite 为信息源）                                  |
| 旧版 session 文件       | `~/.agt/sessions.json`（不再自动读写，仅保留历史迁移用途）                                     |
| Daemon PID 文件         | `~/.agt/daemon.pid`                                                                         |
| Daemon 执行资源配置文件         | `~/.agt/daemon.json`（可选；权限 `0o600`）                                                           |
| 任务工作目录            | `~/.agt/workspaces/<provider>/<task_id>/`                                                   |
| 身份文件（AGENTS.md）   | `~/.agt/identities/<slug>/AGENTS.md`（`agt identity create` 时生成，见 `agt identity`） |
| Alembic migrations      | `migrations/versions/`                                                                      |
| API 文档                | [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs)（Server 运行时）                      |
