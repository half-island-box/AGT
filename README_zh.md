# agt

**中文** | [English](README.md)

**面向 AI 编码 Agent CLI 的本地桥接器与编排层。**

`agt` 可以发现本机已安装的编码 Agent，并通过统一的命令行、Server 和
Daemon 接口使用它们。在基础执行能力之上，项目提供持久会话、跨机器调度、
Goal 驱动的子 Agent 编排、静态 DAG 工作流和统一产物注册表。

> [!NOTE]
> `agt` 正在积极开发中。CLI、Server、Daemon、Goal 模式和 SOP 工作流已经
> 可用；桌面客户端仍在规划中，技术栈尚未确定。

## 功能概览

- **统一 Agent 执行**：通过一套 CLI 运行和对话不同 Provider，支持原生会话
  续接和流式输出。
- **Goal 编排**：root Agent 拆解目标，在全局预算内派生子 Agent，并综合各
  节点的交付物。
- **静态工作流**：使用 YAML 定义可复用的多 Agent DAG，支持 `depends_on`
  和 `${node.output}` 数据引用。
- **Artifact 交接**：把声明的交付物注册为稳定的 `agt-artifact://<id>`，
  记录血缘、摘要和 SHA-256。
- **分布式 Worker**：通过 Server 队列化执行，并使用 HTTP 或反向拨号
  WebSocket 跨机器调度。
- **Identity 路由**：定义 reviewer、tester 等角色，并按 Provider、Daemon
  亲和性和 Runtime 可用性路由。
- **MCP 与编辑器集成**：向支持 MCP 的 Agent 暴露子 Agent Spawn 工具，并
  提供早期 VS Code 扩展，用于 Chat、Goal 和 SOP 编辑。

当前支持 Claude Code、Codex、Cursor Agent、GitHub Copilot、Qwen、Kimi、
OpenCode、OpenClaw，以及自定义 CLI。

## 环境要求

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) 或 pip
- 至少一个受支持的 Agent CLI 已安装并完成认证

## 安装

```bash
git clone https://github.com/half-island-box/agent-team.git
cd agent-team
uv sync
uv run agt --help
```

也可以使用 pip：

```bash
python -m pip install -e .
agt --help
```

## 快速开始

### 运行或对话 Agent

```bash
agt probe
agt run --provider claude --prompt "解释这个仓库"
agt chat --provider claude

# 恢复持久会话
agt chat --list
agt chat --resume <session-id>
```

### 编排开放目标

```bash
agt goal run "审查这个仓库并修复所有 P0 问题"
agt goal status <root-job-id>
agt goal runs
```

Goal 模式会创建动态 Job 树。Agent 可以递归委派、暂停并等待用户输入，
在 synthesis 阶段重试失败任务，也可以把成功运行导出为可复用的 SOP：

```bash
agt goal input <job-id> "使用中文，并按模块拆分任务"
agt goal export <root-job-id> -o pipeline.yaml
```

### 运行可复用工作流

```yaml
# pipeline.yaml
name: review-and-fix
identities:
  reviewer: {did: did:agt:reviewer, provider: claude}
  tester: {provider: codex}
tasks:
  - name: review
    identity: reviewer
    input: 审查 diff，并把结论写到 .agt/outputs/
    output: review-report
  - name: test
    identity: tester
    input: 基于 ${review.review-report} 运行测试
    depends_on: [review]
    output: test-result
```

```bash
agt sop validate pipeline.yaml
agt sop run pipeline.yaml
```

### 启动 Server 与 Daemon

需要任务队列、远程 Worker、共享运行历史或多机编排时，可以启用
Server / Daemon：

```bash
agt server init
agt server start --detach
agt daemon start --detach
agt task submit --prompt "用三个词打招呼"
```

Server 默认监听 `127.0.0.1:8420`，OpenAPI 文档位于
`http://127.0.0.1:8420/docs`。部署拓扑和配置详见
[快速启动指南](<docs/guide%20docs/quick_start.md>)。

## CLI

| 命令 | 用途 |
|---|---|
| `agt probe` | 发现已安装的 Agent CLI |
| `agt run` | 执行一次 Prompt |
| `agt chat` | 开始、列出、恢复或继续对话 |
| `agt goal` | 运行和观测 Goal Agent 树 |
| `agt sop` | 校验并运行静态 DAG 工作流 |
| `agt artifact` | 查看或下载声明的交付产物 |
| `agt session` | 删除已记录的 Chat 会话 |
| `agt server` | 管理任务服务器 |
| `agt daemon` | 管理执行 Daemon |
| `agt task` | 提交和查看单个任务 |
| `agt identity` | 管理角色和路由偏好 |
| `agt runtime` | 查看已注册的 Runtime |
| `agt token` | 签发用于 API 测试的任务级 Token |

完整参数、环境变量和退出码见
[CLI 命令参考](<docs/guide%20docs/commands.md>)。

## 架构

```text
┌───────────┐   HTTP / WS    ┌───────────┐  claim / report  ┌────────────┐
│   CLI     │ ◄────────────► │  Server   │ ◄──────────────► │  Daemon    │
└───────────┘                └───────────┘                  └────────────┘
                                  │                               │
                                  │ SQLite + Artifacts            │ 子进程
                                  │ Job 树 + MCP + SOP            ▼
                                  ▼                         ┌────────────┐
                            ┌───────────┐                   │ Agent CLI  │
                            │ Artifacts │                   └────────────┘
                            └───────────┘
```

- **CLI** 负责提交工作并展示可读的运行状态。
- **Server** 负责协调、持久化、选路、MCP 和 Artifact。
- **Daemon** 负责本地进程执行、并发控制和心跳。
- **Agent CLI** 以独立子进程运行，会话连续性由 Provider 负责。
- **SQLite** 是所有 Server 管理运行的信息源；`sessions.json` 仅用于本机直连 `agt chat`。

## 开发

```bash
uv sync
uv run pytest
uv run agt --help
```

## 参与贡献

欢迎提交 Issue 和 Pull Request。较大的改动请先创建 Issue。请尽量把
Provider 特定逻辑放在运行时适配器中，保持 Python-first 的小型架构，并在
提交前运行相关测试。

## 文档

- [CLI 命令参考](<docs/guide%20docs/commands.md>)
- [快速启动与部署指南](<docs/guide%20docs/quick_start.md>)
- [接入新 Provider](<docs/guide%20docs/provider-registration.md>)
- [编排架构](docs/impl/agent-orchestration-design.md)
- [Goal 模式设计](docs/impl/goal-mode-design.md)
- [本地运行时流程](docs/spec/local-agent-runtime-flow.md)

## 许可证

当前仓库尚未声明开源许可证。在公开发布项目，或按特定许可证接受外部贡献
之前，需要先添加 `LICENSE` 文件。
