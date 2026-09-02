"""ServerClient — async HTTP client for the agt-server API."""

from __future__ import annotations

import asyncio
import json
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
        daemon_name: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._daemon_id = daemon_id
        self._daemon_name = daemon_name
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
        payload: dict[str, Any] = {
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
        if self._daemon_name:
            payload["daemon_name"] = self._daemon_name
        resp = await self._post("/api/runtimes/register", payload)
        return int(resp.get("registered", 0))

    async def list_runtimes(self) -> list[dict]:
        resp = await self._get("/api/runtimes")
        return resp.get("runtimes", [])

    async def deregister_runtimes(self) -> int:
        """Remove all runtime registrations for this daemon (called on shutdown)."""
        resp = await self._do_delete(f"/api/runtimes/unregister/{self._daemon_id}")
        return int(resp.get("removed", 0))

    # ── Daemons (aggregated registry view, Phase 4) ────────────────

    async def list_daemons(self) -> list[dict]:
        """List every known daemon (offline included), aggregated by id."""
        resp = await self._get("/api/daemons")
        return resp.get("daemons", [])

    async def get_daemon(self, daemon_id: str) -> dict | None:
        """Return one daemon's aggregated details, or None if it has none."""
        try:
            return await self._get(f"/api/daemons/{daemon_id}")
        except RPCRejected as exc:
            if exc.status == 404:
                return None
            raise

    async def list_daemon_bindings(self, daemon_id: str) -> list[dict]:
        """List every identity binding on a daemon (across all identities)."""
        resp = await self._get(f"/api/daemons/{daemon_id}/bindings")
        return resp.get("bindings", [])

    async def list_daemon_sessions(
        self, daemon_id: str, limit: int = 200
    ) -> list[dict]:
        """List every conversation session a daemon has executed (cross-dir)."""
        resp = await self._get(
            f"/api/daemons/{daemon_id}/sessions", params={"limit": limit}
        )
        return resp.get("sessions", [])

    async def resolve_session(self, session_id: str) -> dict | None:
        """Reverse-lookup a session → {task_id, provider, identity, dir}.

        Returns None when the Server has no such session (404).
        """
        try:
            return await self._get(f"/api/sessions/{session_id}")
        except RPCRejected as exc:
            if exc.status == 404:
                return None
            raise

    async def get_session_history(self, session_id: str) -> dict | None:
        """Fetch a remote session's transcript via the Server.

        Returns ``{"messages": [...], "not_implemented": bool}`` on success,
        or ``None`` when the Server has no such session (404). Raises
        ``RPCRejected`` for 503 (daemon offline) / 502 (daemon rejection).
        """
        try:
            return await self._get(f"/api/sessions/{session_id}/history")
        except RPCRejected as exc:
            if exc.status == 404:
                return None
            raise

    # ── Tasks ─────────────────────────────────────────────────────

    async def submit_task(
        self,
        prompt: str,
        provider: str = "claude",
        task_id: str | None = None,
        target_daemon_id: str | None = None,
        work_dir: str | None = None,
        identity_did: str | None = None,
        agent_cwd: str | None = None,
    ) -> str:
        """Submit a task and return the assigned task_id.

        ``work_dir`` (Phase 4) is the AGT metadata directory derived from
        the user's cwd. The server persists it ONLY for local/free tasks
        (NULL for remote targets so it doesn't leak the submitter's
        filesystem layout). The CLI submit paths set ``None`` when the
        user passed ``--no-work-dir`` (legacy daemon-local layout).

        ``identity_did`` (Phase 4 Step 4): when set, the server resolves
        the actual (provider, target_daemon_id) via ``select_runtime``
        using ``provider`` as the runtime_hint; the result overrides the
        submitted values. Selection failures raise ``RPCRejected``.

        ``agent_cwd`` (cwd injection): the agent subprocess's working
        directory on the daemon machine — also where IDENTITY.md /
        AGENTS.md are injected for identity-aware tasks. Forwarded to
        the daemon via TaskPayload so it can override the daemon-side
        fallback (``work_dir_root / provider / task_id``). NULL means
        "daemon, use your default".
        """
        payload: dict[str, Any] = {"prompt": prompt, "provider": provider}
        if task_id is not None:
            payload["task_id"] = task_id
        if target_daemon_id is not None:
            payload["target_daemon_id"] = target_daemon_id
        if work_dir is not None:
            payload["work_dir"] = work_dir
        if identity_did is not None:
            payload["identity_did"] = identity_did
        if agent_cwd is not None:
            payload["agent_cwd"] = agent_cwd
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
        outcome: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
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
            "outcome": outcome,
        }
        if artifacts:
            # Phase 2: declared deliverables. Omitted (not []) when
            # empty so legacy payloads stay byte-identical.
            payload["artifacts"] = artifacts
        resp = await self._post(f"/api/tasks/{task_id}/report", payload)
        return bool(resp.get("recorded", False))

    async def upload_artifact(
        self,
        *,
        task_id: str,
        artifact: dict[str, Any],
        content_b64: str,
    ) -> dict[str, Any]:
        """Eager-upload one artifact's bytes (HTTP-reachable daemon path).

        Phase 2: used before the terminal report when no reverse-dial WS
        link exists — large content reaches the server in one step and
        the report can then reference it metadata-only (registration is
        idempotent on ``(step, name, sha256)``).
        """
        return await self._post(
            "/api/artifacts",
            {
                "daemon_id": self._daemon_id,
                "task_id": task_id,
                "artifact": artifact,
                "content_b64": content_b64,
            },
        )

    async def get_artifact_content(self, artifact_id: str) -> bytes:
        """Fetch an artifact's bytes over HTTP (input-side resolution)."""
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.get(
            f"/api/artifacts/{artifact_id}/content",
            headers=self._headers(),
        )
        if resp.status_code >= 300:
            raise RPCRejected(resp.status_code, {"raw": resp.text})
        return resp.content

    async def report_progress(self, task_id: str, stdout_prefix: str) -> bool:
        """Report an incremental stdout prefix (Phase 5 remote streaming).

        Best-effort: the caller treats failures as ignorable — the next
        sample supersedes this one, and the terminal ``report_task`` is the
        authoritative write.
        """
        payload = {
            "daemon_id": self._daemon_id,
            "task_id": task_id,
            "stdout_prefix": stdout_prefix,
        }
        resp = await self._post(f"/api/tasks/{task_id}/progress", payload)
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

    async def stream_task(
        self,
        task_id: str,
        *,
        read_timeout: float = 30.0,
        connect_timeout: float = 10.0,
    ):
        """Subscribe to a task's SSE stream (Phase 5 remote streaming).

        Yields parsed event dicts with a ``type`` key: ``snapshot``,
        ``delta``, or ``done``. The server sends a ``: ping`` comment frame
        every ~15s, so a healthy stream never hits ``read_timeout``; a dead
        server raises ``httpx.ReadTimeout``, which the caller uses to
        trigger reconnect/fallback.

        ``httpx.ConnectError`` / ``httpx.TransportError`` propagate to the
        caller for the same reason — they mean "reconnect, don't hang".
        """
        assert self._client is not None, "ServerClient must be used as async context manager"
        timeout = httpx.Timeout(
            connect_timeout,
            read=read_timeout,
            write=10.0,
            pool=10.0,
        )
        async with self._client.stream(
            "GET",
            f"/api/tasks/{task_id}/stream",
            headers=self._headers(),
            timeout=timeout,
        ) as resp:
            if resp.status_code >= 300:
                raise RPCRejected(resp.status_code, {"raw": ""})
            event_type = "message"
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    # SSE comment (keep-alive ``: ping``) — skip.
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line[len("data:"):].strip())
                    payload.setdefault("type", event_type)
                    yield payload

    # ── Jobs ──────────────────────────────────────────────────────

    async def create_job(
        self,
        prompt: str,
        provider: str = "claude",
        target_daemon_id: str | None = None,
        work_dir: str | None = None,
        identity_did: str | None = None,
        agent_cwd: str | None = None,
    ) -> dict:
        """Create a Job with its first Step. Returns {"job_id", "task_id", "status"}."""
        payload: dict[str, Any] = {"prompt": prompt, "provider": provider}
        if target_daemon_id is not None:
            payload["target_daemon_id"] = target_daemon_id
        if work_dir is not None:
            payload["work_dir"] = work_dir
        if identity_did is not None:
            payload["identity_did"] = identity_did
        if agent_cwd is not None:
            payload["agent_cwd"] = agent_cwd
        return await self._post("/api/jobs", payload)

    async def job_input(self, job_id: str, prompt: str) -> dict:
        """Append a new Step to an existing Job."""
        return await self._post(f"/api/jobs/{job_id}/input", {"prompt": prompt})

    async def job_status(self, job_id: str) -> dict:
        return await self._get(f"/api/jobs/{job_id}/status")

    async def job_cancel(self, job_id: str) -> bool:
        resp = await self._post(f"/api/jobs/{job_id}/cancel", {})
        return bool(resp.get("cancelled", False))

    async def wait_job(
        self,
        job_id: str,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> dict:
        """Poll a Job until it leaves pending/running/waiting_input, or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            status = await self.job_status(job_id)
            if status.get("status") not in ("pending", "running", "waiting_input"):
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"job {job_id} did not settle within {timeout:.0f}s "
                    f"(status={status.get('status')})"
                )
            await asyncio.sleep(poll_interval)

    # ── Sessions ──────────────────────────────────────────────────

    async def get_session(
        self,
        job_id: str,
        provider: str,
        identity_did: str | None = None,
    ) -> str | None:
        """Return session_id for the given job+provider (+identity), or None.

        ``identity_did`` omitted (or None) means the identity-less slot —
        matching the server's ``""`` sentinel for legacy sessions.
        """
        params: dict[str, Any] = {"job_id": job_id, "provider": provider}
        if identity_did is not None:
            params["identity_did"] = identity_did
        try:
            resp = await self._get("/api/sessions", params=params)
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

        ``daemon_name`` rides along when the client was built with one —
        the server's upsert replaces the daemon's rows on every
        heartbeat, so without it the label set at register time would
        be wiped 30s later.
        """
        payload: dict[str, Any] = {
            "daemon_id": self._daemon_id,
            "active_task_count": active_task_count,
        }
        if runtimes is not None:
            payload["runtimes"] = list(runtimes)
        if self._daemon_name:
            payload["daemon_name"] = self._daemon_name
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
        """Force regenerate AGENTS.md from current database state."""
        return await self._post(f"/api/identities/{did}/sync-files", {})

    # ── Identity bindings + selection (Phase 4 Step 2) ─────────────

    async def list_bindings(self, did: str, daemon_id: str | None = None) -> list[dict]:
        """List an identity's bindings, optionally filtered by daemon."""
        params: dict[str, Any] = {}
        if daemon_id:
            params["daemon_id"] = daemon_id
        resp = await self._get(f"/api/identities/{did}/bindings", params=params or None)
        return resp.get("bindings", [])

    async def create_binding(self, did: str, payload: dict) -> dict:
        """Create a binding (daemon_id + provider + priority/is_default)."""
        return await self._post(f"/api/identities/{did}/bindings", payload)

    async def delete_binding(self, did: str, binding_id: int) -> bool:
        """Delete a binding by id. Returns True on 204."""
        assert self._client is not None, "ServerClient must be used as async context manager"
        resp = await self._client.delete(
            f"/api/identities/{did}/bindings/{binding_id}",
            headers=self._headers(),
        )
        if resp.status_code == 204:
            return True
        if resp.status_code >= 300:
            raise RPCRejected(
                resp.status_code,
                resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text},
            )
        return False

    async def set_default_binding(self, did: str, binding_id: int) -> dict:
        """Atomically switch the identity's default binding."""
        return await self._post(
            f"/api/identities/{did}/bindings/{binding_id}/default", {}
        )

    async def resolve_identity(
        self,
        identity_did: str,
        runtime_hint: str | None = None,
        daemon_id: str = "local",
    ) -> dict:
        """Dry-run selection: which runtime would this identity run on?"""
        payload: dict[str, Any] = {"identity_did": identity_did, "daemon_id": daemon_id}
        if runtime_hint is not None:
            payload["runtime_hint"] = runtime_hint
        return await self._post("/api/identities/resolve", payload)


__all__ = ["ServerClient", "DEFAULT_TIMEOUT"]
