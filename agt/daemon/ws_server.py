"""DaemonWsServer — daemon-side WebSocket server for reverse-dial (Step 3.5).

In the reverse-dial topology, the **Server** dials the daemon instead of
the other way around. 
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve

from ..agent_history import (
    delete_provider_session,
    find_provider_session_files,
    load_history,
)
from ..comm.errors import WSRPCUnavailable
from ..comm.wsrpc_link import WsRpcLink
from ..server.services.auth_service import TaskToken
from .config import _is_path_under

log = logging.getLogger("agt.daemon.ws")

# Same cap as the Server-side console browser: a directory with more
# entries than this is truncated rather than streamed.
_MAX_DIRECTORY_ENTRIES = 1000

# Hook called once per inbound server connection, after the link is
# exposed as ``current_link`` and ready to issue RPC.
OnConnectHook = Callable[[WsRpcLink], Awaitable[None]]


class DaemonWsServer:
    """Daemon-side WS server. Exposes ``current_link`` for the claim loop."""

    def __init__(
        self,
        port: int,
        secret: str,
        daemon_id: str,
        url: str,
        *,
        on_connect: OnConnectHook | None = None,
        allowlist: list[Path] | None = None,
    ) -> None:
        self._port = port
        self._secret = secret
        self._daemon_id = daemon_id
        self._url = url
        self._server: serve | None = None
        self.current_link: WsRpcLink | None = None
        self._link_lock = asyncio.Lock()
        self._on_connect = on_connect
        # Roots the remote console may browse (``fs.list``). Kept identical
        # to the execution allowlist so "browsable" always implies
        # "runnable": a path the Server can show the user is a path
        # ``Daemon.run`` will accept.
        self._allowlist = list(allowlist) if allowlist else None
        # Woken by an inbound ``tasks.available`` notify from the Server.
        self.claim_wakeup = asyncio.Event()

    async def start(self) -> None:
        """Bind the WS server on ``0.0.0.0:<port>`` and start serving."""
        self._server = await serve(
            self._handler,
            "0.0.0.0",
            self._port,
            ping_interval=20,
            ping_timeout=20,
        )
        log.info("DaemonWsServer listening on 0.0.0.0:%d", self._port)

    async def stop(self) -> None:
        """Tear down the server and any active link."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        async with self._link_lock:
            link = self.current_link
            self.current_link = None
        if link is not None:
            try:
                await link.close()
            except Exception as exc:
                log.warning("DaemonWsServer link close failed: %s", exc)

    async def _handler(self, ws: ServerConnection) -> None:
        """Per-connection handler: auth → handshake → expose link."""
        # 1. Auth: verify X-Server-Token. Use the raw header lookup before
        #    we ever read a frame, so an unauthenticated caller never
        #    triggers business logic.
        token = ws.request.headers.get("X-Server-Token")
        if not token:
            log.info("DaemonWsServer: missing X-Server-Token; closing 1008")
            await ws.close(code=1008, reason="token required")
            return
        try:
            TaskToken.verify(
                token, self._secret, audience=self._daemon_id, issuer="agt-server"
            )
        except PermissionError as exc:
            log.info("DaemonWsServer: invalid server token (%s); closing 1008", exc)
            await ws.close(code=1008, reason="invalid token")
            return

        # 2. Handshake: capability negotiation + daemon identity proof.
        #    The server must verify our daemon_token before accepting RPC.
        daemon_token = TaskToken.create_daemon(
            self._daemon_id, self._url, self._secret
        )
        await ws.send(
            json.dumps(
                {
                    "type": "heartbeat_ack",
                    "rpc_v1": True,
                    "daemon_token": daemon_token,
                }
            )
        )

        # 3. Build the bidirectional link and start its read loop. 
        link = WsRpcLink(ws, on_request=self._handle_inbound_request)
        await link.start()
        async with self._link_lock:
            self.current_link = link

        # 4. Fire the on_connect hook (if any). This is where the daemon
        # pushes its runtime snapshot so the Server's registry reflects
        # the current probe immediately, instead of waiting up to 30s
        # for the next heartbeat. The hook is best-effort: any error is
        # logged and swallowed so a buggy hook can't poison the link.
        if self._on_connect is not None:
            try:
                await self._on_connect(link)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("DaemonWsServer on_connect hook failed: %s", exc)

        peer = getattr(ws, "remote_address", None)
        log.info("DaemonWsServer: server dial accepted from %s", peer)

        try:
            if link._read_task is not None:  # noqa: SLF001
                # Block here until the read loop exits (i.e. disconnect).
                await link._read_task
            log.info("DaemonWsServer: link closed (peer %s)", peer)
        except (asyncio.CancelledError, Exception) as exc:
            log.info("DaemonWsServer: link terminated (%s): %s", peer, exc)
        finally:
            async with self._link_lock:
                if self.current_link is link:
                    self.current_link = None
            try:
                await link.close()
            except Exception:
                pass

    async def _handle_inbound_request(self, method: str, body: dict) -> dict:
        """Daemon-side inbound RPC handler.
        """
        if method == "tasks.available":
            self.wake_claim_loop()
            return {"ok": True}
        if method == "tasks.cancel":
            return self._handle_tasks_cancel(body)
        if method == "session.history":
            return await self._handle_session_history(body)
        if method == "session.delete":
            return await self._handle_session_delete(body)
        if method == "artifacts.fetch":
            return await self._handle_artifacts_fetch(body)
        if method == "fs.list":
            return await self._handle_fs_list(body)
        raise ValueError({"type": "unknown_method", "message": method})

    async def _handle_fs_list(self, body: dict) -> dict:
        """List directories on THIS machine for the console's project picker.

        The remote console cannot stat a path on this daemon, so the picker
        asks us. Roots are the same ``agent_cwd_allowlist`` used by
        ``Daemon.run`` — anything the user can pick here is therefore also
        a path the daemon will accept as ``agent_cwd``.

        Runs in a worker thread: ``iterdir`` on a network mount can block.
        """
        raw = body.get("path")
        if raw is not None and not isinstance(raw, str):
            raise ValueError({
                "type": "validation_error",
                "message": "fs.list path must be a string",
            })
        return await asyncio.to_thread(self._list_directory, raw)

    def _browse_roots(self) -> list[Path]:
        raw = self._allowlist or [Path.home()]
        roots: list[Path] = []
        for item in raw:
            try:
                roots.append(Path(item).expanduser().resolve(strict=False))
            except (OSError, RuntimeError):
                continue
        return roots or [Path.home().resolve()]

    def _list_directory(self, raw_path: str | None) -> dict:
        """Synchronous body of ``fs.list`` (see ``_handle_fs_list``)."""
        roots = self._browse_roots()
        if raw_path:
            text = raw_path.strip()
            if not text:
                raise ValueError({
                    "type": "path_error",
                    "reason": "path_required",
                    "message": "path cannot be blank",
                })
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                raise ValueError({
                    "type": "path_error",
                    "reason": "relative_path",
                    "message": "path must be absolute",
                })
            try:
                current = candidate.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError({
                    "type": "path_error",
                    "reason": "invalid_path",
                    "message": f"cannot resolve path: {exc}",
                }) from None
            if not any(_is_path_under(current, root) for root in roots):
                raise ValueError({
                    "type": "path_error",
                    "reason": "path_outside_roots",
                    "message": "path is outside the daemon's agent_cwd_allowlist",
                })
        else:
            current = roots[0]

        try:
            if not current.is_dir():
                raise ValueError({
                    "type": "path_error",
                    "reason": "not_a_directory",
                    "message": "path is not a readable directory",
                })
            candidates = list(current.iterdir())
        except OSError as exc:
            raise ValueError({
                "type": "path_error",
                "reason": "directory_not_readable",
                "message": f"cannot read directory: {exc}",
            }) from None

        parent: Path | None = None
        try:
            candidate_parent = current.parent
            if any(_is_path_under(candidate_parent, root) for root in roots):
                parent = candidate_parent
        except (OSError, RuntimeError):
            parent = None

        directories: list[dict[str, str]] = []
        for child in candidates:
            if len(directories) >= _MAX_DIRECTORY_ENTRIES:
                break
            try:
                resolved = child.resolve(strict=False)
                if not resolved.is_dir():
                    continue
                if not any(_is_path_under(resolved, root) for root in roots):
                    continue
            except OSError:
                continue
            directories.append({"name": child.name, "path": str(resolved)})
        directories.sort(key=lambda item: item["name"].lower())

        return {
            "roots": [str(root) for root in roots],
            "parent": str(parent) if parent is not None else None,
            "current": str(current),
            "directories": directories,
        }

    async def _handle_artifacts_fetch(self, body: dict) -> dict:
        """Serve one artifact's bytes from this daemon's local disk.

        The server relays back the ``local_path`` this daemon itself
        reported at registration time, so the path is trusted input from
        an HMAC-authenticated server. A missing/unreadable file answers
        ``artifact_missing`` so the server can mark the row ``lost``.
        """
        local_path = body.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError({
                "type": "validation_error",
                "message": "artifacts.fetch requires local_path",
            })
        import base64
        import hashlib
        from pathlib import Path

        path = Path(local_path)
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            raise ValueError({
                "type": "artifact_missing",
                "reason": "artifact_missing",
                "message": f"local file gone: {local_path}",
            }) from None
        expected = body.get("sha256")
        if isinstance(expected, str) and expected:
            digest = hashlib.sha256(content).hexdigest()
            if digest != expected:
                raise ValueError({
                    "type": "artifact_hash_mismatch",
                    "reason": "artifact_missing",
                    "message": f"sha256 mismatch: {digest}",
                })
        return {"content_b64": base64.b64encode(content).decode()}

    def _handle_tasks_cancel(self, body: dict) -> dict:
        """Server-initiated cancel (Phase 4, reverse-dial channel).

        Looks the task up in the running-task registry and cancels the
        asyncio task; the executor's CancelledError branch kills the
        agent subprocess and reports ``status="cancelled"`` through the
        normal chain. Unknown / already-finished ids are idempotent
        no-ops — the claim-channel delivery and the server's
        late-report guard cover whatever this path misses.
        """
        from . import running_tasks

        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError({
                "type": "validation_error",
                "message": "tasks.cancel requires task_id",
            })
        cancelled = running_tasks.cancel(task_id)
        return {"ok": True, "was_running": cancelled}

    async def _handle_session_history(self, body: dict) -> dict:
        """Read a session transcript from this daemon's local provider store.
        """
        provider = body.get("provider")
        session_id = body.get("session_id")
        if not isinstance(provider, str) or not isinstance(session_id, str):
            raise ValueError({
                "type": "validation_error",
                "message": "session.history requires provider + session_id",
            })
        # Run in a worker thread: the codex reader ``rglob``s the sessions
        # tree, which can block the event loop for a beat on a busy daemon.
        try:
            messages = await asyncio.to_thread(load_history, provider, session_id)
        except NotImplementedError as exc:
            return {"messages": [], "not_implemented": True, "reason": str(exc)}
        return {
            "messages": [
                {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                for m in messages
            ],
            "not_implemented": False,
        }

    async def _handle_session_delete(self, body: dict) -> dict:
        """Delete a provider transcript for a Server-managed session.

        The local direct-chat ``sessions.json`` is deliberately outside
        this Server/daemon RPC surface and is never read or modified here.
        """
        provider = body.get("provider")
        session_id = body.get("session_id")
        if not isinstance(provider, str) or not isinstance(session_id, str):
            raise ValueError({
                "type": "validation_error",
                "message": "session.delete requires provider + session_id",
            })
        dry_run = bool(body.get("dry_run", False))

        if dry_run:
            try:
                found_paths = await asyncio.to_thread(
                    find_provider_session_files, provider, session_id
                )
            except NotImplementedError as exc:
                return {
                    "ok": False,
                    "not_implemented": True,
                    "dry_run": True,
                    "reason": str(exc),
                    "found_paths": [],
                    "cwd_index_found": False,
                }
            return {
                "ok": True,
                "supported": True,
                "dry_run": True,
                "found_paths": [str(path) for path in found_paths],
                "cwd_index_found": False,
                "missing": not found_paths,
            }

        result = await asyncio.to_thread(
            delete_provider_session, provider, session_id
        )
        if not result.supported:
            return {
                "ok": False,
                "not_implemented": True,
                "reason": result.reason,
                "deleted_paths": [],
                "cwd_index_deleted": False,
            }
        if result.errors:
            raise ValueError({
                "type": "provider_delete_failed",
                "message": "; ".join(result.errors),
                "deleted_paths": [str(path) for path in result.deleted_paths],
            })

        return {
            "ok": True,
            "not_implemented": False,
            "deleted_paths": [str(path) for path in result.deleted_paths],
            "cwd_index_deleted": False,
        }

    def wake_claim_loop(self) -> None:
        """Wake the daemon's claim loop immediately.

        Safe to call from any thread/task; if the loop is already awake
        the event remains set, which causes the next sleep to return
        immediately.
        """
        self.claim_wakeup.set()

    def _require_link(self) -> WsRpcLink:
        """Return ``current_link`` or raise ``WSRPCUnavailable`` (no connection)."""
        link = self.current_link
        if link is None or link.closed:
            raise WSRPCUnavailable("no link")
        return link


__all__ = ["DaemonWsServer"]
