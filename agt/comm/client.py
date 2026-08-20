"""ServerClient — async HTTP client for the agt-server API."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..runtime.models import Runtime
from .errors import RPCRejected

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 35.0  # matches Server's claim long-poll cap


class ServerClient:
    """Async HTTP client wrapping every Server endpoint the daemon needs."""

    def __init__(
        self,
        base_url: str,
        daemon_id: str,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._daemon_id = daemon_id
        self._token = token
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ServerClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _post(self, path: str, payload: dict) -> dict[str, Any]:
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.post(path, json=payload, headers=self._headers())
        if resp.status_code >= 300:
            raise RPCRejected(resp.status_code, resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text})
        return resp.json()

    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.get(path, params=params, headers=self._headers())
        if resp.status_code >= 300:
            raise RPCRejected(resp.status_code, {"raw": resp.text})
        return resp.json()

    async def _do_delete(self, path: str) -> dict[str, Any]:
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.delete(path, headers=self._headers())
        if resp.status_code >= 300:
            raise RPCRejected(resp.status_code, {"raw": resp.text})
        return resp.json()

    async def _put(self, path: str, payload: dict) -> dict[str, Any]:
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.put(path, json=payload, headers=self._headers())
        if resp.status_code >= 300:
            raise RPCRejected(resp.status_code, resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text})
        return resp.json()

    # ── Runtimes ──────────────────────────────────────────────────

    async def register_runtimes(self, runtimes: list[Runtime]) -> int:
        """Wipe the daemon's prior runtimes and re-register the given list."""
        payload = {
            "daemon_id": self._daemon_id,
            "runtimes": [
                {
                    "name": rt.name,
                    "type": rt.type,
                    "version": rt.version,
                    "status": rt.status,
                    "device_name": rt.device_name,
                }
                for rt in runtimes
            ],
        }
        resp = await self._post("/api/runtimes/register", payload)
        return int(resp.get("registered", 0))

    async def list_runtimes(self) -> list[dict]:
        resp = await self._get("/api/runtimes")
        return resp.get("runtimes", [])

    async def deregister_runtimes(self) -> int:
        """Remove all runtime registrations for this daemon (called on shutdown)."""
        resp = await self._do_delete(f"/api/runtimes/unregister/{self._daemon_id}")
        return int(resp.get("removed", 0))

    # ── Tasks ─────────────────────────────────────────────────────

    async def submit_task(
        self,
        prompt: str,
        provider: str = "claude",
        task_id: str | None = None,
        target_daemon_id: str | None = None,
        work_dir: str | None = None,
    ) -> str:
        """Submit a task and return the assigned task_id.

        ``work_dir`` (Phase 4) is the AGT metadata directory derived from
        the user's cwd. The server persists it ONLY for local/free tasks
        (NULL for remote targets so it doesn't leak the submitter's
        filesystem layout). The CLI submit paths set ``None`` when the
        user passed ``--no-work-dir`` (legacy daemon-local layout).
        """
        payload: dict[str, Any] = {"prompt": prompt, "provider": provider}
        if task_id is not None:
            payload["task_id"] = task_id
        if target_daemon_id is not None:
            payload["target_daemon_id"] = target_daemon_id
        if work_dir is not None:
            payload["work_dir"] = work_dir
        resp = await self._post("/api/tasks/submit", payload)
        return resp["task_id"]

    async def claim_tasks(
        self,
        timeout_seconds: int = 30,
        runtime_ids: list[str] | None = None,
    ) -> list[dict]:
        """Claim tasks for this daemon. ``runtime_ids`` filters by provider
        (Step 3.4); ``None`` preserves the old "no filter" behavior."""
        payload: dict[str, Any] = {
            "daemon_id": self._daemon_id,
            "max_concurrent": 1,
            "timeout_seconds": timeout_seconds,
        }
        if runtime_ids is not None:
            payload["runtime_ids"] = list(runtime_ids)
        resp = await self._post("/api/tasks/claim", payload)
        return resp.get("tasks", [])

    async def report_task(
        self,
        task_id: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration: float,
        status: str,
        session_id: str | None = None,
        resume_rejected: bool = False,
    ) -> bool:
        payload = {
            "daemon_id": self._daemon_id,
            "task_id": task_id,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration": duration,
            "status": status,
            "session_id": session_id,
            "resume_rejected": resume_rejected,
        }
        resp = await self._post(f"/api/tasks/{task_id}/report", payload)
        return bool(resp.get("recorded", False))

    async def task_status(self, task_id: str) -> dict:
        return await self._get(f"/api/tasks/{task_id}/status")

    async def list_tasks(
        self,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """List tasks. ``statuses`` filters (e.g. ['completed','failed']); None = all."""
        params: dict[str, Any] = {"limit": limit}
        if statuses:
            params["statuses"] = ",".join(statuses)
        resp = await self._get("/api/tasks", params=params)
        return resp if isinstance(resp, list) else resp.get("tasks", [])

    async def wait_task(
        self,
        task_id: str,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> dict:
        """Poll ``task_id`` until it leaves pending/running, or ``timeout``.

        Returns the final status dict. Raises ``TimeoutError`` if the task
        never settles — the caller decides how to present that (e.g. "target
        daemon offline"). Terminal statuses are completed/failed/timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            status = await self.task_status(task_id)
            if status.get("status") not in ("pending", "running"):
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"task {task_id} did not settle within {timeout:.0f}s "
                    f"(status={status.get('status')})"
                )
            await asyncio.sleep(poll_interval)

    # ── Sessions ──────────────────────────────────────────────────

    async def get_session(self, task_id: str, provider: str) -> str | None:
        """Return session_id for the given task+provider, or None if none exists."""
        try:
            resp = await self._get(
                "/api/sessions",
                params={"task_id": task_id, "provider": provider},
            )
        except RPCRejected as exc:
            if exc.status == 404:
                return None
            raise
        return resp.get("session_id")

    # ── Heartbeat ─────────────────────────────────────────────────

    async def heartbeat(
        self,
        active_task_count: int = 0,
        runtimes: list[dict] | None = None,
    ) -> dict:
        """Send a heartbeat. ``runtimes`` is an optional snapshot that, if
        provided, lets the server refresh its runtime registry for this
        daemon in the same call (avoids a separate ``register_runtimes``
        POST). ``None`` preserves backward compatibility with old
        server builds that don't know about the field.
        """
        payload: dict[str, Any] = {
            "daemon_id": self._daemon_id,
            "active_task_count": active_task_count,
        }
        if runtimes is not None:
            payload["runtimes"] = list(runtimes)
        return await self._post("/api/heartbeat", payload)

    # ── Health ────────────────────────────────────────────────────

    async def health(self) -> bool:
        try:
            resp = await self._get("/api/health")
            return resp.get("status") == "ok"
        except (RPCRejected, httpx.HTTPError):
            return False

    # ── Identities (Phase 4 Step 1) ───────────────────────────────

    async def list_identities(
        self,
        daemon_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List identities with optional daemon_id / status filters."""
        params: dict[str, Any] = {}
        if daemon_id:
            params["daemon_id"] = daemon_id
        if status:
            params["status_filter"] = status
        resp = await self._get("/api/identities", params=params or None)
        return resp.get("identities", [])

    async def create_identity(self, payload: dict) -> dict:
        """Create a new identity. Returns the full identity (incl. did)."""
        return await self._post("/api/identities", payload)

    async def get_identity(self, did: str) -> dict | None:
        """Return one identity by DID, or None if it doesn't exist."""
        try:
            # DIDs contain ':' which is safe unencoded in a path segment,
            # but be explicit so proxies/log viewers show the full DID.
            return await self._get(f"/api/identities/{did}")
        except RPCRejected as exc:
            if exc.status == 404:
                return None
            raise

    async def update_identity(self, did: str, payload: dict) -> dict:
        """Partially update an identity (name/description/capabilities)."""
        return await self._put(f"/api/identities/{did}", payload)

    async def deprecate_identity(self, did: str) -> dict:
        """Soft-delete an identity (status='deprecated')."""
        return await self._post(f"/api/identities/{did}/deprecate", {})

    async def revoke_identity(self, did: str) -> dict:
        """Hard-revoke an identity (status='revoked')."""
        return await self._post(f"/api/identities/{did}/revoke", {})

    async def delete_identity(self, did: str) -> bool:
        """Physically delete an identity. Returns True on 204."""
        resp = await self._client.delete(
            f"/api/identities/{did}", headers=self._headers()
        )
        if resp.status_code == 204:
            return True
        if resp.status_code >= 300:
            raise RPCRejected(
                resp.status_code,
                resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text},
            )
        return False

    async def sync_identity_files(self, did: str) -> dict:
        """Force regenerate AGENT.md from current database state."""
        return await self._post(f"/api/identities/{did}/sync-files", {})


__all__ = ["ServerClient", "DEFAULT_TIMEOUT"]