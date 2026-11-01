# Adding a New Provider

> 实战指南：在 AGT 里接入一个新的 agent CLI（`myagent` 之类）。
>
> 本文介绍如何基于 Provider 适配层的 `CommandBuilder`、`ResumeRejectionDetector`、`SessionLocator` 和 `StreamingSink` 接入一个新 provider。
>
> **核心原则**：新加 provider = 改注册类文件。`executor.py` / `cli/main.py` / `daemon/*` 全部零改动。

---

## 0. 一句话总览

```python
# ① KnownAgent 列表加一行（agt/probe/known_agents.py）
# ② 实现 0-4 个 adapter（按需）
# ③ _REGISTRY 加一行（agt/runtime/registry.py）
# ④ 跑测试验证
```

只有这 4 步。下面逐项展开。

---

## 1. Provider 适配层四件套速查

每个 adapter 的职责如下：

| Adapter | 必须？ | 没实现时 |
|---|---|---|
| `CommandBuilder` | ✅ 必须 | 没默认——每个 provider 命令行不同 |
| `ResumeRejectionDetector` | ✅ 必须 | 用 `GenericResumeRejectionDetector()` 保守占位（resume失败时不 drop session） |
| `SessionLocator` | ❌ 可选 | 传 `None` = 不做磁盘 fallback（session 重启后会丢） |
| `StreamingSink` | ❌ 可选 | 用 `NoopSink` = 走老 pipe 文本路径 |

**复用策略**：

- 命令行类似 claude（`-p <prompt>`）→ 复用 `ClaudeCommandBuilder()`
- 命令行类似 codex（`exec <prompt>`）→ 复用 `CodexCommandBuilder()`
- 命令行类似 opencode（`run [flags] <prompt>`）→ 参考 `OpenCodeCommandBuilder()`（支持 `AGT_OPENCODE_MODEL` 选模型）
- 命令行类似 pi（`[flags] -- <prompt>`，`--session-id` 创建缺失会话）→ 参考 `PiCommandBuilder()`（支持 `AGT_PI_MODEL` / `AGT_PI_PROVIDER` / `AGT_PI_THINKING`）
- session 写在 `~/.claude/projects/...` → 复用 `claude_session_locator()`
- session 写在 `~/.codex/sessions/...` → 复用 `codex_session_locator()`

---

## 2. 步骤详解

### 步骤 1 — `KnownAgent` 加一行

[agt/probe/known_agents.py](../../agt/probe/known_agents.py) 的 `KNOWN_AGENTS`：

```python
KNOWN_AGENTS: list[KnownAgent] = [
    KnownAgent("claude", "claude", "Claude Code", stream_format="claude_stream_json"),
    KnownAgent("codex", "codex", "Codex CLI", cli_style="codex"),
    KnownAgent("opencode", "opencode", "OpenCode", stream_format="opencode_json"),
    KnownAgent("pi", "pi", "Pi", stream_format="pi_json"),
    KnownAgent("myagent", "myagent", "My Agent CLI"),  # ← 新增
]
```

字段说明：

- `command`：在 `$PATH` 找到的二进制名（probe 时执行 `which <command>`）
- `provider`：内部标识符，registry / 数据库 / CLI `--provider` 都用它
- `display_name`：用户看到的名字
- `cli_style`：历史遗留字段，新 provider 不用管
- `stream_format`：声明是否支持流式（`"none"` / `"claude_stream_json"` 等）

如果想通过 `AGT_EXTRA_AGENTS` 环境变量让用户自定义时也能配该 provider，改 `_parse_extra`（详见 [probe/known_agents.py](../../agt/probe/known_agents.py)）。

### 步骤 2 — 实现需要的 adapter（按需）

#### 2a. `CommandBuilder`（通常必须新写）

在 [agt/runtime/command_builders.py](../../agt/runtime/command_builders.py) 加一个类。两种风格做参考：

```python
class MyAgentCommandBuilder(CommandBuilder):
    """Build ``<bin> --new-flag [--resume <sid>] <prompt>``."""

    def build(self, binary, task, stream_args):
        cmd = [str(binary), "--new-flag", *stream_args]
        if task.resume_session_id:
            cmd += ["--resume", task.resume_session_id]
        cmd += [task.prompt]
        return cmd
```

如果命令行类似 claude，直接 `ClaudeCommandBuilder()` 复用：

```python
from agt.runtime.command_builders import ClaudeCommandBuilder
# 不写新类，直接复用
```

#### 2b. `ResumeRejectionDetector`（通常复用）

默认用 `GenericResumeRejectionDetector()`（保守返回 False）。只有当你的 provider 有清晰的 resume 拒绝信号、且需要自动 drop session 时才新写：

```python
# agt/runtime/resume_detectors.py
class MyAgentResumeRejectionDetector(ResumeRejectionDetector):
    def is_resume_rejection(self, *, is_error, raw_error, raw_event):
        if not is_error or not isinstance(raw_event, dict):
            return False
        # 填实际信号
        return "session not found" in (raw_event.get("message") or "").lower()
```

#### 2c. `SessionLocator`（可选）

只在你的 provider 把 session 写在磁盘某个标准路径时才需要：

```python
# agt/runtime/session_locators.py
def myagent_session_locator() -> SessionLocator:
    return SessionLocator(
        roots=(Path.home() / ".myagent" / "sessions",),
        glob="*.json",
        id_extractor=lambda p: p.stem,
    )
```

不实现 = `None` = 不会做磁盘 fallback。

#### 2d. `StreamingSink`（可选）

不实现 = `NoopSink` = 走老 pipe 文本路径（一次性返回）。只有当你的 provider 支持真流式时才写：

```python
# agt/runtime/streaming.py
class MyAgentStreamSink(StreamingSink):
    def consume(self, raw):
        mtype = raw.get("type")
        if mtype == "delta":
            self.on_text_delta(raw.get("text", ""))
        elif mtype == "end":
            self._session_id = raw.get("session_id")
            self._is_error = raw.get("error")

    def finalize(self) -> SinkResult:
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,
            text="".join(self.accumulated_text),
        )
```

### 步骤 3 — `_REGISTRY` 注册一行

[agt/runtime/registry.py](../../agt/runtime/registry.py) 的 `_REGISTRY` dict：

```python
_REGISTRY: dict[str, ProviderAdapter] = {
    "claude": _claude_adapter(),
    "codex": _codex_adapter(),
    "opencode": _opencode_adapter(),
    "pi": _pi_adapter(),
    "myagent": ProviderAdapter(                  # ← 新增一行
        command_builder=MyAgentCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
        mcp_profile=None,                        # 见步骤 3.5（MCP 注入，可选）
    ),
}
```

字段含义：

- `command_builder`：实例（不是类）
- `stream_sink_factory`：零参 callable 返回 `StreamingSink` 实例（用 `NoopSink` 不要 `lambda: NoopSink()`，前者本身就是 callable）
- `resume_rejection_detector`：实例
- `mcp_profile`：`McpConfigProfile | None`，决定任务 spawn 时怎么把 MCP 配置注册进该 provider。传 `None`（默认）= 不注入。见 [步骤 3.5](#步骤-35--接-mcp-catalog可选)。

`get_adapter()` 自动 fallback 到 claude 风格 default，所以**未注册的 provider 仍能跑**——但它们没拿到流式、磁盘 fallback 等高级特性。要让特性生效，**必须显式注册**。

### 步骤 3.5 — 接 MCP catalog（可选）

如果希望 agent 在会话里能通过 MCP 查询**在线运行时目录**（`list_runtimes`）并调用 Goal `spawn_subagents`，给 provider 的 adapter 配一个 `mcp_profile`。它描述"这个 provider 从哪个文件读 MCP 配置"，executor 在 spawn 前注入、任务结束后回收，全程不看 provider 名字字面量。

现有三种 profile，都在 [agt/runtime/mcp_config.py](../../agt/runtime/mcp_config.py)：

| 类 | 目标文件 | 适用 |
|---|---|---|
| `ProjectJsonMcpConfig(".mcp.json")` | 项目内 `<cwd>/.mcp.json` | Claude Code（`{"mcpServers": {...}}`） |
| `GlobalTomlMcpConfig()` | 全局 `~/.codex/config.toml` | Codex CLI（`[mcp_servers.agt-catalog]`） |
| `opencode_mcp_profile()` | 项目内 `<cwd>/opencode.json` | OpenCode（`{"mcp": {"servers": {...}}}`，`type: "remote"`） |
| `pi_mcp_profile()` | 项目内 `<cwd>/.pi/mcp.json` | Pi（`{"mcpServers": {...}}`，HTTP `url`；headless 需 `--approve`） |

三选一直接复用即可，不存在就传 `None`：

```python
from agt.runtime.mcp_config import GlobalTomlMcpConfig, ProjectJsonMcpConfig

# 场景 A：provider 用项目内 JSON（像 claude）
"myagent": ProviderAdapter(
    ..., mcp_profile=ProjectJsonMcpConfig(".mcp.json"),
),
# 场景 B：provider 用全局 TOML（像 codex）
"myagent": ProviderAdapter(
    ..., mcp_profile=GlobalTomlMcpConfig(),
),
```

要点：

- **不写新类也能接**：如果你的 provider 既不是 `.mcp.json` 也不是全局 TOML，才需要新写一个实现 `McpConfigProfile`（`inject` / `reclaim` 两个方法）的类，并遵守"**非破坏**"约定——只 merge/remove 自己的 `agt-catalog` 条目，别的都原样保留，reclaim 后恢复原状。
- 全局文件（TOML）profile 只在目标文件上加/删自己的 `[mcp_servers.agt-catalog]` 表，**绝不整文件备份/还原**，并用 fcntl 锁防止同机并发互相踩。
- 认证配置只引用环境变量名 `AGT_TASK_TOKEN`：Claude 写 `Authorization: Bearer ${AGT_TASK_TOKEN}`，Codex 写 `bearer_token_env_var = "AGT_TASK_TOKEN"`。真实 token 由 executor 按 Step 注入，不能写入共享配置文件。
- **Codex 的额外一把开关**：Streamable-HTTP 还得用户自己在 `~/.codex/config.toml` 里开一次 `experimental_use_rmcp_client = true`。这是用户级策略，profile **故意不写**——文档里提示用户即可。其他 provider 若也有类似的"总开关"，同理不要替你写。
- `mcp_profile` 缺省是 `None`，dispatcher 对 `None` 直接 no-op，所以没接的 provider 行为不受影响。

### 步骤 4 — 验证

```bash
# 静态检查：executor.py 不应有 provider名字字面量
grep -nE '"claude"|"codex"|"myagent"' agt/task/executor.py
# 只有注释和类型注解，不应该有字符串字面量（除注释外）

# 跑测试
unset PYTHONPATH; .venv/bin/python -m pytest tests/ \
  -p no:launch_testing \
  --ignore=tests/test_server_pidfile.py \
  --ignore=tests/test_remote_daemon_workdir.py \
  --ignore=tests/test_daemon_agent_cwd_allowlist.py -q
```

`tests/test_provider_registry.py` 已覆盖"每个内置 provider 都有 adapter" + "未注册 provider 回退"——你新加的 provider 自动被测到。

### 步骤 5 — 写新测试（可选，但建议）

最小单测，在 `tests/test_provider_registry.py` 里加（如果默认测试已经覆盖，可以省略）。

如果新写了 adapter（CommandBuilder / Sink / Locator / Detector），最好在对应的测试文件里加针对性测试：

| Adapter | 测试文件 |
|---|---|
| CommandBuilder | [tests/test_command_builders.py](../../tests/test_command_builders.py) |
| SessionLocator | [tests/test_session_locators.py](../../tests/test_session_locators.py) |
| ResumeRejectionDetector | [tests/test_resume_detectors.py](../../tests/test_resume_detectors.py) |
| StreamingSink | [tests/test_streaming_sinks.py](../../tests/test_streaming_sinks.py) |
| History reader | [tests/test_load_pi_history.py](../../tests/test_load_pi_history.py) |
| Registry | [tests/test_provider_registry.py](../../tests/test_provider_registry.py) |

---

## 3. 完整示例：加 `myagent`（一个 claude-like CLI）

**假设** `myagent` 的命令行格式是 `<bin> --prompt "<prompt>"`，支持 `--session <id>` resume，不支持流式。

```python
# ── agt/probe/known_agents.py ──
KNOWN_AGENTS = [
    ...
    KnownAgent("myagent", "myagent", "My Agent CLI"),
]

# ── agt/runtime/command_builders.py ──
class MyAgentCommandBuilder(CommandBuilder):
    """Build ``<bin> --prompt "<prompt>" [--session <id>]``."""
    def build(self, binary, task, stream_args):
        cmd = [str(binary), "--prompt", task.prompt, *stream_args]
        if task.resume_session_id:
            cmd += ["--session", task.resume_session_id]
        return cmd

# ── agt/runtime/registry.py ──
_REGISTRY["myagent"] = ProviderAdapter(
    command_builder=MyAgentCommandBuilder(),
    session_locator=None,                          # 暂不做磁盘 fallback
    stream_sink_factory=NoopSink,                  # 不流式
    resume_rejection_detector=GenericResumeRejectionDetector(),
    mcp_profile=None,                              # 暂不接 MCP catalog
)
```

**3 个文件，~15 行改动**。`executor.py` / `cli/main.py` / `daemon/*` 全部零改动。

跑测试：

```bash
unset PYTHONPATH; .venv/bin/python -m pytest tests/test_provider_registry.py \
  tests/test_command_builders.py -p no:launch_testing -v
```

实际跑：

```bash
unset PYTHONPATH; .venv/bin/agt run \
  --provider myagent -p "say hi"
```

---

## 4. 真实例子：claude 加新流式协议

假设 Claude Code 未来加了 `--output-format stream-json-v2`，事件 shape 不同。

```python
# ── agt/runtime/streaming.py ──
class ClaudeV2StreamSink(StreamingSink):
    """v2 协议的事件解析。"""

    def __init__(self):
        super().__init__()
        self._session_id: str | None = None
        self._is_error: bool = False

    def consume(self, raw):
        if raw.get("type") == "delta":
            self.on_text_delta(raw.get("text", ""))
        elif raw.get("type") == "done":
            self._session_id = raw.get("session")
            self._is_error = bool(raw.get("error"))

    def finalize(self) -> SinkResult:
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,
            text="".join(self.accumulated_text),
        )

# ── agt/runtime/registry.py: 改 1 行 ──
"claude": ProviderAdapter(
    command_builder=ClaudeCommandBuilder(),
    session_locator=claude_session_locator(),
    stream_sink_factory=ClaudeV2StreamSink,    # ← 改这里
    resume_rejection_detector=ClaudeResumeRejectionDetector(),
),
```

`executor.py` 零改动——它在 `_stream_args_for` 里通过 `isinstance(sink, ClaudeStreamSink)` 判断要不要加 stream 参数。如果 v2 需要不同参数，再在那里加个 `isinstance(sink, ClaudeV2StreamSink)` 分支——但**只动一处**（`_stream_args_for`）。

---

## 5. 验证扩展性闭环

客观验收标准：

```bash
# 这条 grep 必须为空（除注释和类型注解外）
grep -nE '"claude"|"codex"|"myagent"' agt/task/executor.py
```

如果返回的是只有 `# Claude` 注释之类，就是通过。如果有 `provider == "claude"` 这种 if/elif，就破了扩展性闭环。

---

## 6. 删除 / 弃用一个 provider

1. 从 `_REGISTRY` 删一行
2. 从 `KNOWN_AGENTS` 删一行
3. （可选）从 `probe/scanner.py` 取消兼容路径
4. 跑测试确认 `tests/test_provider_registry.py` 的"完整性"测试还能通过（未注册的 provider 会自动 fallback）

未注册的 provider 在 `agt run --provider X` 时会收到 `_claude_default_adapter()`（Claude 命令行 + NoopSink + Generic），仍可工作，但会失去该 provider 的特定行为。

---

## 7. 检查清单（新 provider 加完一遍走）

- [ ] `agt/probe/known_agents.py` 加了 `KnownAgent` 行
- [ ] （如果非标准命令行）`agt/runtime/command_builders.py` 加了新 builder
- [ ] （如果支持真流式）`agt/runtime/streaming.py` 加了新 sink
- [ ] （如果磁盘 session 路径特殊）`agt/runtime/session_locators.py` 加了 locator
- [ ] （如果 resume 拒绝信号明确）`agt/runtime/resume_detectors.py` 加了 detector
- [ ] `agt/runtime/registry.py` 的 `_REGISTRY` 加了对应行
- [ ] （可选）接 MCP catalog 时配了 `mcp_profile`（复用现成 profile 或新写一个非破坏 profile）
- [ ] `grep` 校验：executor.py 无 provider 名字字面量
- [ ] `pytest tests/test_provider_registry.py` 全过
- [ ] 跑 `agt run --provider <new> -p "..."` 端到端验证
- [ ] 跑 `agt chat --provider <new>` 多轮验证 session 接续

---

## 8. 与现有文档的关系

| 文档 | 用途 |
|---|---|
| [commands.md](commands.md) | CLI 行为与环境变量参考 |
| [quick_start.md](quick_start.md) | 本地与远程部署指南 |
| **本文档** | 实战指南：新加 provider 的步骤 |

遇到问题先查本文档的"检查清单"；想知道"为什么这样设计"回 design.md；想跑形式化验证看 spec。
