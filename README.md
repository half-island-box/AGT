# agt

[中文文档](README_zh.md) | **English**

**A local bridge and orchestrator for AI coding-agent CLIs.**

`agt` discovers the coding agents installed on your machine and gives them one
command-line, server, and daemon interface. It adds persistent sessions,
multi-machine execution, goal-driven sub-agent orchestration, static DAG
workflows, and a shared artifact registry.

> [!NOTE]
> `agt` is under active development. The CLI, server, daemon, Goal mode, and
> SOP workflows are usable today. The desktop client is planned, but its
> framework has not been selected.

## Highlights

- **Provider-neutral execution** — run and chat through one CLI, with
  provider-native sessions and output streaming.
- **Structured agent events** — chat forwards thinking, tool calls, and tool
  results (server API, SSE, and CLI), and records per-turn token/cache/cost
  usage. Claude Code streams them over `stream-json`; Codex streams them over
  the `codex app-server` JSON-RPC protocol (with automatic fallback to
  `codex exec` on older CLIs).
- **Goal orchestration** — a root agent decomposes an objective, spawns child
  agents within a global budget, and synthesizes their deliverables.
- **Shared context** — Goal agents exchange versioned decisions, constraints,
  facts, and Artifact references through a bounded, auditable ContextBrief.
- **Static workflows** — define reusable multi-agent DAGs in YAML with
  `depends_on` and `${node.output}` dataflow.
- **Artifact handoff** — register declared outputs as stable
  `agt-artifact://<id>` references with lineage, summaries, and SHA-256.
- **Distributed workers** — queue work on a local server and route it across
  machines via HTTP or reverse-dial WebSocket.
- **Identity routing** — assign roles such as reviewer or tester and route them
  by provider, daemon affinity, and runtime availability.
- **Console projects** — persist user-selected directory projects and browse
  allowed directories through loopback-only server APIs. A project always has
  a local (Server-machine) directory and may optionally pin one remote
  daemon directory, browsable over the reverse-dial link and used as the
  default working directory for chat, SOP, and Goal runs on that daemon.
- **MCP and editor integration** — expose sub-agent spawning to MCP-capable
  agents and use the early VS Code extension for chat, Goals, and SOP editing.

Supported agents include Claude Code, Codex, Cursor Agent, GitHub Copilot,
Qwen, Kimi, OpenCode, OpenClaw, and custom CLIs.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) or pip
- At least one supported agent CLI installed and authenticated

## Installation

```bash
git clone https://github.com/half-island-box/agent-team.git
cd agent-team
uv sync
uv run agt --help
```

Or install with pip:

```bash
python -m pip install -e .
agt --help
```

## Quick start

### Run or chat with an agent

```bash
agt probe
agt run --provider claude --prompt "Explain this repository"
agt chat --provider claude

# Attach files to the first chat turn (repeatable, max 10)
agt chat --file report.pdf --file notes.md

# Show thinking, tool calls, and per-turn token/cost usage
agt chat --show-thinking --show-tools --usage

# Resume a persisted session
agt chat --list
agt chat --resume <session-id>
```

### Orchestrate a goal

```bash
agt goal run "Review this repository and fix all P0 issues"
agt goal status <root-job-id>
agt goal runs
```

Goal mode creates a dynamic Job tree. Agents can delegate recursively, pause
for user input, retry failed work during synthesis, and export a successful run
as a reusable SOP:

```bash
agt goal input <job-id> "Use Chinese and split the work by module"
agt goal export <root-job-id> -o pipeline.yaml
```

### Run a reusable workflow

```yaml
# pipeline.yaml
name: review-and-fix
identities:
  reviewer: {did: did:agt:reviewer, provider: claude}
  tester: {provider: codex}
tasks:
  - name: review
    identity: reviewer
    input: Review the diff and write findings to .agt/outputs/
    output: review-report
  - name: test
    identity: tester
    input: Run tests using ${review.review-report} as context
    depends_on: [review]
    output: test-result
```

```bash
agt sop validate pipeline.yaml
agt sop run pipeline.yaml
```

### Start server and daemon mode

Use server mode for queued execution, remote workers, shared history, and
multi-machine orchestration.

```bash
agt server start --detach          # first run generates ~/.agt/server.secret
agt daemon start --detach
agt task submit --prompt "Say hello in three words"
```

The server listens on `127.0.0.1:8420` by default. OpenAPI documentation is at
`http://127.0.0.1:8420/docs`. The server also provides loopback-only console
project APIs for creating, renaming, opening, and listing directory-backed
projects; local directory browsing is constrained by
`AGT_CONSOLE_WORKSPACE_ROOTS`. A project may additionally pin a directory on one
remote daemon (`remote_daemon_id` + `remote_path`): listing it goes over the
reverse-dial link (`GET /api/console/directories?daemon_id=…`) and is bounded by
that daemon's `AGT_AGENT_CWD_ALLOWLIST`, so every selectable directory is also
runnable. Runs always record the project's local path as their run-history
directory (`jobs.cwd`) while keeping the remote directory as the execution
directory (`jobs.agent_cwd`), so chat, SOP and Goal history — including remote
runs — and run counts stay grouped under the project; `GET /api/runs` accepts a
repeated `cwd` to match older rows that recorded a remote path. See the
[quick-start guide](<docs/guide%20docs/quick_start.md>) for deployment
topologies and configuration.

### Connect a remote daemon

A daemon behind NAT listens on a fixed port and the server dials out to it.
Pairing is one command on each side — the invite code carries the shared
secret, the daemon's public URL and its id, so nothing is copied by hand:

```bash
# server machine
agt daemon invite --name cloud-01 --url ws://1.2.3.4:8422 --add
#   → run the printed `agt daemon init agt1.…` on the daemon machine

# daemon machine
agt daemon init agt1.…
agt daemon start --detach

# server machine — retire a remote daemon and drop its target entry
agt daemon remove cloud-01
```

`agt daemon remove` deletes the target's line from `~/.agt/daemons` (comments and
other entries untouched); the server stops dialing it after a refresh or a
restart. The settings panel's **Connection & pairing** block can also register
and remove targets through the loopback-only `/api/pairing/*` endpoints, which
additionally stop the live `DaemonLink` immediately.

`POST /api/daemons/refresh` re-reads the target file into a running server: new
targets start being dialed at once, dropped ones stop, and a daemon that was
offline is re-dialed immediately instead of waiting out its reconnect backoff.
Healthy links are left alone, so in-flight claims are never interrupted. Both
frontends expose it as a refresh button (the plugin's sidebar title bar and each
row of the settings daemon list).

An invite code embeds the server secret; treat it like an SSH private key.
See [reverse-dial pairing](<docs/guide%20docs/quick_start.md#3-反向连接nat-穿透远程-daemon-推荐>).

## CLI

| Command | Purpose |
|---|---|
| `agt probe` | Discover installed agent CLIs |
| `agt run` | Execute one prompt |
| `agt chat` | Start, list, resume, or continue a conversation |
| `agt goal` | Run and inspect goal-driven agent trees |
| `agt context` | Read shared context visible to the current Agent task |
| `agt sop` | Validate and run static DAG workflows |
| `agt artifact` | List or download declared deliverables |
| `agt session` | Delete recorded chat sessions |
| `agt server` | Manage the task server |
| `agt daemon` | Manage execution daemons |
| `agt task` | Submit and inspect individual tasks |
| `agt identity` | Manage roles and routing preferences |
| `agt runtime` | Inspect registered runtimes |
| `agt token` | Issue task-scoped tokens for API testing |

See the [CLI reference](<docs/guide%20docs/commands.md>) for all options,
environment variables, and exit codes.

## Architecture

```text
┌───────────┐   HTTP / WS    ┌───────────┐  claim / report  ┌────────────┐
│   CLI     │ ◄────────────► │  Server   │ ◄──────────────► │  Daemon    │
└───────────┘                └───────────┘                  └────────────┘
                                  │                               │
                                  │ SQLite + artifacts            │ subprocess
                                  │ Job tree + MCP + SOP          ▼
                                  ▼                         ┌────────────┐
                            ┌───────────┐                   │ Agent CLI  │
                            │ Artifacts │                   └────────────┘
                            └───────────┘
```

- **CLI** submits work and provides human-readable views of runs.
- **Server** owns coordination, persistence, routing, MCP, and artifacts.
- **Console projects** are persistent directory pointers; run history remains
  scoped by the project path in `jobs.cwd`. A project's local path is
  mandatory; its optional `(remote_daemon_id, remote_path)` pair supplies the
  default working directory for chat/SOP/Goal runs routed to that daemon.
- **Daemon** owns local process execution, concurrency, and heartbeats.
- **Agent CLIs** run as fresh subprocesses; sessions remain provider-native.
- **SQLite** is the source of truth for all Server-managed runs.
  `sessions.json` indexes are used only by local direct `agt chat`.

## Development

```bash
uv sync
uv run pytest
uv run agt --help
```

## Contributing

Issues and pull requests are welcome. For substantial changes, open an issue
first. Keep provider-specific behavior behind runtime adapters, preserve the
small Python-first architecture, and run the relevant tests before submitting.

## Documentation

- [CLI reference](<docs/guide%20docs/commands.md>)
- [Quick-start and deployment guide](<docs/guide%20docs/quick_start.md>)
- [Adding a provider](<docs/guide%20docs/provider-registration.md>)
- [Orchestration architecture](docs/impl/agent-orchestration-design.md)
- [Goal mode design](docs/impl/goal-mode-design.md)
- [Shared context design](docs/impl/goal-shared-context-design.md)
- [Local runtime flow](docs/spec/local-agent-runtime-flow.md)

## License

The repository does not currently declare an open-source license. Add a
`LICENSE` file before distributing the project publicly or accepting external
contributions under a specific license.
