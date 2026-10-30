"""Input upload routes — user → agent file attachments.

The browser console / VS Code webview / CLI upload a file here first and
receive an ``agt-upload://<upload_id>`` reference. That reference then
travels as a normal A2A FilePart inside a Message; the daemon resolves
the bytes on demand (``uploads.get`` RPC, HTTP fallback).

JSON base64 is used deliberately instead of multipart: ``python-multipart``
is not a declared dependency, and the same payload shape works from every
client we have.
"""

from __future__ import annotations

import base64
import binascii
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import UploadCreateRequest, UploadOut
from ..services.input_service import (
    create_upload,
    delete_upload,
    get_upload,
    read_upload_content,
    upload_to_out,
)
from .artifacts import _content_response

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _max_upload_bytes(request: Request) -> int:
    return int(getattr(request.app.state.config, "max_upload_bytes", 32 * 1024 * 1024))


def _ttl_days(request: Request) -> int:
    return int(getattr(request.app.state.config, "upload_ttl_days", 7))


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def create_upload_route(
    payload: UploadCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UploadOut:
    """Store one input file (idempotent on content hash)."""
    try:
        content = base64.b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid base64 content",
        ) from None

    try:
        row = await create_upload(
            db,
            name=payload.name,
            content=content,
            mime_type=payload.mime_type,
            source=payload.source,
            db_path=request.app.state.config.db_path,
            max_bytes=_max_upload_bytes(request),
            ttl_days=_ttl_days(request),
        )
    except ValueError as exc:
        body = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        if body.get("type") == "upload_too_large":
            raise HTTPException(
                # 413 Payload Too Large (literal: Starlette renamed the
                # constant across versions).
                status_code=413,
                detail=body.get("message", "upload too large"),
            ) from None
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(body or exc)
        ) from None

    await db.commit()
    return UploadOut(**upload_to_out(row))


@router.get("/{upload_id}", response_model=UploadOut)
async def upload_metadata(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
) -> UploadOut:
    row = await get_upload(db, upload_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="upload not found"
        )
    return UploadOut(**upload_to_out(row))


@router.get("/{upload_id}/content")
async def upload_content(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve the raw bytes (preview / download)."""
    row = await get_upload(db, upload_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="upload not found"
        )
    content = await read_upload_content(row)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="upload content is no longer available",
        )
    # Reuse the artifact hardening (nosniff + RFC 5987 filename) so both
    # untrusted-content paths cannot drift.
    return _content_response(
        content, SimpleNamespace(name=row.name, mime_type=row.mime_type)
    )


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload_route(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Drop an attachment the user removed before sending it."""
    removed = await delete_upload(db, upload_id)
    await db.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="upload not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
