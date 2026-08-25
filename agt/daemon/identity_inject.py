"""Identity injection + reclaim helpers for daemon spawn (Phase 4 Step 4).

When the daemon handles a task with an ``identity_did``, we "stack" the
identity onto the agent's working directory: write the identity's
``AGENTS.md`` body into the spawn cwd as ``IDENTITY.md`` and append a
marker block to the spawn cwd's project-instructions file. That file
is provider-aware: Claude Code reads ``CLAUDE.md`` natively (it does
not pick up ``AGENTS.md`` by default), so for ``provider == "claude"``
the marker goes to ``CLAUDE.md``; every other agent follows the
``AGENTS.md`` convention. When the spawn completes (or crashes), we
undo it.

The ``AGENTS.md`` body arrives as the ``identity_md`` parameter
(Option D: server-rendered, bundled into the claim payload). For
daemons running outside the server path we still fall back to reading
``~/.agt/identities/<slug>/AGENTS.md`` from disk.

Design notes:

- **Idempotent**: ``_inject_identity`` always reclaims first, so a
  crash-leftover marker/IDENTITY.md is cleaned up before injection.
  Safe to call twice; safe to call against a cwd with stale state —
  including a leftover in the *other* provider's file (reclaim strips
  both).
- **Non-destructive**: the user's existing file is never overwritten —
  we only append the marker block to its end. If the file didn't
  exist, we create it and remember (via a hidden sidecar file) that it
  was ours so reclaim deletes it back.
- **Pure file IO**: no DB, no daemon-state. Reclaim is a pure
  function of cwd contents — must succeed (or no-op) even after
  daemon restart.
"""

from __future__ import annotations

import re
from pathlib import Path

# Sidecar stem that records "we created this file this time". Existence
# means reclaim is allowed to delete the file when it ends up empty
# (i.e. the user never had content of their own). One sidecar per
# target file: ``.agt-identity-managed-AGENTS.md`` /
# ``.agt-identity-managed-CLAUDE.md``.
_MANAGED_MARKER = ".agt-identity-managed"

# Marker block bounds. We match the WHOLE block (START line + content +
# END line) and remove it on reclaim.
_START_RE = re.compile(
    r"^<!--\s*AGT-IDENTITY-START:\s*(?P<did>[^\s]+)\s*-->\s*$"
)
_END_RE = re.compile(r"^<!--\s*AGT-IDENTITY-END\s*-->\s*$")

# Every file a marker block can land in. Reclaim strips blocks from all
# of them so a crash leftover from a run of the *other* provider is
# healed too (e.g. a claude run's CLAUDE.md leftover before a codex run).
_MARKER_FILES = ("AGENTS.md", "CLAUDE.md")


def _marker_filename(provider: str | None) -> str:
    """The project-instructions file a provider reads natively.

    Claude Code reads ``CLAUDE.md`` and does not pick up ``AGENTS.md``
    by default; every other agent follows the AGENTS.md convention.
    ``None`` (legacy callers) keeps the old AGENTS.md behavior.
    """
    return "CLAUDE.md" if provider == "claude" else "AGENTS.md"


def _sidecar_path(aagt_dir: Path, filename: str) -> Path:
    return aagt_dir / f"{_MANAGED_MARKER}-{filename}"


def _source_path(identity_did: str) -> Path:
    """Resolve the source AGENTS.md for an identity. Returns a path even
    if the file does not exist; callers must check existence."""
    slug = identity_did.split(":")[-1]
    return Path.home() / ".agt" / "identities" / slug / "AGENTS.md"


def _resolve_identity_md(
    identity_did: str, identity_md: str | None,
) -> str | None:
    """Pick the AGENTS.md body for an inject.

    Option D (server-supplied): when ``identity_md`` is set, return it
    verbatim — the server already rendered the current identity state
    and bundled it into the claim payload. The daemon never has to
    reach back to the Server (impossible in reverse-dial).

    Legacy fallback: when ``identity_md`` is None, fall back to reading
    ``~/.agt/identities/<slug>/AGENTS.md`` so a daemon running outside
    the server path still works. Returns ``None`` if the file is
    absent — the caller will silently skip injection in that case.
    """
    if identity_md is not None:
        return identity_md
    src = _source_path(identity_did)
    if not src.exists():
        return None
    return src.read_text(encoding="utf-8")


def _marker_block(identity_did: str) -> str:
    """The text appended (or written) to ``AGENTS.md`` at inject time.

    Inline summary (DID slug + pointer to IDENTITY.md). We deliberately
    do NOT pull name/capabilities from the Server here — the executor
    doesn't carry them and a per-spawn fetch would couple agent
    spawn to a round trip. IDENTITY.md (a copy of the identity's
    AGENTS.md) carries the full description for the model.
    """
    slug = identity_did.split(":")[-1]
    return (
        f"\n<!-- AGT-IDENTITY-START: {identity_did} -->\n"
        f"你当前以身份 `{slug}` (DID: {identity_did}) 运行。\n"
        f"完整身份描述与工作风格见同目录 IDENTITY.md。\n"
        f"<!-- AGT-IDENTITY-END -->\n"
    )


def _strip_marker_block(text: str, did: str | None = None) -> str:
    """Remove every AGT-IDENTITY block. If ``did`` is given, only
    remove blocks whose START line names that did."""
    out_lines: list[str] = []
    skip_until_end = False
    keep_current = True
    for line in text.splitlines():
        m_start = _START_RE.match(line)
        if m_start and not skip_until_end:
            block_did = m_start.group("did")
            keep_current = (did is None or block_did == did)
            skip_until_end = True
            if keep_current:
                continue
            # else fall through (preserve line) — unusual: malformed file
        if skip_until_end:
            if _END_RE.match(line):
                skip_until_end = False
                if keep_current:
                    continue
                # else append the END line below
            elif not keep_current:
                out_lines.append(line)
            # if keep_current: skip this content line
        else:
            out_lines.append(line)
    # Re-join: keep final newline if original had one
    result = "\n".join(out_lines)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _inject_identity(
    cwd: Path,
    identity_did: str,
    provider: str | None = None,
    *,
    identity_md: str | None = None,
) -> None:
    """Inject the identity into ``cwd``. Idempotent (reclaim-first).

    ``identity_md`` (Option D) takes priority: when provided, the
    server already rendered the identity's AGENTS.md body and bundled
    it into the claim payload — we use it verbatim. When ``None``, we
    fall back to reading ``~/.agt/identities/<slug>/AGENTS.md`` from
    disk (legacy local path). If both are absent, the call silently
    skips.

    Steps (full rationale in the module docstring):

    1. Reclaim any prior injection (idempotent / crash-self-heal — in
       both AGENTS.md and CLAUDE.md).
    2. Resolve the AGENTS.md body (server-rendered or disk-fallback).
       If absent, skip silently.
    3. Ensure ``cwd/.agt`` exists; write the body → ``cwd/IDENTITY.md``.
    4. Append (or create) the provider's instructions file
       (``CLAUDE.md`` for claude, else ``AGENTS.md``) with the marker
       block. Track whether we created the file (per-file sidecar) so
       reclaim can delete it if it ends up empty.
    """
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    # 1. Idempotent reclaim — handles crash leftovers from a prior run.
    _reclaim_identity(cwd)

    # 2. Resolve body.
    source_md = _resolve_identity_md(identity_did, identity_md)
    if source_md is None:
        return

    # 3. Write identity file into cwd as IDENTITY.md.
    aagt_dir = cwd / ".agt"
    aagt_dir.mkdir(exist_ok=True)
    identity_file = cwd / "IDENTITY.md"
    identity_file.write_text(source_md, encoding="utf-8")

    # 4. Append (or create) the marker file with the marker block.
    target = cwd / _marker_filename(provider)
    block = _marker_block(identity_did)
    if not target.exists():
        target.write_text(block, encoding="utf-8")
        # Mark "we created this file" so reclaim may delete it.
        _sidecar_path(aagt_dir, target.name).touch()
    else:
        with target.open("a", encoding="utf-8") as f:
            f.write(block)


def _reclaim_identity(cwd: Path, *, did: str | None = None) -> None:
    """Undo a prior ``_inject_identity``. Idempotent.

    Steps:

    1. Strip any AGT-IDENTITY marker blocks (the one we just added,
       or leftovers from a crashed prior run) from **every** potential
       marker file — a crash leftover may sit in the other provider's
       file.
    2. Delete ``cwd/IDENTITY.md`` if present.
    3. For each file we created ourselves (per-file sidecar present)
       that is now empty, delete it — restoring the pre-injection
       "file didn't exist" state. A legacy bare sidecar (pre per-file
       sidecars) still authorizes deleting AGENTS.md.
    4. Clean up the sidecars.
    """
    cwd = Path(cwd)
    aagt_dir = cwd / ".agt"
    identity_file = cwd / "IDENTITY.md"

    # 1. Strip marker blocks from every potential target file.
    for name in _MARKER_FILES:
        target = cwd / name
        if not target.exists():
            continue
        sidecars = [_sidecar_path(aagt_dir, name)]
        if name == "AGENTS.md":
            sidecars.append(aagt_dir / _MANAGED_MARKER)  # legacy bare sidecar
        managed = any(s.exists() for s in sidecars)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            text = ""
        new_text = _strip_marker_block(text, did=did)
        if new_text != text:
            if new_text.strip() == "" and managed:
                # We created this file and the strip left nothing — drop it.
                target.unlink()
            else:
                target.write_text(new_text, encoding="utf-8")
        # Sidecar cleanup for this file (whether or not we deleted it).
        for s in sidecars:
            if s.exists():
                try:
                    s.unlink()
                except OSError:
                    pass

    # 2. Delete IDENTITY.md.
    if identity_file.exists():
        try:
            identity_file.unlink()
        except OSError:
            pass


__all__ = ["_inject_identity", "_reclaim_identity"]