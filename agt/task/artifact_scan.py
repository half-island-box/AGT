"""Daemon-side artifact detection (Phase 2).

Agents are third-party CLI subprocesses — there is no API to call. The
only observation surfaces are the filesystem and stdout. This module
implements the filesystem contract:

- The daemon pre-creates ``<agent_cwd>/.agt/outputs/`` and tells the
  agent (via AGENTS.md injection or a prompt suffix) to place declared
  deliverables there. **Appearing in this whitelist directory IS the
  declaration** — we never diff the whole work_dir (repo edits, deps,
  and caches are workspace noise, not deliverables; see design D2).
- Attribution: the executor snapshots ``{path → sha256}`` before spawn
  and diffs after exit; new/changed files belong to the current Step.
  sha256 (not mtime) is the change verdict — mtime is only a prefilter.
- ``manifest.json`` is a control file: the agent may declare
  ``[{name, mime_type, summary, status}]`` there for richer metadata.
- Security: realpath containment (no symlink escapes, no ``..``), a
  per-file size cap, a per-Step count cap, and an inline byte budget.
  All failures are best-effort — a scan problem must never fail the
  Step report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from ..a2a import (
    INLINE_MAX_BYTES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACTS_PER_STEP,
    REPORT_INLINE_BUDGET_BYTES,
)

log = logging.getLogger(__name__)

OUTPUTS_DIRNAME = ".agt/outputs"
MANIFEST_NAME = "manifest.json"

# The one-line contract handed to the agent. English on purpose: agent
# CLIs parse prompts in English more reliably, and it stays stable
# across locales.
OUTPUTS_INSTRUCTION = (
    "[agt] Place final deliverable files in the '.agt/outputs/' directory "
    "(relative to the current working directory). Optionally write "
    "'.agt/outputs/manifest.json' as "
    '[{"name": ..., "mime_type": ..., "summary": ..., "status": "pass|fail"}] '
    "to describe them. Do not put scratch or intermediate files there."
)


@dataclass
class ScannedArtifact:
    """One declared deliverable found in the outputs directory."""

    name: str  # path relative to .agt/outputs/, "/"-separated
    mime_type: str
    summary: str | None = None
    status_flag: str | None = None
    size: int = 0
    sha256: str = ""
    local_path: str = ""  # absolute path on this daemon (internal)
    inline_bytes: bytes | None = None  # set when <= INLINE_MAX_BYTES


def outputs_dir_for(agent_cwd: Path) -> Path:
    return agent_cwd / ".agt" / "outputs"


def ensure_outputs_dir(agent_cwd: Path) -> Path:
    """Pre-create the whitelist directory (idempotent)."""
    outputs = outputs_dir_for(agent_cwd)
    outputs.mkdir(parents=True, exist_ok=True)
    return outputs


def guess_mime(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_output_files(outputs_dir: Path):
    """Yield contained regular files, enforcing containment strictly.

    Symlinks are skipped outright (an escape attempt is not data), and
    any path whose realpath resolves outside the outputs directory is
    rejected — the whitelist is the whole security model.
    """
    try:
        root = outputs_dir.resolve()
    except OSError:
        return
    for path in sorted(outputs_dir.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                log.warning("artifact scan: path escapes outputs dir: %s", path)
                continue
        except OSError as exc:
            log.warning("artifact scan: stat failed for %s: %s", path, exc)
            continue
        yield path


def snapshot_outputs(outputs_dir: Path) -> dict[str, str]:
    """Capture ``{relative_path → sha256}`` before spawn.

    Best-effort: any unreadable file is skipped (it simply won't
    participate in the diff). ``manifest.json`` is excluded — it is a
    control file, not an artifact.
    """
    snapshot: dict[str, str] = {}
    if not outputs_dir.exists():
        return snapshot
    for path in _iter_output_files(outputs_dir):
        rel = path.relative_to(outputs_dir).as_posix()
        if rel == MANIFEST_NAME:
            continue
        try:
            snapshot[rel] = _sha256_of(path)
        except OSError as exc:
            log.warning("artifact snapshot: hash failed for %s: %s", path, exc)
    return snapshot


def _load_manifest(outputs_dir: Path) -> dict[str, dict]:
    """Parse manifest.json into ``{name → declared metadata}``."""
    manifest_path = outputs_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("artifact manifest unreadable: %s", exc)
        return {}
    entries = raw.get("artifacts") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return {}
    declared: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            declared[entry["name"]] = entry
    return declared


def scan_outputs(outputs_dir: Path, before: dict[str, str]) -> list[ScannedArtifact]:
    """Diff the outputs directory against the pre-spawn snapshot.

    New files and sha256-changed files are the current Step's artifacts.
    Enforces: realpath containment, 256MB per file, 50 per Step, and the
    64KB cumulative inline budget. Every failure is a skip + warning —
    never an exception (the Step report must survive the scan).
    """
    if not outputs_dir.exists():
        return []

    declared = _load_manifest(outputs_dir)
    artifacts: list[ScannedArtifact] = []
    inline_budget = 0

    for path in _iter_output_files(outputs_dir):
        if len(artifacts) >= MAX_ARTIFACTS_PER_STEP:
            log.warning(
                "artifact scan: more than %d files; ignoring the rest",
                MAX_ARTIFACTS_PER_STEP,
            )
            break

        rel = path.relative_to(outputs_dir).as_posix()
        if rel == MANIFEST_NAME:
            continue

        try:
            digest = _sha256_of(path)
        except OSError as exc:
            log.warning("artifact scan: hash failed for %s: %s", path, exc)
            continue
        if before.get(rel) == digest:
            continue  # unchanged — belongs to an earlier Step

        try:
            size = path.stat().st_size
        except OSError as exc:
            log.warning("artifact scan: stat failed for %s: %s", path, exc)
            continue
        if size > MAX_ARTIFACT_FILE_BYTES:
            log.warning(
                "artifact scan: %s exceeds %d bytes; skipping",
                rel,
                MAX_ARTIFACT_FILE_BYTES,
            )
            continue

        meta = declared.get(rel, {})
        inline: bytes | None = None
        if size <= INLINE_MAX_BYTES and inline_budget + size <= REPORT_INLINE_BUDGET_BYTES:
            try:
                inline = path.read_bytes()
                inline_budget += size
            except OSError as exc:
                log.warning("artifact scan: inline read failed for %s: %s", path, exc)

        artifacts.append(
            ScannedArtifact(
                name=rel,
                mime_type=str(meta.get("mime_type") or guess_mime(rel)),
                summary=meta.get("summary"),
                status_flag=meta.get("status"),
                size=size,
                sha256=digest,
                local_path=str(path),
                inline_bytes=inline,
            )
        )

    return artifacts


def scanned_to_report_item(a: ScannedArtifact) -> dict:
    """Serialize a ScannedArtifact into the report wire shape."""
    import base64

    return {
        "name": a.name,
        "mime_type": a.mime_type,
        "summary": a.summary,
        "status_flag": a.status_flag,
        "size": a.size,
        "sha256": a.sha256,
        "local_path": a.local_path,
        "inline_b64": (
            base64.b64encode(a.inline_bytes).decode() if a.inline_bytes else None
        ),
    }


__all__ = [
    "ScannedArtifact",
    "OUTPUTS_DIRNAME",
    "MANIFEST_NAME",
    "OUTPUTS_INSTRUCTION",
    "outputs_dir_for",
    "ensure_outputs_dir",
    "snapshot_outputs",
    "scan_outputs",
    "scanned_to_report_item",
    "guess_mime",
]
