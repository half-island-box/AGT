# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project shape

`agt` is a Python-first local bridge and orchestrator for coding-agent CLIs.
Keep the backend modular and provider-specific behavior behind runtime
adapters. The browser console and VS Code extension are clients of the same
server API; do not assume a desktop shell that the repository has not adopted.

## Development workflow

```bash
uv sync
uv run pytest
uv run agt --help

cd vscode
npm ci
npm run typecheck
npm test
```

Run the narrowest relevant tests for a change and explain any new dependency or
architecture decision in the pull request.

## Documentation

User-facing CLI and deployment documentation lives in `docs/guide/`. Keep
`README.md`, `README_zh.md`, and the guide current when behavior or commands
change. Avoid describing internal implementation phases in public documentation;
document the current interface and behavior instead.

## Code review expectations

- Keep changes small, testable, and reviewable.
- Preserve provider-neutral orchestration and execution boundaries.
- Do not commit runtime state, credentials, local session data, build caches,
  or screenshots containing real machine names, usernames, paths, or IDs.
- Prefer standard-library or narrowly scoped dependencies.
- Keep generated frontend assets in sync with their TypeScript sources.
