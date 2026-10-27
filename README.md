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
agt daemon config show
agt daemon config set --max-concurrent 4 --agent-timeout 1800
agt daemon config set --work-dir /home/user/agt-work
agt daemon config unset --max-concurrent
```

Resolution order for each managed setting is environment variable > configuration file > built-in default. The file is written atomically with `0o600` permissions and validated at startup; a malformed file causes `agt daemon start` to exit with an actionable error. Changes take effect only after the daemon is restarted.

Remote daemons connect via HTTP or **reverse-dial WebSocket** (server dials out, NAT-friendly). See the [quick-start guide](<docs/guide%20docs/quick_start.md>).

## VS Code extension (early)

The `vscode/` directory holds a TypeScript UI client for the server. It includes server/daemon lifecycle controls, the daemon tree and status bar, Settings, Chat, the visual SOP editor, and Goal orchestration. Settings now includes Identity management: users can create, inspect, and edit identities, then manage optional daemon/provider routing preferences. Chat and Goal select an identity directly; SOP identity aliases choose from the same managed identity catalog. Bindings remain optional routing hints, so an identity can run on any live provider.

```bash
cd vscode
npm install
npm run build       # bundle extension + webview
npm run typecheck   # strict TypeScript check
npm run dev:web     # browser preview of webviews with mock data (no VS Code needed)
npm run test:loop   # SOP loop/YAML round-trip regressions
npm run check:sop   # apply an editing session and validate the result with the Python DSL
npm run check:run   # submit the sample SOP to a running server and read status + node log
```

Debug in VS Code: open `vscode/`, run `npm run watch`, press **F5** (Extension Development Host). The extension auto-starts `agt server` (configurable via `agt.startCommand`) and reads daemons from `/api/daemons`. Open **AGT: Open Chat** from the command palette or the AGT sidebar to use Chat mode.

Whenever the extension starts the server — on window startup or from the settings page — it also brings up the local daemon, so a fresh window is immediately usable without a separate `agt daemon start`. Both switches live in the settings page: `agt.autoStart` (start the server on window startup) and `agt.autoStartDaemon` (start the local daemon together with the server, default on). An already-running daemon is detected through `~/.agt/daemon.pid` and left untouched, the configured `serverUrl` is exported as `AGT_SERVER_URL` to the daemon process, and the commands themselves are configurable via `agt.daemonStartCommand` / `agt.daemonStopCommand` (also available as **AGT: Start Local Daemon** / **AGT: Stop Local Daemon**).

>>>>>>> Stashed changes
## SOP example

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
agt server init
agt server start --detach
agt daemon start --detach
agt task submit --prompt "Say hello in three words"
```

The server listens on `127.0.0.1:8420` by default. OpenAPI documentation is at
`http://127.0.0.1:8420/docs`. See the
[quick-start guide](<docs/guide%20docs/quick_start.md>) for deployment
topologies and configuration.

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

### Core invariants

- Every agent invocation is a **per-task subprocess**; agt does not keep an agent process alive.
- Conversation continuity is delegated to the provider and restored via persisted session IDs.
- Provider-specific behavior is isolated behind **runtime adapters** (registry pattern).
- The **server** owns coordination and persistence; **daemons** own local execution.
- Spawn lineage (`parent_job_id`) and DAG dependencies (`job_parents`) are orthogonal and coexist.

### Project structure

```text
agt/
├── a2a.py         A2A-shaped internal object model (Message/Part/Artifact)
├── agent_history  Provider transcript readers (Claude Code / Codex)
├── ids.py         Stable ID derivation (uuid5 from daemon URL)
├── cli/           Click command-line interface
│   ├── main.py    All core commands (probe/run/chat/server/daemon/task/...)
│   ├── sop.py     SOP pipeline commands (validate/submit/run/status/log)
│   └── goal.py    Goal orchestration commands (submit/run/status/log/input/
│                  cancel/artifacts/export)
├── goal/          Goal-mode prompt templates (orchestrator strategy)
├── comm/          HTTP + WebSocket client, WS-RPC link
├── daemon/        Runtime registration, task claiming, reverse-dial WS server,
│                  MCP proxy, running-task tracking, local resource server
├── probe/         Agent CLI discovery (which/shell fallback)
├── runtime/       Provider adapter registry:
│                  command_builders, session_locators, streaming sinks,
│                  resume_detectors, mcp_config profiles
├── server/        FastAPI application:
│   ├── routes/    tasks, jobs, artifacts, identities, sessions, daemons,
│   │              runtimes, heartbeat, sop, goal
│   ├── services/  job lifecycle, spawn orchestration, artifact registry,
│   │              claim, report, selection, SOP expansion, goal submit/export,
│   │              catalog
│   ├── mcp_server MCP catalog + authenticated Goal spawn (FastMCP)
│   └── stream_hub SSE pub/sub for remote streaming
├── sop/           SOP DSL parsing + static validation
└── task/          Task executor (3 modes), session store, artifact scanner,
                   spawn intent parser, message renderer
```

The TypeScript client lives in `vscode/` with a strict layer split: `src/core` (API client, server lifecycle, i18n — no `vscode` imports, reusable by future hosts like Obsidian) and `src/platform` + `src/webviews` (VS Code-specific UI).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGT_SERVER_URL` | `http://127.0.0.1:8420` | Server used by CLI and daemon. |
| `AGT_SERVER_SECRET` | `~/.agt/server.secret` fallback | Shared HMAC secret used by server and daemon. |
| `AGT_SERVER_PORT` | `8420` | Server port. |
| `AGT_DB_PATH` | `~/.agt/agt.db` | SQLite database location. |
| `AGT_MAX_CONCURRENT` | `1` | Max concurrent agent tasks per daemon; overrides `~/.agt/daemon.json`. |
| `AGT_AGENT_TIMEOUT` | `600` | Agent subprocess timeout (seconds); overrides `~/.agt/daemon.json`. |
| `AGT_WORK_DIR` | `~/.agt/workspaces` | Task workspace root; overrides `~/.agt/daemon.json`. |
| `AGT_AGENT_CWD_ALLOWLIST` | `~` (home directory) | Colon-separated allowed agent cwd roots; overrides `~/.agt/daemon.json`. |
| `AGT_LISTEN_MODE` | `0` | Set `1` for reverse-dial (NAT) mode. |
| `AGT_DAEMON_WS_PORT` | `8422` | Reverse-dial WS listen port. |
| `AGT_DAEMON_MCP_PORT` | `8428` | Loopback MCP proxy port (remote daemons). |
| `AGT_EXTRA_AGENTS` | — | Extra agents: `cmd:provider:Display[:stream_format]`. |

### Common files

| Data | Location |
|---|---|
| Server database | `~/.agt/agt.db` |
| Project chat sessions | `<project>/.agt/sessions.json` |
| Identity files | `~/.agt/identities/<slug>/AGENTS.md` |
| Server PID / log | `~/.agt/server.pid`, `~/.agt/server.log` |
| Daemon PID / log | `~/.agt/daemon.pid`, `~/.agt/daemon.log` |
| Daemon execution config | `~/.agt/daemon.json` (optional, `0o600`) |

## Documentation

- [CLI command reference](<docs/guide%20docs/commands.md>)
- [End-to-end quick start](<docs/guide%20docs/quick_start.md>)
- [New-provider registration guide](<docs/guide%20docs/provider-registration.md>)
- [Architecture & orchestration design](docs/impl/agent-orchestration-design.md)
- [SOP design](docs/impl/phase5-sop-design.md)
- [Goal mode design](docs/impl/goal-mode-design.md)
- [Dynamic spawn design](docs/impl/phase3-dynamic-spawn-design.md)

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
