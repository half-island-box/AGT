# agt

**中文** | [English](README.md)

`agt` 是一个本地 AI coding-agent CLI 桥接与编排器。它为 Claude Code、
Codex、Cursor Agent、GitHub Copilot、Qwen、Kimi、OpenCode、OpenClaw
以及自定义 CLI 提供统一接口，覆盖会话、队列执行、多 Agent 工作流、
产物管理和分布式路由。

> **状态：** 项目仍处于早期活跃开发阶段。CLI、Server、Daemon、Goal
> 编排和 SOP 工作流已可使用；桌面客户端仍在规划中，具体框架尚未确定。

## 核心能力

- **Provider 无关的 CLI** — 发现、执行和恢复本机已安装的 Agent CLI。
- **结构化运行事件** — 在 Provider 支持时输出文本、thinking、工具调用、
  工具结果以及 token / 成本用量。
- **Goal 编排** — 由根 Agent 拆解目标、派生子 Agent、共享上下文并综合交付。
- **SOP 工作流** — 使用 YAML 定义可复用的多 Agent DAG。
- **Artifact 产物管理** — 将输出登记为可寻址对象，包含血缘、摘要和
  SHA-256 哈希。
- **分布式 Daemon** — 通过 HTTP 或支持 NAT 场景的反向拨号 WebSocket
  在多台机器间路由任务。

## 环境要求

- Python 3.12+
- 至少一个已安装并完成认证的受支持 Agent CLI
- 源码安装需要 [uv](https://docs.astral.sh/uv/)，或直接使用 pip

## 快速开始

### 通过 pip 安装

```bash
python -m pip install agt-team

agt probe
agt run --provider claude --prompt "总结当前仓库"
```

如需队列执行和编排能力，启动 Server 与本地 Daemon：

```bash
agt server start --detach
agt daemon start --detach
agt task submit --prompt "Say hello" --provider claude
```

### 从源码安装

```bash
git clone https://github.com/half-island-box/agent-team.git
cd agent-team
uv sync

uv run agt probe
uv run agt run --provider claude --prompt "总结当前仓库"
```

## 可视化界面

`agt` 可以通过终端、浏览器或 VS Code 使用：

- **Web 控制台** — 启动 Server 后打开
  `http://127.0.0.1:8420/console/`，可以管理项目、Agent、Chat、SOP、
  Goal、Daemon、Identity 和用量。
- **VS Code 插件** — 将 AGT 插件连接到同一个本地 Server，即可在编辑器内
  使用 Chat、SOP 编辑、Goal 运行、Daemon 管理和设置。

Server API 默认优先面向本机回环地址。将服务暴露到其他机器或网络前，
请先阅读部署指南并确认安全边界。

## 主要命令

| 命令 | 用途 |
|---|---|
| `agt probe` | 发现已安装的 Agent CLI |
| `agt run` | 执行一次 Prompt |
| `agt chat` | 创建、列出、恢复或继续会话 |
| `agt goal` | 提交并查看动态 Agent 编排 |
| `agt sop` | 校验和运行 YAML DAG 工作流 |
| `agt artifact` | 查看和下载 Artifact |
| `agt server` | 管理协调 Server |
| `agt daemon` | 管理本地或远程执行 Daemon |
| `agt identity` | 管理身份与路由偏好 |

运行 `agt --help`，或查看
[CLI 参考](docs/guide/commands.md) 了解全部参数。

## 架构

```text
CLI / Web 控制台 / VS Code
        │
        ▼
     Server ── SQLite、Job、Artifact、路由、MCP、SOP
        │
        ▼
     Daemon ── 本地进程执行与并发管理
        │
        ▼
     Agent CLI ── Provider 原生会话与流式输出
```

Python 包将编排、Server、Daemon、运行时适配器和 CLI 划分为独立模块。
Agent CLI 始终以子进程方式运行；`agt` 不重新实现模型能力，也不内嵌模型。

## 文档

- [快速开始与部署](docs/guide/quick_start.md)
- [CLI 参考](docs/guide/commands.md)
- [Provider 注册](docs/guide/provider-registration.md)

## 开发

```bash
uv sync
uv run pytest
uv run agt --help
```

请保持改动小而清晰，延续 Python 优先的架构；Provider 特有行为应放进
运行时适配器，并在提交 Pull Request 前运行相关测试。

## 贡献

欢迎提交 Issue 和 Pull Request。如果是较大的架构或 Provider 变更，
请先创建 Issue 讨论设计。

## 许可证

本项目基于 [MIT License](LICENSE) 发布。
