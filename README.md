# agt

Local AI agent CLI bridge — discover, register, and invoke local AI coding agents (Claude Code, Codex, Cursor Agent, etc.) from the terminal.

`agt` is a Python re-implementation of the local-daemon layer described in [docs/spec/local-agent-runtime-flow.md](docs/spec/local-agent-runtime-flow.md). Phase 2 adds a local Server + background Daemon for task queuing and concurrent multi-agent execution.

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

## Phase 2: Server + background Daemon

Phase 1 commands (`probe` / `run` / `chat`) still work **without** any server. Phase 2 adds an opt-in server + daemon pair for task queuing, concurrent execution, and richer reporting:

```bash
# Terminal 1: start the Server (FastAPI on 127.0.0.1:8420, SQLite-backed)
$ agt server start
[uvicorn] agt-server ready on 127.0.0.1:8420

# Terminal 2: start the background Daemon (polls Server for tasks)
$ AGT_SERVER_URL=http://127.0.0.1:8420 agt daemon start
2026-08-09 ... Registered runtimes with Server

# Terminal 3: submit a task to the Server
$ agt task submit -p "say hi in 3 words"
task_id=79dd86c7
  query status with: agt task status 79dd86c7

$ agt task status 79dd86c7
task_id   : 79dd86c7
provider  : claude
status    : completed
session_id: 695852fa-b891-4847-9fbe-f79d53fd675f
exit_code : 0
duration  : 5.08s

# Multiple concurrent tasks are dequeued in parallel (up to AGT_MAX_CONCURRENT)
$ for i in 1 2 3; do agt task submit -p "task $i" &; done

# Stop the daemon when done
$ agt daemon stop
```

The Server exposes its full API surface at <http://127.0.0.1:8420/docs> (FastAPI auto-generated OpenAPI).

### New env vars (Phase 2)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGT_SERVER_PORT` | `8420` | Server TCP port |
| `AGT_SERVER_HOST` | `127.0.0.1` | Server bind address (localhost-only) |
| `AGT_DB_PATH` | `~/.agt/agt.db` | SQLite database file |
| `AGT_DAEMON_PORT` | `8421` | Local resource server (only for agent subprocesses) |
| `AGT_SERVER_SECRET` | — | **Phase 3, required.** Shared HMAC secret between Server and Daemon for task-scoped tokens. Generate with `openssl rand -hex 32`. |
| `AGT_DAEMON_LOCAL_TOKEN` | random per launch | Phase 3. Token for `X-Daemon-Local-Token` on the local resource server. Auto-rotated each daemon restart. |
| `AGT_USE_WS_CLAIM` | `0` | **Phase 3.3, opt-in.** Remote daemon only. Set to `1` to claim via WS-first (`ws://<server>/ws/daemon`) with HTTP fallback; HTTP-only otherwise. |

`AGT_SERVER_URL` is read by both the CLI's `task` commands and the Daemon.

## Phase 3: Task-scoped tokens (callback auth)

Phase 3 introduces HMAC-signed tokens so that agent subprocesses can call back
to the Server (e.g. reporting progress) without a long-lived WebSocket. The
Daemon automatically signs a fresh token per spawned task and injects it via
the `AGT_TASK_TOKEN` environment variable.

```bash
# Quick start — Phase 3 requires a shared HMAC secret between Server and Daemon:
$ export AGT_SERVER_SECRET=$(openssl rand -hex 32)

# Server reads it on startup (will refuse to start without it):
$ agt server start

# Daemon reads it on startup (silently skips token injection if unset):
$ agt daemon start
```

For manually testing the Server's `/api/agent/*` endpoints (curl, notebooks),
issue a token from the CLI:

```bash
$ TOKEN=$(AGT_SERVER_SECRET=$AGT_SERVER_SECRET agt token issue --task-id mytask)
$ curl -X POST http://127.0.0.1:8420/api/agent/callback \
    -H "X-Task-Token: $TOKEN" -H "X-Task-Id: mytask" \
    -H "Content-Type: application/json" -d '{...}'
```

See [`docs/cli/quick_start.md`](docs/cli/quick_start.md) for an end-to-end guide (local + remote daemon setup),
and [`docs/cli/commands.md`](docs/cli/commands.md) for full command reference.

### Phase 3.2: Remote daemons over WebSocket

Remote daemons (running on machines other than the Server's host) connect to
the Server over WebSocket instead of HTTP:

```
ws://<server-host>:8420/ws/daemon    Header: X-Daemon-Token: <session token>
```

The server pushes a `heartbeat_ack{rpc_v1:true}` frame immediately on accept
as capability negotiation; subsequent frames are JSON-RPC requests
demultiplexed by `request_id` over a single connection. Local daemons
continue to use plain HTTP — the WS path is reserved for remote machines
where the extra safety window (server timeout + grace) is needed to prevent
double-claim on transient disconnects.

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

## Phase 2 limitations

- **Localhost only.** Server binds to `127.0.0.1`; no auth. Multi-machine / remote daemon support is Phase 3.
- **No WebSocket.** Phase 2 uses HTTP. WS-RPC for the rare Uncertain-frame scenarios is Phase 3.
- **No multi-agent orchestration.** Tasks are 1:1 with one agent run. `Task.parent_task_id` is persisted (Phase 3 placeholder) but no orchestrator reads it yet.

## Phase 4 workdir: cwd-based task workdir

`agt run` / `agt chat` derive a task's *AGT metadata directory* from the
user's current working directory. The agent itself runs in that same cwd —
intuitive and "what you see is what you operate on".

```
cd /home/user/project-x
agt run --prompt "explain this module"
#     agent cwd  = /home/user/project-x           (the user's cwd)
#     AGT meta   = /home/user/project-x/.agt/tasks/<id>/   (sessions.json, etc.)
```

If `/home/user/project-x/.git` exists, `agt run` idempotently appends
`.agt/` to `.gitignore` so the metadata never leaks into a commit.

### Flags

| Flag | Purpose |
|------|---------|
| `--work-dir <path>` | Override the AGT metadata directory. Agent cwd is **not** changed. Useful for sandboxed setups. |
| `--no-work-dir` | Use the legacy daemon-local layout (`~/.agt/workspaces/<provider>/<id>/`) for both agent cwd and AGT metadata. |

### Cleaning up

```
agt task clean <task_id>          # remove one task's AGT metadata
agt task clean --all-done         # batch-remove completed/failed/timeout tasks
agt task clean <task_id> --force  # also remove a still-running task's metadata
```

Remote-daemon tasks (no `work_dir` on the Server) are warned about and
skipped — the receiving daemon's filesystem isn't ours to touch.

### Identity AGENT.md path change

Identity `AGENT.md` files moved from `~/.agt/workspaces/<provider>/identity/<did>/`
to `~/.agt/identities/<did>/`. Identity is now daemon-local and **decoupled
from provider** — the same identity shares one AGENT.md across providers.

If you have existing files, move them once:

```
mv -r ~/.agt/workspaces/*/identity/* ~/.agt/identities/
```

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