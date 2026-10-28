"""Server-owned user input uploads (chat attachments).

An upload is *job input*: the user sends bytes to the Server before any
Step exists, gets back an ``agt-upload://<upload_id>`` reference, and the
reference travels inside a normal A2A ``Message`` FilePart. The daemon
resolves the bytes on demand (``uploads.get`` RPC / HTTP fallback) and
materializes them under ``<agent_cwd>/.agt/inputs/`` — there is no
server-side ``local_path`` hint because the bytes belong to the Server,
not to any daemon.

Storage mirrors the artifact foundation: content under
:data:`agt.a2a.INLINE_MAX_BYTES` lives inline in the DB, everything else
on the Server disk under ``<db dir>/uploads/``. Identical content
(``sha256``) reuses one row, so sending the same file twice costs no
extra storage and no extra transfer.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import INLINE_MAX_BYTES, make_upload_uri
from ..models import InputUploadModel

log = logging.getLogger(__name__)

# Filenames are display-only, but they end up as real files on the daemon
# under ``.agt/inputs/`` — strip anything that could traverse or break a
# path while keeping Unicode readable (中文文件名 must survive).
_UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
MAX_NAME_CHARS = 200
DEFAULT_NAME = "upload.bin"

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(value: datetime) -> str:
    return value.strftime(_TS_FORMAT)


def sanitize_upload_name(name: str | None) -> str:
    """Reduce a client-supplied name to a safe basename.

    Only the last path segment is kept; control characters and the
    separators that matter to Windows/Unix are replaced. The extension is
    preserved (it decides the on-disk name on the daemon, never the mime
    type).
    """
    raw = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_NAME_RE.sub("_", raw).strip().strip(".")
    if not cleaned:
        return DEFAULT_NAME
    return cleaned[:MAX_NAME_CHARS]


def uploads_dir_for(db_path: str | Path) -> Path:
    """Server upload blob directory, derived from the DB location."""
    return Path(db_path).expanduser().parent / "uploads"


def upload_to_out(upload: InputUploadModel) -> dict:
    """Public (JSON-safe) representation of one upload row."""
    return {
        "upload_id": upload.upload_id,
        "name": upload.name,
        "mime_type": upload.mime_type,
        "size": upload.size,
        "sha256": upload.sha256,
        "uri": make_upload_uri(upload.upload_id),
        "source": upload.source,
        "created_at": upload.created_at,
        "expires_at": upload.expires_at,
    }


def _too_large(max_bytes: int) -> ValueError:
    return ValueError({
        "type": "upload_too_large",
        "message": f"upload exceeds the {max_bytes} byte limit",
        "max_bytes": max_bytes,
    })


async def _by_sha(db: AsyncSession, sha256: str) -> InputUploadModel | None:
    return (
        await db.execute(
            select(InputUploadModel).where(InputUploadModel.sha256 == sha256)
        )
    ).scalars().first()


async def create_upload(
    db: AsyncSession,
    *,
    name: str,
    content: bytes,
    mime_type: str | None = None,
    source: str = "",
    db_path: str | Path,
    max_bytes: int = 32 * 1024 * 1024,
    ttl_days: int = 7,
    inline_max: int = INLINE_MAX_BYTES,
) -> InputUploadModel:
    """Store one upload, deduplicating on ``sha256``.

    Raises ``ValueError({"type": "upload_too_large", ...})`` when the
    content exceeds ``max_bytes``. The caller owns the transaction
    (commit/rollback).
    """
    if len(content) > max_bytes:
        raise _too_large(max_bytes)

    digest = hashlib.sha256(content).hexdigest()
    prior = await _by_sha(db, digest)
    if prior is not None:
        # Same bytes: reuse the row (and keep it alive a bit longer).
        now = _utcnow()
        new_expiry = _fmt(now + timedelta(days=ttl_days))
        if prior.expires_at is None or prior.expires_at < new_expiry:
            prior.expires_at = new_expiry
        return prior

    upload_id = str(uuid4())
    row = InputUploadModel(
        upload_id=upload_id,
        name=sanitize_upload_name(name),
        mime_type=(mime_type or "")[:128],
        size=len(content),
        sha256=digest,
        source=(source or "")[:32],
        created_at=_fmt(_utcnow()),
        expires_at=_fmt(_utcnow() + timedelta(days=ttl_days)),
    )
    if len(content) <= inline_max:
        row.inline_bytes = content
    else:
        directory = uploads_dir_for(db_path)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / upload_id
        target.write_bytes(content)
        row.storage_path = str(target)

    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent identical upload won the unique-index race; keep the
        # winner and drop ours (and any blob we just wrote).
        await db.rollback()
        if row.storage_path:
            try:
                Path(row.storage_path).unlink()
            except OSError:
                pass
        existing = await _by_sha(db, digest)
        if existing is None:
            raise
        return existing
    return row


async def get_upload(
    db: AsyncSession, upload_id: str
) -> InputUploadModel | None:
    return await db.get(InputUploadModel, upload_id)


async def read_upload_content(upload: InputUploadModel) -> bytes | None:
    """Return the upload's bytes, or ``None`` when the blob is gone."""
    if upload.inline_bytes is not None:
        return bytes(upload.inline_bytes)
    if upload.storage_path:
        try:
            return Path(upload.storage_path).read_bytes()
        except OSError:
            return None
    return None


async def delete_upload(db: AsyncSession, upload_id: str) -> bool:
    """Remove one upload row and its blob. Returns False when unknown."""
    upload = await get_upload(db, upload_id)
    if upload is None:
        return False
    if upload.storage_path:
        try:
            Path(upload.storage_path).unlink()
        except OSError:
            pass
    await db.delete(upload)
    await db.flush()
    return True


async def sweep_expired_uploads(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Delete uploads past ``expires_at`` (and their blobs). Idempotent.

    A reference already written into a Step's ``input_message`` stays
    valid for the job's lifetime — the TTL (default 7 days) is far
    longer than any chat turn, and a claimed Step has already been
    materialized by the daemon.
    """
    cutoff = _fmt(now or _utcnow())
    rows = (
        await db.execute(
            select(InputUploadModel).where(
                InputUploadModel.expires_at.is_not(None),
                InputUploadModel.expires_at < cutoff,
            )
        )
    ).scalars().all()
    for row in rows:
        if row.storage_path:
            try:
                Path(row.storage_path).unlink()
            except OSError:
                pass
        await db.delete(row)
    if rows:
        log.info("Swept %d expired input upload(s)", len(rows))
    return len(rows)


__all__ = [
    "sanitize_upload_name",
    "uploads_dir_for",
    "upload_to_out",
    "create_upload",
    "get_upload",
    "read_upload_content",
    "delete_upload",
    "sweep_expired_uploads",
]
