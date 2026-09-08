# agt

**agt** is a local bridge for AI coding-agent CLIs. It discovers installed agents, runs them directly from the terminal, and provides an optional server/daemon layer for queued and concurrent execution.

It is an experimental Python foundation for a future multi-agent desktop client. The desktop UI has intentionally not been chosen yet.

## Features

- Discover local agent CLIs such as Claude Code, Codex, Cursor Agent, GitHub Copilot, Qwen, Kimi, OpenCode, and OpenClaw.
- Run one-shot prompts or continue an interactive chat session.
- Resume provider sessions by persisting provider-specific session IDs.
- Stream Claude Code output token-by-token while the agent is running (via `--include-partial-messages`).
- Queue tasks through a local FastAPI server and execute them on one or more daemons.
- Route tasks by provider, daemon, identity, and runtime binding.
- Protect agent-to-server callbacks with task-scoped HMAC tokens.
- Route identity-based sub-tasks dynamically across daemons (capability + affinity + load scoring; chat sessions stay pinned to one daemon) and propagate cancellation to running remote subprocesses (`tasks.cancel` push for reverse-dial daemons, claim-response delivery for HTTP daemons).
- Collect declared agent deliverables as addressable artifacts (`agt-artifact://<id>`): agents place files in `.agt/outputs/` (optionally described by `manifest.json`), the server registers lineage/mime/summary, and later steps can consume them by reference — bytes only move cross-daemon, on demand.
- Expose a live runtime catalog over MCP so a parent agent can discover which daemons are up (and what providers they run) before spawning a child.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) or pip
- At least one supported agent CLI installed and authenticated

## Install

From a clone:

```bash
git clone <repository-url>
cd agt
uv sync
```

Or install in editable mode with pip:

```bash
python -m pip install -e .
```

Both approaches provide the `agt` command. With uv, prefix commands with `uv run`.

## Quick start

Check which agents are available:

```bash
agt probe
```

Run a prompt directly:

```bash
agt run --provider claude --prompt "Explain this repository"
```

Start an interactive session:

```bash
agt chat --provider claude
```

Session metadata is stored in `<project>/.agt/sessions.json`. In Git repositories, agt idempotently adds `.agt/` to `.gitignore`. Remote `--daemon` chat sessions belong to the daemon and are not mirrored locally — list them across directories with `agt chat --list --daemon=<id>` and resume with `agt chat --resume <sid> --daemon=<id>` (provider / identity / directory are resolved through the server). Remote resume prints the past transcript too: the transcript stays on the daemon machine and the server fetches it on demand over the reverse-dial link.

## Server and daemon mode

Local commands work without a server. To use task queues and concurrent execution:

```bash
export AGT_SERVER_SECRET="$(openssl rand -hex 32)"

agt server start --detach
agt daemon start --detach

agt task submit --prompt "Say hello in three words"
agt task status <task-id>

agt daemon stop
agt server stop
```

By default, the server listens on `127.0.0.1:8420`. Its OpenAPI documentation is available at [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs) while the server is running.

Remote daemons can connect over HTTP or reverse-dial WebSocket, allowing execution on machines behind NATs. See the [quick-start guide](<docs/guide%20docs/quick_start.md>) for deployment details.

The server also serves a live **runtime catalog** over MCP (Streamable HTTP) at `http://127.0.0.1:8420/mcp` — it starts automatically with the server, no separate command. It exposes a single `list_runtimes` tool (optional `capability` / `provider` filters) that returns the daemons that are currently online and the providers each can run, so a parent agent can pick a valid `daemon_id`/`provider` before spawning a child. Local agents reach the server endpoint directly; a remote daemon (reverse-dial) serves the same tool through a loopback proxy on `127.0.0.1:8428/mcp` (`AGT_DAEMON_MCP_PORT`), forwarding each call over the reverse-dial link.

Remote `--daemon` / `--identity` chat streams agent output live: the daemon samples stdout prefixes, the server pushes them over SSE, and the CLI renders them incrementally (`snapshot → delta* → done`), with a poll fallback if the stream drops.

## CLI overview

| Command              | Description                                              |
| -------------------- | -------------------------------------------------------- |
| `agt probe`        | Discover installed agent CLIs.                           |
| `agt run`          | Run one prompt locally or through a daemon.              |
| `agt chat`         | Start, list, or resume an interactive agent session.     |
| `agt server`       | Start, stop, or inspect the task server.                 |
| `agt daemon`       | Start, stop, inspect, list, or show daemons.             |
| `agt task`         | Submit tasks and inspect or clean task state.            |
| `agt runtime list` | List runtimes registered with the server.                |
| `agt identity`     | Manage identities, identity files, and runtime bindings. |
| `agt artifact`     | List and download a session's declared deliverables.     |
| `agt token issue`  | Issue a task-scoped token for API testing.               |

Full option descriptions are in the [CLI reference](<docs/guide%20docs/commands.md>).

## Architecture

```text
┌───────────┐    HTTP / WebSocket     ┌───────────┐    claim / report    ┌────────────┐
│   CLI     │ <----------------------> │  Server   │ <------------------> │  Daemon    │
└───────────┘                         └───────────┘                      └────────────┘
     │                                      │                                 │
     │ fresh subprocess                     │ SQLite task/identity state      │ fresh subprocess
     ▼                                      ▼                                 ▼
┌────────────┐                                                               ┌────────────┐
│ Agent CLI  │                                                               │ Agent CLI  │
└────────────┘                                                               └────────────┘
```

Core invariants:

- Every agent invocation is a per-task subprocess; agt does not keep an agent process alive.
- Conversation continuity is delegated to the provider and restored with persisted session IDs.
- Provider-specific behavior is isolated behind runtime adapters.
- The server owns coordination and persistence; daemons own local execution.

### Project structure

```text
agt/
├── cli/       Click command-line interface
├── comm/      HTTP and WebSocket client communication
├── daemon/    Runtime registration, task claiming, and local execution
├── probe/     Agent discovery
├── runtime/   Provider adapters and streaming support
├── server/    FastAPI routes, services, persistence, and authentication
└── task/      Local task execution and session storage
docs/          Specifications, implementation notes, and CLI documentation
migrations/    Database schema migrations
tests/         Unit and integration tests
main.py        Minimal Python entry point
pyproject.toml Package metadata and dependencies
```

## Configuration

The most useful environment variables are:

| Variable               | Default                   | Purpose                                                    |
| ---------------------- | ------------------------- | ---------------------------------------------------------- |
| `AGT_SERVER_URL`     | `http://127.0.0.1:8420` | Server used by CLI and daemon commands.                    |
| `AGT_SERVER_SECRET`  | —                        | Shared HMAC secret; required by the server.                |
| `AGT_SERVER_HOST`    | `127.0.0.1` in the CLI  | Server bind address.                                       |
| `AGT_SERVER_PORT`    | `8420`                  | Server port.                                               |
| `AGT_DB_PATH`        | `~/.agt/agt.db`         | SQLite database location.                                  |
| `AGT_MAX_CONCURRENT` | `1`                     | Maximum concurrent agent tasks per daemon.                 |
| `AGT_AGENT_TIMEOUT`  | `600`                   | Agent subprocess timeout in seconds.                       |
| `AGT_WORK_DIR`       | `~/.agt/workspaces`     | Fallback task workspace root.                              |
| `AGT_EXTRA_AGENTS`   | —                        | Extra agent definitions in`cmd:provider:Display` format. |
| `AGT_DAEMON_MCP_PORT`| `8428`                  | Reverse-dial daemon proxy MCP port (loopback).           |

### Common files

| Data                  | Location                                     |
| --------------------- | -------------------------------------------- |
| Server database       | `~/.agt/agt.db`                            |
| Project chat sessions | `<project>/.agt/sessions.json`             |
| Identity files        | `~/.agt/identities/<slug>/AGENTS.md`       |
| Server PID/log        | `~/.agt/server.pid`, `~/.agt/server.log` |
| Daemon PID/log        | `~/.agt/daemon.pid`, `~/.agt/daemon.log` |

## Documentation

- [CLI command reference](<docs/guide%20docs/commands.md>)
- [End-to-end quick start](<docs/guide%20docs/quick_start.md>)
- [New-provider registration guide](<docs/guide%20docs/provider-registration.md>)

## Development

Run the CLI from the repository environment:

```bash
uv run agt --help
```

Run the test suite with the environment's pytest executable, for example:

```bash
.venv/bin/python -m pytest
```

Keep changes small and provider-specific logic behind the runtime adapter interfaces.

## Status

agt is under active development. Multi-agent task orchestration and the desktop client are planned; the current repository focuses on the CLI, runtime, server, and daemon primitives needed to support them.
