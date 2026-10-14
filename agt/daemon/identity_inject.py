from __future__ import annotations

import re
from pathlib import Path

_MANAGED_MARKER = ".agt-identity-managed"

# 注入内容的标记块。注入的身份信息被包裹在这个标记块中，方便后续回收和清理。
_START_RE = re.compile(
    r"^<!--\s*AGT-IDENTITY-START:\s*(?P<did>[^\s]+)\s*-->\s*$"
)
_END_RE = re.compile(r"^<!--\s*AGT-IDENTITY-END\s*-->\s*$")

# 注入到Agents/Claude.md中
_MARKER_FILES = ("AGENTS.md", "CLAUDE.md")


_OUTPUTS_START = "<!-- AGT-OUTPUTS-START -->"
_OUTPUTS_END = "<!-- AGT-OUTPUTS-END -->"

# Claude Code特殊处理
def _marker_filename(provider: str | None) -> str:
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

    if identity_md is not None:
        return identity_md
    src = _source_path(identity_did)
    if not src.exists():
        return None
    return src.read_text(encoding="utf-8")


def _marker_block(identity_did: str) -> str:
 
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

# 身份注入核心代码
def _inject_identity(
    cwd: Path,
    identity_did: str,
    provider: str | None = None,
    *,
    identity_md: str | None = None,
) -> None:

    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)


    _reclaim_identity(cwd, _keep_sidecars=True)

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


def _reclaim_identity(
    cwd: Path,
    *,
    did: str | None = None,
    _keep_sidecars: bool = False,
) -> None:
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
        # Skipped on a crash-heal (``_keep_sidecars=True``): a fresh
        # inject is about to run and still needs the ownership marker.
        if not _keep_sidecars:
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


def _strip_outputs_block(text: str) -> str:
    """Remove every AGT-OUTPUTS block (deliverables contract).

    Line-based, mirroring :func:`_strip_marker_block` but matching only
    the outputs tag — a strip of one block must never touch the other.
    """
    out_lines: list[str] = []
    skipping = False
    for line in text.splitlines():
        if not skipping and line.strip() == _OUTPUTS_START:
            skipping = True
            continue
        if skipping:
            if line.strip() == _OUTPUTS_END:
                skipping = False
            continue
        out_lines.append(line)
    result = "\n".join(out_lines)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _inject_outputs_instruction(
    cwd: Path,
    provider: str | None,
    instruction: str,
) -> None:
    """Inject the deliverables contract into the provider's
    project-instructions file (``CLAUDE.md`` for claude, else
    ``AGENTS.md``).

    Mirrors :func:`_inject_identity`'s inject/reclaim cycle but with its
    own tag, so a crash-leftover block is healed without touching the
    identity marker (and vice versa). Deliberately does NOT own the
    "delete empty file" decision — identity reclaim (which the executor
    runs after this) owns that; see :func:`_reclaim_outputs_instruction`.
    """
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    # Crash-heal: strip any stale block before re-appending.
    _reclaim_outputs_instruction(cwd)

    target = cwd / _marker_filename(provider)
    block = f"\n{_OUTPUTS_START}\n{instruction}\n{_OUTPUTS_END}\n"
    if not target.exists():
        target.write_text(block, encoding="utf-8")
        # Mark "we created this file" so identity reclaim may delete it
        # when both blocks are later stripped and nothing remains.
        aagt_dir = cwd / ".agt"
        aagt_dir.mkdir(exist_ok=True)
        _sidecar_path(aagt_dir, target.name).touch()
    else:
        with target.open("a", encoding="utf-8") as f:
            f.write(block)


def _reclaim_outputs_instruction(cwd: Path) -> None:
    cwd = Path(cwd)
    for name in _MARKER_FILES:
        target = cwd / name
        if not target.exists():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = _strip_outputs_block(text)
        if new_text != text:
            target.write_text(new_text, encoding="utf-8")


__all__ = [
    "_inject_identity",
    "_reclaim_identity",
    "_inject_outputs_instruction",
    "_reclaim_outputs_instruction",
]