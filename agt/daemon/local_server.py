"""LocalResourceServer — a tiny HTTP server bound to 127.0.0.1.

Used by agent subprocesses to fetch resources (git checkout of repos, skill
bundles) from the parent daemon. This is NOT a daemon control plane and
NOT a CLI endpoint.

Auth model:
    Every endpoint (except ``/health``) requires the ``X-Daemon-Local-Token``
    header to match the random per-launch token configured on the daemon.
    Subprocesses inherit the token via the ``AGT_DAEMON_TOKEN`` env var. The
    token is rotated every daemon restart, so a leaked token self-expires on
    restart. ``127.0.0.1`` binding still applies as defense-in-depth.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("agt.daemon.local")


class CheckoutRequest(BaseModel):
    """Body schema for ``POST /repo/checkout``.

    Defined at module level so Pydantic's forward-ref resolution can find it.
    """

    repo_url: str
    branch: str = "main"
    work_dir: str | None = None


class LocalResourceServer:
    """Bind an HTTP server to 127.0.0.1:<port>.

    Spawns a uvicorn server with strict host checking; only localhost
    connections are accepted. All endpoints (except /health) require the
    shared ``local_token`` via ``X-Daemon-Local-Token``.
    """

    def __init__(self, port: int, local_token: str) -> None:
        self._port = port
        self._local_token = local_token
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the server in the background; returns immediately."""
        config = uvicorn.Config(
            self._make_app(),
            host="127.0.0.1",
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Reference kept; uvicorn keeps the task alive internally.
        log.info("LocalResourceServer listening on 127.0.0.1:%d", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        log.info("LocalResourceServer stopped")

    # ── Auth dependency ──────────────────────────────────────────

    def _verify_local_call(
        self,
        x_daemon_local_token: str | None = Header(None, alias="X-Daemon-Local-Token"),
    ) -> None:
        """FastAPI dependency: reject any call without the matching token."""
        if not x_daemon_local_token:
            raise HTTPException(status_code=401, detail="daemon local token required")
        if x_daemon_local_token != self._local_token:
            raise HTTPException(status_code=403, detail="daemon local token mismatch")

    def _make_app(self) -> FastAPI:
        """Build the FastAPI app with our endpoints."""
        app = FastAPI(title="agt-local-resources", version="0.2.0")
        # Capture self in the dependency closure so each route shares the
        # same token check.
        verify = self._verify_local_call

        @app.get("/health")
        async def health() -> dict:
            # Health stays unauthenticated so external liveness probes (e.g.
            # an ops dashboard) don't need the rotating token.
            return {"status": "ok"}

        @app.post("/repo/checkout")
        async def repo_checkout(
            req: CheckoutRequest = Body(...),
            request: Request = None,  # type: ignore[assignment]
            _token: None = Depends(verify),
        ) -> dict:
            if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
                raise HTTPException(status_code=403, detail="localhost only")

            work_dir = (
                Path(req.work_dir).expanduser()
                if req.work_dir
                else Path.home() / ".agt" / "workspaces" / "checkout"
            )
            work_dir.mkdir(parents=True, exist_ok=True)

            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    "--branch",
                    req.branch,
                    "--single-branch",
                    req.repo_url,
                    str(work_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except (FileNotFoundError, asyncio.TimeoutError) as exc:
                raise HTTPException(status_code=500, detail=f"git failed: {exc}")
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"git clone failed: {stderr.decode(errors='replace')[:500]}",
                )
            return {"work_dir": str(work_dir)}

        return app


__all__ = ["LocalResourceServer", "CheckoutRequest"]