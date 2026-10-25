# agt

[中文文档](README_zh.md) | **English**

**agt** is a local bridge for AI coding-agent CLIs. It discovers installed agents, runs them from the terminal, and provides a server/daemon layer for queued, concurrent, multi-machine execution — including goal-driven orchestration, MCP-driven sub-agent spawning, static DAG pipelines, artifact management, and an MCP runtime catalog.

It is an experimental Python foundation for a future multi-agent desktop client. The desktop UI has intentionally not been chosen yet.

## Features

### Agent execution

- **Discover** local agent CLIs: Claude Code, Codex, Cursor Agent, GitHub Copilot, Qwen, Kimi, OpenCode, OpenClaw.
- **Run** one-shot prompts or **chat** interactively with session continuity (provider-native `--resume`).
- **Stream** Claude Code output token-by-token locally (via `--include-partial-messages`); remote `--daemon` chat streams over SSE (`snapshot → delta* → done`).
- **Resume** provider sessions across invocations by persisting session IDs.

### Orchestration

- **Goal-driven orchestration**: hand one requirement to a root orchestrator agent (`agt goal`), which decomposes it, recursively spawns child agents within a per-run budget (`--max-depth` / `--max-descendants` / `max_batch`), and synthesizes the final answer. Goal history is grouped by the submitter cwd independently from the optional daemon-side execution directory. A full Job-tree view (`agt goal status`), `AGT-ASK` questions answered via `agt goal input`, cascade cancel, and artifact download round out the entry.
- **Dynamic spawn**: MCP-capable agents call `spawn_subagents` with structured child envelopes. The task-scoped tool authenticates the current Goal Step, creates child Jobs immediately, enforces budgets, deduplicates retries, waits for completion, then spawns a synthesis step. Providers without MCP keep the `AGT-SPAWN <json>` marker fallback.
- **Static DAG pipelines (SOP)**: author reusable multi-agent workflows in YAML with `depends_on`, `${node.output}` dataflow references, and dependency-gated activation (`blocked → queued`). Terminal-node artifacts are the pipeline's final output. A successful goal run can be distilled back into a SOP draft via `agt goal export`.
- **Identity-based routing**: identities (DID) bind to `(daemon, provider)` runtimes; sub-tasks route dynamically across daemons by capability + affinity + load, while chat sessions stay pinned.
- **Cancellation**: cancels cascade to spawn descendants and DAG dependents; running remote subprocesses are terminated via `tasks.cancel` push (reverse-dial) or claim-response delivery (HTTP).

### Artifacts & data flow

- **Artifact registry**: agents declare deliverables in `.agt/outputs/` (optionally via `manifest.json`); the server registers lineage/mime/summary/sha256 as `agt-artifact://<id>` references.
- **On-demand bytes**: same-daemon consumption is zero-copy (local path hint); cross-daemon fetches lazily pull bytes over the reverse-dial link, verify sha256, and materialize on the server.
- **Artifact CLI**: `agt artifact list --session <sid> [--daemon <id>]` and `agt artifact get <uri|id>` / `--session --out-dir` for batch download.

### Infrastructure

- **Server**: FastAPI + SQLite (`jobs`, `tasks`, `runtimes`, `sessions`, `identities`, `identity_bindings`, `artifacts`, `job_parents`, `spawn_batches`), Alembic migrations, HMAC task tokens.
- **Daemon**: HTTP long-poll or reverse-dial WebSocket (NAT traversal), semaphore-based concurrency (`AGT_MAX_CONCURRENT`), heartbeat + runtime registration.
- **MCP tools**: `/mcp/` (Streamable HTTP) exposes `list_runtimes` plus the authenticated Goal `spawn_subagents` tool. Claude Code gets the endpoint via project `.mcp.json`; Codex via global `~/.codex/config.toml`. Both configs reference `AGT_TASK_TOKEN` instead of storing credentials.
- **Provider adapters**: command builders, session locators, stream sinks, resume-rejection detectors, and MCP config profiles are registered per provider — adding a new agent means adding a registry row, not touching the executor.
- **VS Code extension (in development)**: TypeScript client in `vscode/` — activity-bar daemon tree, server + local daemon lifecycle management, status bar, settings webview, Chat mode, a visual SOP editor, Goal orchestration, and zh/en UI. It talks to the same HTTP API as the CLI; no Python logic is duplicated.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) or pip
- At least one supported agent CLI installed and authenticated

## Install

```bash
git clone <repository-url>
cd agt
uv sync
```

Or with pip:

```bash
python -m pip install -e .
```

Both provide the `agt` command. With uv, prefix commands with `uv run`.

## Quick start

```bash
# Discover agents
agt probe

# One-shot run (local, streaming)
agt run --provider claude --prompt "Explain this repository"

# Interactive chat (local)
agt chat --provider claude

# Remote chat (pinned to a daemon, SSE streaming)
agt chat --provider codex --daemon <daemon-id>

# List / resume sessions
agt chat --list
agt chat --list --daemon <daemon-id> --dir <daemon-cwd>  # remote sessions filtered by daemon-side cwd
agt chat --resume <session-id> --daemon <daemon-id>
```

Session metadata lives in `<project>/.agt/sessions.json`. In Git repositories, agt idempotently adds `.agt/` to `.gitignore`.

## Server and daemon mode

```bash
agt server init          # one-time: generates ~/.agt/server.secret
agt server start --detach
agt daemon start --detach

agt task submit --prompt "Say hello in three words"
agt task status <task-id>

agt daemon stop
agt server stop
```

`agt server init` persists `AGT_SERVER_SECRET` to `~/.agt/server.secret` with `0o600` permissions; `agt server start` auto-discovers it. If you prefer environment variables, set `AGT_SERVER_SECRET` manually and skip the init step.

Default: server on `127.0.0.1:8420`, OpenAPI at [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs).

### Daemon persistent configuration

Daemon execution settings can be persisted in `~/.agt/daemon.json` and managed with the CLI:

```bash
agt daemon config show
agt daemon config set --max-concurrent 4 --agent-timeout 1800
agt daemon config set --work-dir /home/user/agt-work
agt daemon config unset --max-concurrent
```

Resolution order for each managed setting is environment variable > configuration file > built-in default. The file is written atomically with `0o600` permissions and validated at startup; a malformed file causes `agt daemon start` to exit with an actionable error. Changes take effect only after the daemon is restarted.

Remote daemons connect via HTTP or **reverse-dial WebSocket** (server dials out, NAT-friendly). See the [quick-start guide](<docs/guide%20docs/quick_start.md>).

## VS Code extension (early)

The `vscode/` directory holds a TypeScript UI client for the server. The current milestone includes server lifecycle, daemon tree, status bar, settings, and Chat mode. Chat supports daemon/agent/identity selection with provider logos, daemon session history, live SSE output, multi-turn continuation, resumable turn stopping, confirmed session deletion, and zh/en static text; SOP and Goal modes are planned in later milestones.

```bash
cd vscode
npm install
npm run build       # bundle extension + webview
npm run dev:web     # browser preview of webviews with mock data (no VS Code needed)
npm run test:core   # core SOP editor tests (round-trip, graph, layout)
npm run test:inspector   # inspector regressions (selects, identity clearing, roots)
npm run test:run         # run-tracking helpers (local daemon detection, fingerprint)
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
agt sop runs                      # list SOP runs for the current cwd
```

## Goal example

```bash
# Hand one open-ended requirement to a root orchestrator agent.
agt goal run "Write deployment docs for this repo"

# Inspect the grown Job tree / answer an agent question mid-run.
agt goal status <root_job_id>
agt goal input <job_id> "Use Chinese and split by module"

# List goal runs for the current cwd.
agt goal runs

# Distill a successful run into a reusable SOP DAG.
agt goal export <root_job_id> -o pipeline.yaml
agt sop validate pipeline.yaml && agt sop run pipeline.yaml
```

## CLI overview

| Command | Description |
|---|---|
| `agt probe` | Discover installed agent CLIs. |
| `agt run` | Run one prompt locally or through a daemon. |
| `agt chat` | Start, list, or resume an interactive agent session. |
| `agt session delete` | Permanently delete one or more server-recorded chat sessions. |
| `agt sop` | Validate / submit / run / runs / status / log static DAG pipelines. |
| `agt goal` | Submit / run / runs / status / log / input / cancel / artifacts / export goal-driven runs. |
| `agt artifact` | List and download declared deliverables. |
| `agt server` | Start, stop, or inspect the task server. |
| `agt daemon` | Start, stop, inspect, list, show, or configure daemons. |
| `agt task` | Submit tasks and inspect or clean task state. |
| `agt runtime list` | List runtimes registered with the server. |
| `agt identity` | Manage identities, identity files, and runtime bindings. |
| `agt token issue` | Issue a task-scoped token for API testing. |

Full option descriptions: [CLI reference](<docs/guide%20docs/commands.md>).

## Architecture

```text
┌───────────┐   HTTP / WS    ┌───────────┐  claim / report  ┌────────────┐
│   CLI     │ ◄────────────► │  Server   │ ◄──────────────► │  Daemon    │
└───────────┘                └───────────┘                  └────────────┘
                                  │                               │
                                  │ SQLite (jobs/tasks/artifacts) │ fresh subprocess
                                  │ MCP catalog + spawn (/mcp)    ▼
                                  │ SOP DAG (job_parents)    ┌────────────┐
                                  ▼                          │ Agent CLI  │
                            ┌───────────┐                    └────────────┘
                            │ Artifacts │
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
| `AGT_SERVER_SECRET` | — | Shared HMAC secret (required by server). |
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
uv run agt --help
.venv/bin/python -m pytest
```

> On systems with conflicting pytest plugins (e.g. ROS), use:
> `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest`

Keep changes small and provider-specific logic behind the runtime adapter interfaces.

## Status

Under active development. Multi-agent task orchestration (dynamic spawn + static SOP) and the artifact pipeline are functional. The desktop client is planned; the current focus is CLI, runtime, server, and daemon primitives.
