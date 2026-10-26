# agt

**中文** | [English](README.md)

**agt** 是一个本地 AI 编码 Agent CLI 桥接器。它能发现已安装的 agent、从终端直接执行，并提供 Server/Daemon 层实现任务队列、并发执行与多机调度——包括 MCP 驱动的动态子 Agent 派生（spawn）、静态 DAG 流水线（SOP）、产物管理与 MCP 运行时目录。

这是一个面向未来多 Agent 桌面客户端的实验性 Python 基础项目。桌面端技术栈暂未选定。

## 功能特性

### Agent 执行

- **发现**本地 Agent CLI：Claude Code、Codex、Cursor Agent、GitHub Copilot、Qwen、Kimi、OpenCode、OpenClaw。
- **一次性执行**（`run`）或**交互式对话**（`chat`），支持会话续接（provider 原生 `--resume`）。
- **流式输出**：本地 Claude Code 逐 token 实时打印；远程 `--daemon` 对话通过 SSE 增量推送（`snapshot → delta* → done`）。
- **会话恢复**：跨调用持久化 session ID，随时 `--resume` 续接。

### 多 Agent 编排

- **动态派生（Dynamic Spawn）**：支持 MCP 的 Agent 调用 `spawn_subagents` 提交结构化子任务；工具通过 task token 绑定当前 Goal Step，立即创建子 Job、执行预算与幂等校验，全部完成后自动追加 synthesis Step。未接 MCP 的 provider 继续使用 `AGT-SPAWN <json>` 回退。
- **静态 DAG 流水线（SOP）**：用 YAML 声明可复用的多 Agent 流程，支持 `depends_on` 依赖、`${node.output}` 数据流引用、依赖门控激活（`blocked → queued`）。末端节点产物即最终结果。
- **身份路由**：Identity（DID）绑定到 `(daemon, provider)` 运行时；子任务按能力 + 亲和 + 负载动态路由到不同 daemon，chat 会话保持钉住同一台。
- **取消级联**：取消操作级联到 spawn 后代与 DAG 下游；运行中的远程子进程通过 `tasks.cancel` 推送（反向拨号）或 claim 响应（HTTP）终止。

### 产物与数据流

- **产物登记**：Agent 将交付文件放在 `.agt/outputs/`（可通过 `manifest.json` 声明元数据）；Server 登记 lineage/mime/摘要/sha256，生成 `agt-artifact://<id>` 引用。
- **按需传输**：同 daemon 消费零拷贝（本地路径提示）；跨 daemon 懒拉取字节（反向拨号 RPC），校验 sha256 后在 Server 物化。
- **产物 CLI**：`agt artifact list --session <sid> [--daemon <id>]` 查看产物；`agt artifact get <uri|id>` 单条下载或 `--session --out-dir` 批量下载。

### 基础设施

- **Server**：FastAPI + SQLite（`jobs`、`tasks`、`runtimes`、`sessions`、`identities`、`identity_bindings`、`artifacts`、`job_parents`、`spawn_batches`），Alembic 迁移，HMAC 任务令牌。
- **Daemon**：HTTP 长轮询或**反向拨号 WebSocket**（NAT 穿透），信号量并发控制（`AGT_MAX_CONCURRENT`），心跳 + 运行时注册。
- **MCP 工具**：`/mcp/`（Streamable HTTP）同时暴露 `list_runtimes` 与需要 task token 的 Goal `spawn_subagents`。Claude Code 通过项目 `.mcp.json` 接入；Codex 通过全局 `~/.codex/config.toml` 接入；配置只引用 `AGT_TASK_TOKEN` 环境变量，不落真实 token。
- **Provider 适配器**：命令构造、会话定位、流式 sink、resume 拒绝检测、MCP 配置注入均按 provider 注册——新增 Agent 只需加一行注册表，无需改执行器。

## 环境要求

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) 或 pip
- 至少一个受支持的 Agent CLI 已安装并认证

## 安装

```bash
git clone <repository-url>
cd agt
uv sync
```

或用 pip：

```bash
python -m pip install -e .
```

两种方式都提供 `agt` 命令。用 uv 时在命令前加 `uv run`。

## 快速上手

```bash
# 发现 Agent
agt probe

# 一次性执行（本地，流式输出）
agt run --provider claude --prompt "解释这个仓库"

# 交互式对话（本地）
agt chat --provider claude

# 远程对话（钉到某个 daemon，SSE 流式）
agt chat --provider codex --daemon <daemon-id>

# 查看 / 恢复会话
agt chat --list
agt chat --list --daemon <daemon-id>   # 跨目录查看远程会话
agt chat --resume <session-id> --daemon <daemon-id>
```

会话元数据存储在 `<project>/.agt/sessions.json`。在 Git 仓库中，agt 会幂等地把 `.agt/` 加入 `.gitignore`。

## Server / Daemon 模式

```bash
agt server init

agt server start --detach
agt daemon start --detach

agt task submit --prompt "用三个词打招呼"
agt task status <task-id>

agt daemon stop
agt server stop
```

`agt server init` 会把共享 secret 写入 `~/.agt/server.secret`；`agt server start` 和
`agt daemon start` 都会自动读取。显式设置的 `AGT_SERVER_SECRET` 优先级更高。

默认 Server 监听 `127.0.0.1:8420`，OpenAPI 文档在 [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs)。

远程 Daemon 通过 HTTP 或**反向拨号 WebSocket**（Server 主动拨出，穿透 NAT）接入。详见[快速启动指南](<docs/guide%20docs/quick_start.md>)。

## SOP 示例

```yaml
# pipeline.yaml
name: review-and-fix
identities:
  reviewer: {did: did:agt:reviewer, provider: claude}
  tester: {provider: codex}
tasks:
  - name: review
    identity: reviewer
    input: 审查 diff 并把结论写到 .agt/outputs/
    output: review-report
  - name: test
    identity: tester
    input: 基于 ${review.review-report} 跑测试
    depends_on: [review]
    output: test-result
```

```bash
agt sop validate pipeline.yaml
agt sop run pipeline.yaml
```

## CLI 总览

| 命令                 | 说明                                                      |
| -------------------- | --------------------------------------------------------- |
| `agt probe`        | 探测已安装的 Agent CLI。                                  |
| `agt run`          | 本地或通过 Daemon 执行一次 prompt。                       |
| `agt chat`         | 开始、列出或恢复交互式 Agent 会话。                       |
| `agt sop`          | 静态 DAG 流水线：validate / submit / run / status / log。 |
| `agt artifact`     | 查看和下载声明的交付产物。                                |
| `agt server`       | 启动、停止或查看任务服务器。                              |
| `agt daemon`       | 启动、停止、查看、列出或展示 Daemon。                     |
| `agt task`         | 提交任务并查看或清理任务状态。                            |
| `agt runtime list` | 列出 Server 上注册的运行时。                              |
| `agt identity`     | 管理身份、身份文件和运行时绑定。                          |
| `agt token issue`  | 签发任务级令牌（用于 API 测试）。                         |

完整参数说明：[CLI 命令参考](<docs/guide%20docs/commands.md>)。

## 架构

```text
┌───────────┐   HTTP / WS    ┌───────────┐  claim / report  ┌────────────┐
│   CLI     │ ◄────────────► │  Server   │ ◄──────────────► │  Daemon    │
└───────────┘                └───────────┘                  └────────────┘
                                  │                               │
                                  │ SQLite (jobs/tasks/artifacts) │ 每次新子进程
                                  │ MCP 目录 + spawn (/mcp)       ▼
                                  │ SOP DAG (job_parents)    ┌────────────┐
                                  ▼                          │ Agent CLI  │
                            ┌───────────┐                    └────────────┘
                            │ Artifacts │
                            └───────────┘
```

### 核心不变式

- 每次 Agent 调用都是**独立子进程**；agt 不保持 Agent 进程存活。
- 会话连续性委托给 provider，通过持久化 session ID 恢复。
- Provider 特定行为隔离在**运行时适配器**（注册表模式）之后。
- **Server** 负责协调和持久化；**Daemon** 负责本地执行。
- Spawn 谱系（`parent_job_id`）与 DAG 依赖（`job_parents`）是正交关系，可共存。

### 项目结构

```text
agt/
├── a2a.py         A2A 形状的内部对象模型（Message/Part/Artifact）
├── agent_history  Provider 转录读取器（Claude Code / Codex）
├── ids.py         稳定 ID 派生（基于 daemon URL 的 uuid5）
├── cli/           Click 命令行接口
│   ├── main.py    所有核心命令（probe/run/chat/server/daemon/task/...）
│   └── sop.py     SOP 流水线命令（validate/submit/run/status/log）
├── comm/          HTTP + WebSocket 客户端、WS-RPC 链路
├── daemon/        运行时注册、任务认领、反向拨号 WS 服务端、
│                  MCP 代理、运行中任务追踪、本地资源服务
├── probe/         Agent CLI 发现（which/shell 回退）
├── runtime/       Provider 适配器注册表：
│                  命令构造、会话定位、流式 sink、
│                  resume 拒绝检测、MCP 配置注入
├── server/        FastAPI 应用：
│   ├── routes/    tasks、jobs、artifacts、identities、sessions、
│   │              daemons、runtimes、heartbeat、sop
│   ├── services/  Job 生命周期、spawn 编排、产物登记、
│   │              claim、report、选路、SOP 展开、运行时目录
│   ├── mcp_server MCP 目录 + 认证 Goal spawn（FastMCP）
│   └── stream_hub SSE 发布/订阅（远程流式）
├── sop/           SOP DSL 解析 + 静态校验
└── task/          任务执行器（3 种模式）、会话存储、产物扫描、
                   spawn 意图解析、消息渲染
```

## 配置

| 变量                    | 默认值                    | 说明                                                  |
| ----------------------- | ------------------------- | ----------------------------------------------------- |
| `AGT_SERVER_URL`      | `http://127.0.0.1:8420` | CLI 和 Daemon 使用的 Server 地址。                    |
| `AGT_SERVER_SECRET`   | 回退 `~/.agt/server.secret` | Server 与 Daemon 共用的 HMAC 密钥。                |
| `AGT_SERVER_PORT`     | `8420`                  | Server 端口。                                         |
| `AGT_DB_PATH`         | `~/.agt/agt.db`         | SQLite 数据库位置。                                   |
| `AGT_MAX_CONCURRENT`  | `1`                     | 每个 Daemon 最大并发任务数。                          |
| `AGT_AGENT_TIMEOUT`   | `600`                   | Agent 子进程超时（秒）。                              |
| `AGT_LISTEN_MODE`     | `0`                     | 设为`1` 启用反向拨号（NAT 穿透）模式。              |
| `AGT_DAEMON_WS_PORT`  | `8422`                  | 反向拨号 WS 监听端口。                                |
| `AGT_DAEMON_MCP_PORT` | `8428`                  | 本地回环 MCP 代理端口（远程 Daemon）。                |
| `AGT_EXTRA_AGENTS`    | —                        | 额外 Agent：`cmd:provider:显示名[:stream_format]`。 |

### 常用文件

| 数据              | 位置                                         |
| ----------------- | -------------------------------------------- |
| Server 数据库     | `~/.agt/agt.db`                            |
| 项目对话会话      | `<project>/.agt/sessions.json`             |
| 身份文件          | `~/.agt/identities/<slug>/AGENTS.md`       |
| Server PID / 日志 | `~/.agt/server.pid`、`~/.agt/server.log` |
| Daemon PID / 日志 | `~/.agt/daemon.pid`、`~/.agt/daemon.log` |

## 文档

- [CLI 命令参考](<docs/guide%20docs/commands.md>)
- [端到端快速启动](<docs/guide%20docs/quick_start.md>)
- [新 Provider 接入指南](<docs/guide%20docs/provider-registration.md>)
- [编排架构设计](docs/impl/agent-orchestration-design.md)
- [SOP 设计](docs/impl/phase5-sop-design.md)
- [动态 Spawn 设计](docs/impl/phase3-dynamic-spawn-design.md)

## 开发

```bash
uv run agt --help
.venv/bin/python -m pytest
```

> 在有 pytest 插件冲突的系统上（如 ROS），使用：
> `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest`

保持改动小，Provider 特定逻辑放在运行时适配器接口之后。

## 状态

积极开发中。多 Agent 任务编排（动态 spawn + 静态 SOP）和产物流水线已可用。桌面客户端在规划中；当前重点是 CLI、运行时、Server 和 Daemon 基础设施。
