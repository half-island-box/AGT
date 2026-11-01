# agt 快速启动指南

> 跑起来一个 Server + 一个（或多个）Daemon 的最短路径。
> 涵盖本地部署与远程部署，覆盖 Phase 3（WS-RPC + Provider 路由 + heartbeat 同步）+ Phase 5（本地流式输出 + Provider 适配层）。

完整命令参考见 [`commands.md`](commands.md)；本文档只讲**端到端怎么跑起来**。

---

## 0. 前置

- Python ≥ 3.11
- 装好 agt（开发模式或 wheel 都行）：

  ```bash
  # 方式 A：开发模式（推荐，改完代码不用重装）
  $ git clone https://github.com/<you>/agt.git ~/agt
  $ cd ~/agt
  $ pip install -e .

  # 方式 B：用 uv$ uv sync
  ```

  验证：

  ```bash
  $ agt --help
  $ agt server --help
  $ agt daemon --help
  ```
- 一台或多台机器上能跑 agent CLI（claude / codex / copilot 任意）

---

## 1. 三种部署形态

| 形态               | Server                      | Daemon   | 用途                                                                       |
| ------------------ | --------------------------- | -------- | -------------------------------------------------------------------------- |
| **本地**     | 同机                        | 同机     | 全部走 HTTP，最简单                                                        |
| **反向连接** | 任意（即使在 NAT 后）       | 多台机器 | **推荐远程 daemon 部署**：daemon 监听固定端口，Server 主动 dial 出去 |
| **混合**     | 多 daemon 一台或多台 server | —       | 多机调度                                                                   |

下面从本地（最快）讲到反向连接（推荐远程场景）。

---

---

## 2. 本地部署（最快，5 条命令）

Server 和 Daemon 在同一台机器，都走 HTTP。

### 2.1 server secret（通常无需手工创建）

`agt server start` 首次运行会自动生成一个随机 secret 写到 `~/.agt/server.secret`（`0600`），
`agt daemon start` 会读取同一个文件——**同机部署不需要 export 任何变量**。
想显式创建或轮换时再跑 `agt server init`：

```bash
$ agt server init
Generated server secret at /home/user/.agt/server.secret

The secret is now auto-loaded by `agt server start` and `agt daemon start`. If you prefer environment variables, run:
    export AGT_SERVER_SECRET="$(cat /home/user/.agt/server.secret)"
```

之后每个新 shell 都无需再 `export`。如果你更喜欢把 secret 放在 `~/.config/agt/env` 里（例如还需要放其它变量），详见 [§8.1 持久化 secret](#81-持久化-secret-推荐方式二)。

### 2.2 启动 Server 与 Daemon

```bash
# ① 启动 Server（前台阻塞；Ctrl-C 停止）
$ agt server start
INFO:     Uvicorn running on http://127.0.0.1:8420 (Press CTRL+C to quit)
INFO:     AGT console: http://127.0.0.1:8420/

# ② 另开一个 shell，启动 Daemon（后台进程）
$ agt daemon start
# 日志写到 ~/.agt/daemon.log

# ③ 提交一个任务
$ agt task submit --prompt "say hello" --provider claude
task_id=e2d812c3
  query status with: agt task status e2d812c3

# ④ 看状态
$ agt task status e2d812c3
$ agt runtime list    # 看 daemon 注册的 agent
```

> **Web 控制台**：Server 启动后浏览器打开 `http://127.0.0.1:8420/console/`
> 即可使用项目、Chat、SOP、Goal、设置等页面，**不需要额外跑 `npm`**——页面
> bundle 随包发布；从含 `vscode/` 源码的 checkout 启动且 bundle 缺失时，
> `agt server start` 会自动在 `vscode/` 里执行一次 `npm run build:console`（设
> `AGT_CONSOLE_AUTOBUILD=0` 可关闭，`AGT_CONSOLE_SOURCE_DIR=<path>` 可指定
> 源码位置）。修改 webview 源码后仍需手动重新构建。

> **Phase 5 本地流式**：直接跑 `agt run -p "..."` / `agt chat`（**不带 `--daemon`**）会在终端看到逐 token 的 agent 输出。Claude Code 默认走 `--output-format stream-json --verbose`，codex 走 `codex app-server` 的 JSON-RPC 流（thinking / 工具调用 / token 用量与 claude 同通道），session_id / resume_rejected 从结构化字段读取；没有 `app-server` 的旧版 codex 自动回退到 `codex exec` pipe 文本，其它 provider 仍走老路径。详见 [commands.md `agt run`](commands.md#agt-run)。

> **SOP 静态流水线**：把确定的多 agent 流程写成 YAML DAG（`agt sop validate /
> submit / run / runs / status / log`）。节点按 `depends_on` 依赖门控激活，`${node.output}`
> 引用上游产物，末端节点产物即最终结果。`sop runs` 按 cwd 列出历史运行，`sop status`
> 展示各节点 provider/session_id，`sop log <root> <node>` 按节点名查看完整 stdout/stderr。

> **goal 动态编排**：把一句开放式需求交给 root 编排者 agent 自主拆解、按预算
> （`--max-depth` / `--max-descendants`）递归 spawn 子 agent、synthesis 综合交付
> （`agt goal submit / run / runs / status / log / input / cancel / artifacts / export`）。
> Claude Code / Codex 优先通过 `spawn_subagents` MCP 工具派生子任务；其他 provider
> 自动使用 `AGT-SPAWN` 兼容路径。两种路径共用同一套预算、路由、幂等和 synthesis 状态机。
> `goal runs` 按提交任务时的本地 cwd 列出历史运行；`--dir` 只指定 daemon 侧执行目录，不参与历史分组。
> 运行时可用 `agt goal status` 观测整棵 Job 树、`goal input` 回答 agent 的 `AGT-ASK`
> 提问、`goal cancel` 级联取消；一次拆得好的运行可用 `agt goal export` 蒸馏成 SOP
> YAML，经审阅后 `agt sop run` 复跑复用。详见
> [commands.md `agt goal`](commands.md#agt-goal)。

> **Goal 共享上下文**：Goal 树内的 Agent 可以通过 MCP
> `context_write` / `context_read` / `context_retract` 交换有版本的决策、约束、
> 事实和 Artifact 引用。新 Step 会自动获得有预算的 ContextBrief；非 MCP Agent
> 可使用 `AGT-CONTEXT` 写入，并在子进程内运行 `agt context read` 读取。用户可用
> `agt goal context <root_job_id>` 观察整次运行。详见
> [共享上下文方案](../impl/goal-shared-context-design.md)。

> **动态路由与取消**：带 identity 的子任务会按「能力 + 亲和 + 负载」在多台在线 daemon 间
> 动态落位（无候选时回退父 daemon）。identity binding 是可选的调度偏好，不是使用
> identity 的前置条件；没有匹配 binding 时仍可选择任一 live runtime。chat 会话始终钉在首次落位的 daemon。取消 Job
> 会级联整棵子树并尽力终止远端正在运行的子进程（反向拨号 daemon 走 `tasks.cancel`
> 推送，HTTP daemon 在下一次 claim 响应中收到取消列表）。

> **产物（Artifact）**：每次执行会在工作目录预创建 `.agt/outputs/`，并提示 agent
> 把最终交付文件放入该目录（可选用 `.agt/outputs/manifest.json` 声明元数据）。经
> Server 执行的 Step 会把这些文件登记为可寻址产物（`agt-artifact://<id>`），后续
> Step 可按引用消费；本地直跑不登记，文件留在 `.agt/outputs/`。

### 本地部署的 env 变量

| 变量                  | 默认                      | 说明                                          |
| --------------------- | ------------------------- | --------------------------------------------- |
| `AGT_SERVER_SECRET` | —                        | **必填**，Daemon 和 Server 必须一致；优先读环境变量，未设置则回退 `~/.agt/server.secret` |
| `AGT_SERVER_URL`    | `http://127.0.0.1:8420` | Daemon 连哪台 Server                          |
| `AGT_DAEMON_ID`     | `~/.agt/daemon.json` → `daemons` 的 `id=` → `uuid5(URL)` | 标识这台 daemon；一般由 `agt daemon init` 写入，无需手工设置 |

#### 持久化 Daemon 执行资源配置（推荐）

除了环境变量，你还可以把 Daemon 的并发、超时、工作目录根、agent cwd 白名单持久化到 `~/.agt/daemon.json`。配置在 **Daemon 下次启动时**生效，环境变量仍具有最高优先级。

```bash
# 查看下一次启动时真正生效的值
$ agt daemon config show

# 持久化常用设置
$ agt daemon config set --max-concurrent 4 --agent-timeout 1800
$ agt daemon config set --work-dir /home/user/agt-work
$ agt daemon config set --agent-cwd-allowlist /home/user/projects --agent-cwd-allowlist /home/user/experiments

# 不需要时移除，回落到默认值
$ agt daemon config unset --max-concurrent
```

保存后若本地 Daemon 正在运行，CLI 会提示你重启才能生效。

停掉：

```bash
$ agt daemon stop
# Ctrl-C 退出 agt server start 的前台进程
```

---

---

## 3. 反向连接（NAT 穿透，远程 daemon 推荐）

如果你的 Server 跑在家用 NAT / 云安全组后面、入站被拒，但远程 daemon 在云上有公网 IP，
**让 daemon 监听固定端口、Server 主动 dial 出去**——出站连接天然穿过 NAT。

### 拓扑

```
┌──────────────────────────┐                            ┌──────────────────────────┐
│  本机 Server (家用 NAT后) │                            │  云上远程 daemon          │
│                          │  ──── WS dial (出站) ──► │  ws://0.0.0.0:8422/daemon │
│  监听 :8420 (HTTP)       │  (NAT 放行)                │  (公网 IP, 静态)         │
│  维护 daemon-link 池      │  ◄── rpc_request ────── │                          │
│                          │      (claim 请求)         │                          │
└──────────────────────────┘                            └──────────────────────────┘
```

关键点：**连接方向反了**，但 RPC 方向不变——daemon 仍然发 `tasks.claim`，server
仍然跑 `claim_tasks_for_daemon`。只是请求跑在"server dial 出来的连接"上。

### 3.1 两步配对（推荐）

**Server 端**：先确保有 secret（`agt server start` 首次运行会自动生成，也可显式 `agt server init`），
然后为远程机器生成一个邀请码：

```bash
$ agt server init                     # 首次：写入 ~/.agt/server.secret（0600）
$ agt daemon invite --name cloud-01 --url ws://1.2.3.4:8422 --add
Added cloud-01 ws://1.2.3.4:8422 (id=8f3c…) in /home/user/.agt/daemons

Run this on the daemon machine:

    agt daemon init agt1.eyJ2IjoxLCJpZCI6Ii4uLn0

The code embeds your server secret. Treat it like an SSH private key.
```

`--add` 会同时把这条目标登记进本机 `~/.agt/daemons`（带显式 `id=`）。不加 `--add` 就只打印邀请码，
此时 Server 不会去拨这台 daemon。

**远程机器**：把上面那行 `agt daemon init …` 原样粘过去执行：

```bash
$ agt daemon init agt1.eyJ2IjoxLCJpZCI6Ii4uLn0
Wrote server secret to /home/user/.agt/server.secret
Configured cloud-01 (ws://1.2.3.4:8422, id=8f3c…) in /home/user/.agt/daemons

Next: agt daemon start --detach
Make sure this machine's port 8422 is reachable from the Server.

$ agt daemon start --detach
# 日志（~/.agt/daemon.log）：Reverse-dial mode: listening on 0.0.0.0:8422/daemon
```

`agt daemon init <code>` 一次写入三件事，不需要再手工抄任何东西：

| 文件 | 内容 |
|---|---|
| `~/.agt/server.secret` | 与 Server 相同的 HMAC secret（`0600`） |
| `~/.agt/daemon.json` | `daemon_id` / `daemon_url` / `listen_mode=true` / `ws_port` |
| `~/.agt/daemons` | 自己的条目（`name url id=<uuid>`） |

已有明文 secret 且与新邀请码不同时，`init` 会拒绝覆盖（避免把一台已经配好的机器改坏）；
确认要换绑再加 `--force`。加 `--start` 则在配置完成后直接后台启动 daemon。

> **邀请码等同 SSH 私钥**：它内含 Server 的主 secret。不要贴进公开聊天、issue 或 CI 日志；
> 怀疑泄露就轮换 secret（重新 `agt server init --force`，或换新 secret 后对所有 daemon 重新 `init`）。

daemon 在 8422 监听，等 Server 来连。**这台机器的 8422 端口必须能从 Server 网络到达**
（云上公网 IP + 防火墙放行 8422）。

新增目标后需要让运行中的 Server 重新读一次目标列表：重启，或调用 `POST /api/daemons/refresh`
（设置页的刷新按钮走的就是这个端点），新目标会立刻开始拨号。

**设置页**：VS Code 客户端的设置页和 Web 控制台都有「连接与配对」区块，流程相同——
填写远程机器的**公网 IP 即可**（`ws://` 前缀和端口 8422 由界面固定拼接，不需要手写），
生成邀请码后一键复制。要撤下某台远程 daemon，在同一区块点「移除」：对应的
`~/.agt/daemons` 条目会被删除，Server 立即关闭它的 DaemonLink、不再向它派发任务，
刷新（或重启 Server）后它也会从 daemon 列表里消失。命令行等价物是 `agt daemon remove <name|url>`。

daemon 列表每行右侧的刷新按钮会调用 `POST /api/daemons/refresh?daemon_id=…`：
重新读取目标文件，并立即重连这一行对应的 daemon，不用等 Server 的自动重连退避（最长 60 秒）。

### 3.2 手工分步（与邀请码等价）

不想用邀请码、或要写进自动化脚本时，两边各自配置：

```bash
# ---- 远程 daemon 机器 ----
$ export AGT_SERVER_SECRET=xxx              # 与 Server 相同
$ export AGT_LISTEN_MODE=1                  # ← daemon 监听而不是 dial
$ export AGT_DAEMON_WS_PORT=8422            # 监听端口（默认 8422）
$ agt daemon start
```

Server 端把目标写进 `~/.agt/daemons` 后重启（或调用 `POST /api/daemons/refresh`）。
更持久的做法是让 daemon 把监听设置写进
`~/.agt/daemon.json`，之后启动不再依赖环境变量：

```bash
$ agt daemon config set --listen-mode --ws-port 8422
```

### 3.3 `daemon_id` 与身份

远程 daemon 与 Server 必须对该 daemon 的 id 达成一致。`daemon_id` 按以下优先级解析
（两边各自独立计算，**不需要互相传**）：

1. `AGT_DAEMON_ID` 环境变量
2. `~/.agt/daemon.json` 的 `daemon_id`（`agt daemon init` 写入）
3. `~/.agt/daemons` 中指向本机的条目的 `id=<uuid>`
4. `uuid5(NS, daemon_url)` —— 老安装的派生方式
5. 随机 `uuid4`（仅在前几项都拿不到时，本地直连场景）

使用 `id=` 之后，身份与 URL 拼写解耦：换域名、换端口、改 `ws://` 写法都不会再导致
「Server 认为你是另一台机器」。老安装无需迁移——第 4 步保证了原有 uuid5 身份不变。

### 3.4 daemon 列表文件格式

`~/.agt/daemons`（文本文件，`#` 起注释行）：

```
name URL [id=<uuid>] [capabilities]
```

```bash
# ---- 远程 daemon 上 ----
$ cat > ~/.agt/daemons <<EOF
# 注释行被忽略
local ws://0.0.0.0:8422
cloud-01 ws://1.2.3.4:8422 id=8f3c…
EOF

# ---- Server 机器上 ----
$ cat > ~/.agt/daemons <<EOF
cloud-01 ws://1.2.3.4:8422 id=8f3c…
cloud-02 wss://5.6.7.8:8422 id=2b7d…
EOF
$ chmod 600 ~/.agt/daemons
```

- `name` 是人类可读的标签，不影响身份。
- `id=` 可省略：省略时回落到 `uuid5(URL)`。**带 `id=` 时两边必须一致**（邀请码/`init` 会自动保证）。
- `id=` 之后可继续跟 `capabilities` 静态标签，如 `containers,logs`；顺序不影响解析。
- 建议 `chmod 600 ~/.agt/daemons`，因为该文件决定了 Server 会拨到哪里去。

`AGT_DAEMONS_FILE` 可覆盖路径。Server 启动时读这个文件，对每个 daemon 起一个 dial 任务，
指数退避重连（1s → 60s）。

### 双向认证

reverse-dial 握手现在是双向认证：

1. Server 用 `AGT_SERVER_SECRET` 签发 JWT（`X-Server-Token`），其中 `aud` 绑定目标 daemon 的 `daemon_id`。
2. daemon 校验签名、`aud` 和 `iss`，拒绝不属于自己的 token。
3. daemon 回传自己的 JWT（`daemon_token`），其中 `sub` 包含自己的 `daemon_id`。
4. Server 校验 daemon 的 token，确认 `sub` 与配置中的 `daemon_id` 一致，才接受 RPC。

这意味着：
- 即使某 token 被截获，也只能用于特定 daemon，不能跨 daemon 滥用。
- 假 daemon 无法骗取 Server 的任务和敏感数据。

### Server 端启动

```bash
$ AGT_SERVER_HOST=0.0.0.0 agt server start
# 首次运行会打印 Generated server secret at ~/.agt/server.secret
# 日志：
#   Reverse-dial pool: 2 daemon link(s) → <daemon_id-for-cloud-01>, <daemon_id-for-cloud-02>
#   DaemonLink <daemon-id> connected (ws://1.2.3.4:8422)
```

`AGT_SERVER_HOST` 只影响 HTTP API 的绑定地址，与反向拨号无关——反向拨号是 Server 主动出站。

### 与 daemon-dial 路径的关系

本文只覆盖 **reverse-dial（推荐）**。`AGT_LISTEN_MODE` 设为 `1` 后：

- daemon 不主动连 server，**只监听 8422 等 server 来连**
- server 读 `~/.agt/daemons` 主动 dial 出去
- daemon 通过这条 server-dialed 连接发 `tasks.claim`（RPC 方向不变）

`AGT_USE_WS_CLAIM=1`（daemon 主动 dial server 的老路径）也仍然可用，但只用于 Server 暴露公网入口的场景。**远程 daemon 在 NAT 后面**时**必须用 `LISTEN_MODE=1`**，老路径连不上。

简单说：**Server 在 NAT 后**（家用 / 公司网 / 大多数场景）→ `LISTEN_MODE=1`；
**Server 有公网入口**（公网 ECS / 反向代理）→ `USE_WS_CLAIM=1` 也能用。

### 验证

```bash
# 远程 daemon 上确认在监听
$ ss -tlnp | grep 8422
# 应看到 0.0.0.0:8422

# 本机 server 上看 daemon-link 池状态
# （看 server 日志: "DaemonLink daemon-cloud-01 connected"）
$ grep DaemonLink ~/.agt/server.log | tail

# 提交任务,看 daemon_id 是不是远程那个
$ agt task submit --prompt "..." --provider claude
$ python3 -c "import sqlite3; c=sqlite3.connect('~/.agt/agt.db'); print(c.execute('SELECT task_id, daemon_id FROM tasks ORDER BY created_at DESC LIMIT 1').fetchone())"
# 应该看到 daemon_id 与 ~/.agt/daemons 中该条目的 id= 一致 (而非 'cli-direct')

# 检查双向认证是否成功（连接后不会断）
$ agt daemon list
# ONLINE=yes 且 daemon_id 与 ~/.agt/daemons 中该条目的 id= 一致
```

### 定向到指定 daemon 的 agent（run / chat）

反向连接下通常有多台 daemon。默认 `agt task submit --provider claude` 是**自由任务**：
谁先 claim 谁跑。要**明确指定某台 daemon 的某个 agent**，加 `--daemon`：

```bash
# 单次执行：指定 daemon-cloud-01 上的 claude（走 server 定向 + 轮询到完成）
$ agt run -p "..." --provider claude --daemon daemon-cloud-01

# 交互聊天：每轮都钉在 daemon-cloud-01（session 续接也钉在同一台）
$ agt chat --provider claude --daemon daemon-cloud-01

# 给第一轮附文件：先上传到 Server，再以 agt-upload:// 引用下发，daemon 落到 .agt/inputs/<文件名>
$ agt chat --provider claude --daemon daemon-cloud-01 --dir=/srv/app-a \
    --file ./logs/app.log --file ./docs/需求.md

# 查看思考过程 / 工具调用 / 每轮 token 与费用（三个开关可单独使用，本地与远程 chat 均生效）
$ agt chat --provider claude --show-thinking --show-tools --usage

# 只提交、自己查状态
$ agt task submit -p "..." --provider claude --daemon daemon-cloud-01

# 远程会话查看 / 恢复：SQLite 是信息源，不读写任何 sessions.json
$ agt chat --list --daemon=daemon-cloud-01 --dir=/srv/app-a  # 按 daemon 侧 cwd 过滤，带 DIR 列
$ agt chat --resume <sid> --daemon=daemon-cloud-01  # 经 Server 反查 provider/identity/dir 并续接；历史转录由 Server 向 daemon 拉取后回显

# 批量永久删除指定 daemon / provider 下的会话
$ agt session delete <sid-1> <sid-2> --daemon daemon-cloud-01 --provider claude --yes
```

规则：

- `--daemon` 的值必须等于 `~/.agt/daemons` 里登记的 `daemon_id`（server 侧静态配置）
- 目标 daemon 必须已注册该 provider，否则 submit 直接 `400`（不会让任务永远 pending）
- 不带 `--daemon` 的 `run` / `chat` 走**本地直连**（不经过 server）；带 `--daemon` 才走 server 定向
- 定向任务只有目标 daemon 能抢；自由任务（不带 `--daemon`）任何 provider 匹配的 daemon 都能抢，两种并存
- 目标 daemon 离线时，任务保持 pending；`run --daemon` 等 5 分钟超时后提示「daemon 是否在线」，不会自动降级到别的机器

> `--server-url` 默认 `http://127.0.0.1:8420`（可用 `AGT_SERVER_URL` 覆盖）。server 不在本机时记得带上。

### Web 控制台：项目目录（本地必填，远程可选）

Web 控制台新建项目时**本地目录必填**——它是 Server 机器上的目录（默认必须位于
`AGT_CONSOLE_WORKSPACE_ROOTS` 之内），也是该项目运行历史的归属目录（`jobs.cwd`）。

若该项目还要跑在远程 daemon 上，再填**远程工作目录**（可选）：

1. 选择目标 daemon；
2. 像本地一样在目录树里浏览/点选——Server 通过该 daemon 的反向连接调用 `fs.list`
   远程列目录，**只能看到该 daemon `AGT_AGENT_CWD_ALLOWLIST` 之内的路径**，
   所以「能选到的目录」一定「能被执行」；
3. 也可以直接输入远程机器上的绝对路径。

规则：

- 创建时 Server 会向 daemon 校验该目录；daemon 暂时离线时允许先保存，等上线后首次使用再校验。
- 同一台 daemon 上的同一目录不能同时绑定两个项目。
- 填了远程目录后，chat / SOP / Goal 三个模式只要选中该 daemon，就**默认使用这个远程工作目录**；
  没填的话，三个模式各自在里面再选择。
- 远程目录仅供项目默认使用，本地 `path` 始终存在，因此项目列表、运行历史都不依赖 daemon 在线。

**运行历史的归属目录**：项目下的 chat / SOP / Goal 运行一律把本地 `path` 记为运行历史目录
（`jobs.cwd`），远程运行时另把远程目录存进执行目录（`jobs.agent_cwd`）。所以：

- 远程会话也会出现在该项目的会话列表里，并标注它所在的 daemon；
- 项目卡片的运行计数按项目合并统计，不区分远程还是本地；
- 打开/继续一条远程会话时，任务仍落在原来那台 daemon 的远程目录上；
- 早期版本创建的远程 chat 会话把远程目录当成了历史目录，控制台查询时会同时匹配
  `path` 与项目的远程目录（`GET /api/runs` 允许重复传 `cwd`），所以这些旧会话同样可见；
  导出类接口若按 `cwd` 精确过滤，记得把两个路径都传上。

---

## 4. 完整 env 变量速查

| 变量                       | 在哪设               | 默认                  | 必填 | 说明                                             |
| -------------------------- | -------------------- | --------------------- | ---- | ------------------------------------------------ |
| `AGT_SERVER_SECRET`      | Server + 所有 Daemon | —                    | ✅   | 共享 HMAC secret                                 |
| `AGT_SERVER_HOST`        | Server               | `127.0.0.1`         |      | 远程 daemon 要改成`0.0.0.0`                    |
| `AGT_DAEMON_ID`          | 每个 Daemon          | 自动`uuid4()[:8]`   |      | 多机时**建议**手工设                       |
| `AGT_DAEMON_PORT`        | 每个 Daemon          | `8421`              |      | 本地资源服务端口（spawn agent 用）               |
| `AGT_DAEMON_LOCAL_TOKEN` | 每个 Daemon          | 随机                  |      | spawn agent 用的本地 token                       |
| `AGT_LISTEN_MODE`        | 每个 Daemon          | `0`                 |      | 远程 daemon 设`1`，改为监听等 server 连        |
| `AGT_DAEMON_WS_PORT`     | 远程 Daemon (listen) | `8422`              |      | daemon WS 监听端口（仅`LISTEN_MODE=1` 时生效） |
| `AGT_MAX_CONCURRENT`       | 每个 Daemon            | `1`                 |      | 同时跑几个 agent；优先级高于 `~/.agt/daemon.json`           |
| `AGT_AGENT_TIMEOUT`        | 每个 Daemon            | `600`               |      | 单个 task 最多跑多久（秒）；优先级高于 `~/.agt/daemon.json`      |
| `AGT_WORK_DIR`             | 每个 Daemon            | `~/.agt/workspaces` |      | 任务工作目录；优先级高于 `~/.agt/daemon.json`                |
| `AGT_AGENT_CWD_ALLOWLIST`  | 每个 Daemon            | `~`（home）           |      | 允许的 agent cwd 根目录，冒号分隔；优先级高于 `~/.agt/daemon.json` |
| `AGT_DAEMONS_FILE`       | Server               | `~/.agt/daemons`    |      | 远程 daemon 列表文件路径                         |

---

## 5. 常见配置组合

### 单机（最简单）

```bash
# 终端 1
$ export AGT_SERVER_SECRET=xxx
$ agt server start

# 终端 2（同一机）
$ export AGT_SERVER_SECRET=xxx
$ agt daemon start
```

### Server 在 A，daemon 在 A + B（混合）

A 机既是 server 也是一台 daemon；B 是远程 daemon。

```bash
# A：server（绑 0.0.0.0）
$ export AGT_SERVER_SECRET=xxx
$ AGT_SERVER_HOST=0.0.0.0 agt server start

# A：daemon（本地，连自己 server；普通 HTTP claim）
$ export AGT_SERVER_SECRET=xxx
$ export AGT_DAEMON_ID=daemon-A
$ agt daemon start

# B：daemon（远程，反向连接）
$ export AGT_SERVER_SECRET=xxx   # 与 A 一致！
$ export AGT_LISTEN_MODE=1        # 监听 8422，等 server 来连
$ export AGT_DAEMON_WS_PORT=8422  # 默认就是 8422
$ export AGT_DAEMON_ID=daemon-B
$ agt daemon start

# A 的 server 上：把 B 加进 daemon 列表
$ cat > ~/.agt/daemons <<EOF
daemon-B ws://<B的公网IP>:8422
EOF
```

### 多 remote worker，B / C / D 都不在 A 上

每台 daemon 都用 `LISTEN_MODE=1`；server 的 `~/.agt/daemons` 列出所有：

```bash
$ cat > ~/.agt/daemons <<EOF
daemon-B ws://<B-IP>:8422
daemon-C ws://<C-IP>:8422
daemon-D ws://<D-IP>:8422
EOF
```

每台 daemon 上 `AGT_DAEMON_ID=daemon-B` / `daemon-C` / `daemon-D` 区分。

---

## 6. 验证清单

跑完上面的步骤，应该全部满足：

- [ ] `agt server start` 启动后能访问 `http://<server-host>:8420/docs`（FastAPI 自动文档）
- [ ] loopback Server 启动后能访问 `http://127.0.0.1:8420/`（浏览器控制台）
- [ ] 每台机器 `agt daemon start` 后，`agt runtime list` 能看到该 daemon 注册的 runtime
- [ ] `last_seen` 字段每 30s 滚动（heartbeat 活着）
- [ ] 提交 provider='claude' 的任务，**只有**有 claude 的 daemon claim 到
- [ ] 提交 provider='codex' 的任务，**只有**有 codex 的 daemon claim 到
- [ ] `agt task status <task_id>` 显示 `daemon_id` 是某台 daemon
- [ ] `agt run -p "..." --daemon daemon-cloud-01` 的任务，`daemon_id` 一定是 daemon-cloud-01（定向生效）
- [ ] 远程 daemon 上 `pip install` 新 agent 后，≤30s `agt runtime list` 自动反映

---

## 7. 排错速查

| 现象                                   | 大概率原因                            | 排查                                              |
| -------------------------------------- | ------------------------------------- | ------------------------------------------------- |
| `connection refused`                 | Server 没绑 0.0.0.0                   | `ss -tlnp \| grep 8420`                          |
| `invalid signature` (WS 握手)        | 两边`AGT_SERVER_SECRET` 不一致      | 两边`echo $AGT_SERVER_SECRET` 对比              |
| daemon 起来但拿不到任务                | provider 没在它的 runtime 里          | `agt runtime list` 看 daemon 的 provider        |
| 任务一直 pending                       | 没有 daemon 能跑该 provider           | 装该 provider 的机器起 daemon                     |
| `agt runtime list` 看不到远程 daemon | 远程 daemon 没起来 / 网络不通         | `nc -zv <server-ip>8420`                        |
| 远程装了新 agent 后 list 不更新        | （已修复）需要等 ≤30s heartbeat 同步 | 等一下；如 1 分钟还没出来查 heartbeat 路径        |
| daemon 日志`WSRPC connect failed`    | 网络/secret/防火墙 三选一             | 先`nc -zv`，再 `echo $AGT_SERVER_SECRET` 对比 |

---

## 8. 持久化 secret（推荐方式二）

最简方式：运行 `agt server init`，它会生成 secret 并写到 `~/.agt/server.secret`，Server 和 Daemon 都会自动读取，无需再手动 `export`：

```bash
$ agt server init
```

如果你还需要同时管理多个环境变量（如 `AGT_DAEMON_ID`、`AGT_LISTEN_MODE` 等），可以继续使用 `~/.config/agt/env`：

```bash
# 1. 创建目录
$ mkdir -p ~/.config/agt

# 2. 从 agt 生成的 secret 文件读取，写到 env 文件
$ echo "export AGT_SERVER_SECRET=$(cat ~/.agt/server.secret)" > ~/.config/agt/env

# 3. 收紧权限
$ chmod 600 ~/.config/agt/env

# 4. 让每个新 shell 自动加载（bash）
$ echo 'source ~/.config/agt/env 2>/dev/null' >> ~/.bashrc

# zsh 用户把 .bashrc 换成 .zshrc
```

效果：

- 每次开新 shell，`AGT_SERVER_SECRET` 自动就位
- `agt server start` / `agt daemon start` 不用再手动 export
- 文件权限 600，只有你能读；其他用户看不到

### 8.1 多变量一起管理

`~/.config/agt/env` 里可以放任意多个变量：

```bash
$ cat ~/.config/agt/env
export AGT_SERVER_SECRET=xxx
export AGT_DAEMON_ID=daemon-A
# 远程 daemon: 反向连接（监听 8422 等 server 连）
export AGT_LISTEN_MODE=1
export AGT_DAEMON_WS_PORT=8422
```

远程机器上的 daemon 同样适用（先 `scp` 把文件传过去）：

```bash
# 本机 → 远程 B 机
$ scp ~/.config/agt/env user@remoteB:~/.config/agt/env
$ ssh user@remoteB 'chmod 600 ~/.config/agt/env && echo "source ~/.config/agt/env 2>/dev/null" >> ~/.bashrc'
```

### 8.2 备份与迁移

```bash
# 备份到加密盘
$ cp ~/.config/agt/env /path/to/backup/agt-env

# 换机器时恢复
$ mkdir -p ~/.config/agt
$ cp /path/to/backup/agt-env ~/.config/agt/env
$ chmod 600 ~/.config/agt/env
```

⚠️ **安全提醒：**

- 这个文件**别**提交到 git。在仓库根目录的 `.gitignore` 里加：
  ```gitignore
  .config/agt/env
  ```
- 别 echo 到聊天 / 截图里（明文 secret 一次就完）
- 多机器用**同一** secret；每改一次，全部机器都要同步

### 8.3 其他持久化方式（场景化）

| 场景                        | 推荐                                                 | 备注                 |
| --------------------------- | ---------------------------------------------------- | -------------------- |
| 本机个人开发                | 方式二（`~/.config/agt/env`）                      | 最简单               |
| 多环境隔离（dev/test/prod） | [direnv](https://direnv.net/) 进项目目录自动加载/卸载 | 多装一个工具         |
| 远程机器长期跑的 daemon     | systemd`EnvironmentFile=/etc/agt.env`              | 仅 root 可读，最安全 |
| 临时测试                    | 直接`export`，不落盘                               | 用完即弃             |

---

## 9. 配置文件清单

跑通 agt 需要三个配置文件（server 端两个、daemon 端一个），外加几个运行时自动生成的文件。

### 9.1 一图总览

| 文件                  | 在哪台机器                       | 必填       | 用途                  |
| --------------------- | -------------------------------- | ---------- | --------------------- |
| `~/.config/agt/env` | Server**和** daemon 各一份 | ✅         | 环境变量（secret 等） |
| `~/.agt/daemons`    | Server                           | 反向连接时 | 远程 daemon 列表      |
| `~/.agt/agt.db`     | Server                           | 自动生成   | SQLite 数据库         |
| `~/.agt/daemon.json`  | Daemon                           | 可选         | Daemon 执行资源配置（并发/超时/工作目录/allowlist），用 `agt daemon config` 管理 |
| `~/.agt/daemon.pid` | Daemon                           | 自动生成   | 进程 PID              |

### 9.2 Server 端：`~/.config/agt/env`

放 secret 等环境变量，新 shell 自动加载（创建方法见 §8）：

```bash
$ cat ~/.config/agt/env
export AGT_SERVER_SECRET=a57d9f...    # 与所有 daemon 共享的 HMAC 密钥
export AGT_SERVER_HOST=0.0.0.0        # 可选；默认 0.0.0.0
export AGT_SERVER_PORT=8420           # 可选；默认 8420
export AGT_DAEMONS_FILE=~/.agt/daemons  # 可选；覆盖 daemon 列表文件路径
```

### 9.3 Server 端：`~/.agt/daemons`（反向连接用）

纯文本，每行一个 daemon：`daemon_id URL`，`#` 起注释行、空行忽略。

```bash
$ cat ~/.agt/daemons
# 注释行被忽略
daemon-cloud-01 ws://203.0.113.10:8422
daemon-cloud-02 wss://secure.example:8422
```

规则：

- 一行一个 daemon，空格分隔 `daemon_id` 和 `URL`
- URL **必须**以 `ws://` 或 `wss://` 开头，否则该行被忽略（log warning）
- 文件不存在或为空 → server 正常启动，但没有反向连接
- 改完需重启 server（或调用 `POST /api/daemons/refresh`）才生效（`from_env()` 只在启动时读一次）
- 路径可用 `AGT_DAEMONS_FILE` 覆盖

### 9.4 远程 daemon 端：`~/.config/agt/env`

放 daemon 自己的环境变量：

```bash
$ cat ~/.config/agt/env
export AGT_SERVER_SECRET=a57d9f...        # 与 server 端**完全一致**
export AGT_DAEMON_ID=daemon-cloud-01     # 稳定标识，多机时必填
export AGT_LISTEN_MODE=1                 # 反向连接（监听 8422 等 server 连）
export AGT_DAEMON_WS_PORT=8422           # 可选；监听端口，默认 8422
```

本地 daemon（与 server 同机）**不需要** `AGT_LISTEN_MODE` / `AGT_DAEMON_WS_PORT`——
直接 `export AGT_SERVER_SECRET=xxx` + `export AGT_DAEMON_ID=xxx` 走 HTTP claim 即可。

### 9.5 运行时自动生成的文件（不用手配）

| 文件                     | 谁生成          | 内容                                                        | 清理方式                     |
| ------------------------ | --------------- | ----------------------------------------------------------- | ---------------------------- |
| `~/.agt/agt.db`        | Server          | SQLite 库（chat / sop / goal 的运行、步骤和 session 信息） | 删掉后重启 server 自动重建   |
| `~/.agt/daemon.pid`    | Daemon          | 当前 daemon 进程的 PID                                      | `agt daemon stop` 自动清理 |
| `<cwd>/.agt/sessions.json` | 本机直连 chat | 仅 bare `agt chat` 使用的本地 session 索引                   | 本机 chat 不再需要时可删     |
| `~/.agt/sessions.json` | 旧版（Phase 1） | 不再自动读写，仅保留历史迁移用途                             | 可手动迁移后删除             |

---

## 10. 下一步

完整命令清单 → [`commands.md`](commands.md)

实现细节 → [`../impl/phase3-design.md`](../impl/phase3-design.md)

要开发 / 改代码 → 仓库根目录的 [`../../README.md`](../../README.md)
