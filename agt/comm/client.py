"""ServerClient — async HTTP client for the agt-server API."""

from __future__ import annotations

import asyncio
import logging
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
    ) -> str:
        payload: dict[str, Any] = {"prompt": prompt, "provider": provider}
        if task_id is not None:
            payload["task_id"] = task_id
        resp = await self._post("/api/tasks/submit", payload)
        return resp["task_id"]

    async def claim_tasks(self, timeout_seconds: int = 30) -> list[dict]:
        payload = {
            "daemon_id": self._daemon_id,
            "max_concurrent": 1,
            "timeout_seconds": timeout_seconds,
        }
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

    async def heartbeat(self, active_task_count: int = 0) -> dict:
        payload = {
            "daemon_id": self._daemon_id,
            "active_task_count": active_task_count,
        }
        return await self._post("/api/heartbeat", payload)

    # ── Health ────────────────────────────────────────────────────

    async def health(self) -> bool:
        try:
            resp = await self._get("/api/health")
            return resp.get("status") == "ok"
        except (RPCRejected, httpx.HTTPError):
            return False


__all__ = ["ServerClient", "DEFAULT_TIMEOUT"]