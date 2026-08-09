# agt

Local AI agent CLI bridge — discover, register, and invoke local AI coding agents (Claude Code, Codex, Cursor Agent, etc.) from the terminal.

`agt` is a Python re-implementation of the local-daemon layer described in [docs/spec/local-agent-runtime-flow.md](docs/spec/local-agent-runtime-flow.md). Phase 1 implements local invocation only; Phase 2 will add persistent server connection for task dispatch.

## Install

```bash
# from the project root, in editable mode
pip install -e .
```

This installs the `agt` console script (entry point declared in `pyproject.toml`).

## Commands

| Command | Purpose |
|---------|---------|
| `agt probe` | Discover AI agent CLIs installed on this machine. |
| `agt run -p "..."` | Spawn the agent once with a prompt and stream its output. |
| `agt chat` | Interactive chat — each turn spawns a fresh agent subprocess; session id is reused across turns via the `SessionStore` so Claude Code's `--resume` carries conversation context forward. |
| `agt daemon start` | Phase 2 placeholder. In Phase 1, `run` / `chat` work without a background daemon. |

### Examples

```bash
# Discover what's installed
$ agt probe
🔍 Scanning for AI agent CLIs on this machine...

  Provider   Command        Path                                     Version      Status
  ──────────────────────────────────────────────────────────────────────────────────────────
  claude     claude         /home/user/.local/bin/claude             2.1.76       online

✅ Found 1 agent(s) on this machine.

# One-shot invocation
$ agt run -p "explain this function"
🚀 Executing with claude...
...
✅ Task completed in 5.3s (exit_code=0, session_id=—)

# Chat with session continuity
$ agt chat
🚀 agt chat mode (provider=claude, task=chat-a1b2c3d4). Type 'exit' or 'quit' to leave.

agt> 帮我加个 --debug 参数
... (Claude responds, session id is persisted)

agt> 再加个 --version 参数
... (Claude sees the previous turn via --resume sess-xxx)

agt> exit
👋 Bye!
```

## Configuration

All configuration is via `AGT_*` environment variables (read by `DaemonConfig.from_env()`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGT_SERVER_URL` | — | Multica server URL (Phase 2) |
| `AGT_DAEMON_ID` | auto uuid[:8] | Stable daemon identifier |
| `AGT_DEVICE_NAME` | hostname | Display name for this machine |
| `AGT_RUNTIME_NAME` | — | Optional override for Runtime names |
| `AGT_MAX_CONCURRENT` | 1 | Phase 2: parallel task limit |
| `AGT_AGENT_TIMEOUT` | 600 | Seconds before a hung agent is killed |
| `AGT_WORK_DIR` | `~/.agt/workspaces` | Root for per-task working directories |
| `AGT_CLAUDE_PATH` | — | Override path to `claude` binary |
| `AGT_EXTRA_AGENTS` | — | Extra agent definitions: `cmd:provider:Display,cmd:provider:Display` |

## Phase 1 limitations

- **No server connection.** The persistent daemon↔server link lives in Phase 2.
- **No `--resume` session-id extraction from agent output.** Phase 1 relies on the agent to echo a session id in stdout; if the agent doesn't, the session is recorded as `None` and the next turn starts fresh. A robust fallback that scans `~/.claude/projects/<hash>/*.jsonl` for the newest file is a known follow-up.
- **No workspace / repo / skill-bundle preparation** before spawning the agent.
- **One task at a time.** `AGT_MAX_CONCURRENT` is wired through but Phase 1 has no scheduler.

## Architecture

See [docs/spec/local-agent-runtime-flow.md](docs/spec/local-agent-runtime-flow.md) for the high-level model, and [docs/impl/python-replication-design.md](docs/impl/python-replication-design.md) for the Python implementation details. The key invariant in Phase 1:

> **The agent CLI is always a per-task subprocess.** The daemon spawns a new Claude Code process per run and lets it exit. There is no long-lived connection between the daemon and the agent.
> Session continuity is achieved by *persisting the session_id* and passing it back as `--resume <id>` on the next run. Full conversation context lives inside the agent itself (e.g. `~/.claude/projects/<hash>/*.jsonl`).

```
┌──────────┐  (Phase 2: persistent) ┌──────────┐  per-task subprocess ┌─────────────┐
│  Server  │<──────────────────────>│  Daemon  │<─────────────────────>│ Agent CLI   │
└──────────┘   heartbeat/claim      └──────────┘   spawn → exec → exit └─────────────┘
                                       │
                                       └─ SessionStore (~/.agt/sessions.json)
                                          task_id → session_id (pointer only)
```