"""Loopback-only endpoints backing the "Connection & Pairing" settings panel.

Everything here returns or handles the Server's HMAC secret, so the
whole router is gated on the **request source** being loopback. The
existing console mount switch (``console_enabled``) is deliberately not
reused: it is a coarse on/off for the whole UI, while these endpoints
need a stricter rule that also holds when
``AGT_CONSOLE_ALLOW_REMOTE=1`` exposes the console to the LAN.

The daemon-side counterpart is ``agt daemon init``; both consume the
same ``agt1.`` invite payload (see :mod:`agt.invite`).
"""

from __future__ import annotations

import ipaddress
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...daemons_file import (
    DaemonEntry,
    DaemonsFileError,
    daemons_file_path,
    read_daemons_entries,
    upsert_daemons_entry,
)
from ...invite import InviteError, encode_invite

router = APIRouter(prefix="/api/pairing", tags=["pairing"])


def _is_loopback_address(value: str) -> bool:
    """True for IPv4/IPv6 loopback literals (``127.0.0.1``, ``::1``)."""
    text = (value or "").strip().strip("[]")
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


async def require_loopback(request: Request) -> None:
    """FastAPI dependency: reject anything that did not arrive locally."""
    client = request.client.host if request.client else ""
    if not _is_loopback_address(client):
        raise HTTPException(
            status_code=403,
            detail="pairing endpoints are loopback-only",
        )


def _config(request: Request):
    return request.app.state.config


def _daemons_path(request: Request):
    cfg = _config(request)
    return cfg.daemons_path or str(daemons_file_path())


def _connected_ids(request: Request) -> set[str]:
    links = getattr(request.app.state, "daemon_links", {}) or {}
    return {daemon_id for daemon_id, link in links.items() if link.connected}


def _mask(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


class PairingStateOut(BaseModel):
    secret_configured: bool
    secret_source: str  # "env" | "file"
    daemons: list[dict[str, Any]]
    daemons_path: str


class SecretOut(BaseModel):
    secret: str | None = None
    masked: str
    source: str


class InviteOut(BaseModel):
    daemon_id: str
    invite_code: str
    command: str


class AddDaemonIn(BaseModel):
    name: str
    url: str
    capabilities: str | None = None


class AddDaemonOut(InviteOut):
    added: str
    daemons_path: str
    restart_required: bool


def _secret_source() -> str:
    return "env" if os.environ.get("AGT_SERVER_SECRET", "").strip() else "file"


@router.get("/state", response_model=PairingStateOut, dependencies=[Depends(require_loopback)])
async def pairing_state(request: Request) -> PairingStateOut:
    """Configured daemons plus whether a secret is available."""
    cfg = _config(request)
    connected = _connected_ids(request)
    path = _daemons_path(request)
    entries = read_daemons_entries(path)
    return PairingStateOut(
        secret_configured=bool(cfg.secret),
        secret_source=_secret_source(),
        daemons=[
            {
                "name": e.name,
                "url": e.url,
                "daemon_id": _effective_id(e),
                "capabilities": list(e.capabilities),
                "connected": _effective_id(e) in connected,
            }
            for e in entries
        ],
        daemons_path=path,
    )


@router.get("/secret", response_model=SecretOut, dependencies=[Depends(require_loopback)])
async def pairing_secret(request: Request, reveal: bool = False) -> SecretOut:
    """Return the secret, masked unless ``reveal=true``.

    The masked form is enough for the UI to show *that* something is
    configured; the plaintext is only fetched when the operator clicks
    "show" or copies an invite.
    """
    cfg = _config(request)
    secret = cfg.secret or ""
    if not secret:
        raise HTTPException(status_code=404, detail="no server secret configured")
    return SecretOut(
        secret=secret if reveal else None,
        masked=_mask(secret),
        source=_secret_source(),
    )


@router.get("/invite", response_model=InviteOut, dependencies=[Depends(require_loopback)])
async def pairing_invite(
    request: Request,
    name: str,
    url: str,
    daemon_id: str | None = None,
) -> InviteOut:
    """Build an invite code for an already-registered target.

    The daemons file is not modified. A target must already be configured
    because the Server only dials what it read at startup — handing out an
    invite for an unregistered URL would produce a daemon nobody reaches.
    Use ``POST /api/pairing/daemons`` to register and get a code at once.
    """
    cfg = _config(request)
    if not cfg.secret:
        raise HTTPException(status_code=404, detail="no server secret configured")
    try:
        from ...daemons_file import validate_name, validate_url

        validate_name(name)
        validate_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    target = next(
        (
            t
            for t in cfg.daemons
            if t.url == url and (daemon_id is None or t.daemon_id == daemon_id)
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=409,
            detail="no configured daemon matches that URL; "
            "register it first with POST /api/pairing/daemons",
        )
    code = encode_invite(
        secret=cfg.secret,
        url=target.url,
        daemon_id=target.daemon_id,
        name=target.name,
        ws_port=_port_of(target.url),
    )
    return InviteOut(
        daemon_id=target.daemon_id,
        invite_code=code,
        command=f"agt daemon init {code}",
    )


@router.post(
    "/daemons",
    response_model=AddDaemonOut,
    dependencies=[Depends(require_loopback)],
)
async def pairing_add_daemon(request: Request, body: AddDaemonIn) -> AddDaemonOut:
    """Register a reverse-dial target and return its invite code.

    The entry is written to the daemons file with an explicit ``id=``.
    ``ServerConfig.daemons`` is read once at app construction, so the
    new target only starts being dialed after a Server restart — hence
    ``restart_required``.
    """
    cfg = _config(request)
    if not cfg.secret:
        raise HTTPException(status_code=404, detail="no server secret configured")

    caps = tuple(
        c.strip() for c in (body.capabilities or "").replace("，", ",").split(",")
        if c.strip()
    )
    daemon_id = str(uuid.uuid4())
    try:
        outcome = upsert_daemons_entry(
            DaemonEntry(
                name=body.name.strip(),
                url=body.url.strip(),
                daemon_id=daemon_id,
                capabilities=caps,
            ),
            _daemons_path(request),
        )
    except DaemonsFileError as exc:
        status = 409 if exc.code in {"duplicate-url", "name-conflict"} else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None

    try:
        code = encode_invite(
            secret=cfg.secret,
            url=body.url.strip(),
            daemon_id=daemon_id,
            name=body.name.strip(),
            ws_port=_port_of(body.url.strip()),
        )
    except InviteError as exc:  # pragma: no cover — defensive
        raise HTTPException(status_code=400, detail=str(exc)) from None

    already_live = any(t.daemon_id == daemon_id for t in cfg.daemons)
    return AddDaemonOut(
        daemon_id=daemon_id,
        invite_code=code,
        command=f"agt daemon init {code}",
        added=outcome,
        daemons_path=_daemons_path(request),
        restart_required=not already_live,
    )


def _port_of(url: str) -> int | None:
    from ...daemon.config import _port_of_url

    raw = _port_of_url(url)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _effective_id(entry: DaemonEntry) -> str:
    """The id this entry resolves to, deriving it for legacy lines."""
    if entry.daemon_id:
        return entry.daemon_id
    from ...ids import daemon_id_from_url

    return daemon_id_from_url(entry.url)


__all__ = ["router", "require_loopback"]
