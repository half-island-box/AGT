from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain

if TYPE_CHECKING:
    from ..comm.client import ServerClient
    from ..comm.wsrpc_link import WsRpcLink

log = logging.getLogger("agt.daemon.ws_claim")


# `time.monotonic` is injected via a module attribute so unit tests can
# patch it without monkey-patching the stdlib.
_monotonic = time.monotonic

# Claim safety window: if a WS frame is sent but the server's response is
# uncertain, we block the next claim/report tick for this many seconds.
class ClaimStateMachine:
    def __init__(self, server_timeout: int = 5, grace: int = 2) -> None:
        self._uncertain_until: float = 0.0
        self._safe_window: float = float(server_timeout + grace)

    @property
    def blocked(self) -> bool:
        return _monotonic() < self._uncertain_until

    def mark_uncertain(self, sent_at: float) -> None:
        """Open the safety window ending at ``sent_at + safe_window``."""
        self._uncertain_until = sent_at + self._safe_window

    def reset(self) -> None:
        """Clear any pending window."""
        self._uncertain_until = 0.0

# 优先用WS通道来Claim任务和上报任务结果，如果WS通道不可用，则回退到HTTP通道
async def claim_tasks_ws_first(
    *,
    wsrpc: "WsRpcLink | None",
    http_client: "ServerClient",
    state: ClaimStateMachine,
    runtime_ids: list[str],
    max_tasks: int,
) -> tuple[list[dict], list[str]]:
    
    if wsrpc is None or not wsrpc.supports_rpc_v1 or state.blocked:
        return await http_client.claim_tasks(timeout_seconds=30)

    try:
        result = await wsrpc.call(
            "tasks.claim",
            {"runtime_ids": list(runtime_ids), "max_tasks": max_tasks},
            timeout_ms=5000,
        )
        return (
            list(result.get("tasks", [])),
            list(result.get("cancelled_task_ids", [])),
        )
    except WSRPCUnavailable:
        # Frame never left the client — HTTP fallback is safe.
        return await http_client.claim_tasks(timeout_seconds=30)
    except WSRPCUncertain as exc:
        # Frame MAY have committed; open the window, skip HTTP this tick.
        state.mark_uncertain(exc.frame_sent_at)
        return [], []
    # RPCRejected propagates unchanged — server's verdict is final.


async def report_task_ws_first(
    *,
    wsrpc: "WsRpcLink | None",
    http_client: "ServerClient",
    state: ClaimStateMachine,
    task_id: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    status: str,
    session_id: str | None,
    resume_rejected: bool,
    artifacts: list | None = None,
    outcome: str | None = None,
    outcome_payload: dict | None = None,
) -> bool:
    """Write a task result via WS first; HTTP fallback when not safe.

    Mirror of :func:`claim_tasks_ws_first` for the result-write path,
    but with two semantic differences that matter for retry safety:

    1. **Result writes are idempotent on the server side** — the task
       row's terminal fields get overwritten by whichever report lands
       last. So an Uncertain WS write followed by an HTTP retry can
       record the same result twice without ill effect (the latest one
       wins). We still honor the safety window to avoid hammering the
       server mid-frame, but the retry that follows isn't dangerous.

    2. **RPCRejected is NOT fatal here** — unlike claim, where an
       explicit rejection means "this task isn't for you" and HTTP
       fallback would be wrong. For reports, a rejection (e.g. 404 task
       not found) usually means the WS server's view of state is
       stale; falling back to HTTP may still succeed.

    Returns True if any path recorded the result (best-effort); the
    caller doesn't act on ``False`` beyond logging.
    """
    body = {
        "task_id": task_id,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration": duration,
        "status": status,
        "session_id": session_id,
        "resume_rejected": resume_rejected,
    }
    if artifacts:
        # Phase 2: declared deliverables ride the report body. Omitted
        # entirely for legacy scans with no artifacts (older servers
        # ignore unknown fields anyway, but absence keeps the payload
        # byte-identical to Phase 1).
        body["artifacts"] = artifacts
    if outcome is not None:
        body["outcome"] = outcome
    if outcome_payload is not None:
        body["outcome_payload"] = outcome_payload

    if wsrpc is not None and wsrpc.supports_rpc_v1 and not state.blocked:
        try:
            await wsrpc.call("tasks.report", body, timeout_ms=5000)
            return True
        except WSRPCUnavailable:
            # Frame never left the client — HTTP fallback is safe.
            pass
        except WSRPCUncertain as exc:
            # Mark the window so subsequent ticks of the claim/report
            # loop don't double-fire. We don't block-wait here — the
            # caller's outer scheduler will retry on the next tick,
            # which will fall through to HTTP once the window expires.
            state.mark_uncertain(exc.frame_sent_at)
            log.debug(
                "report over WS Uncertain; will retry via HTTP after "
                "safety window (task=%s)", task_id,
            )
            return False
        except RPCRejected as exc:
            # Server explicitly rejected (typically 404 task not found).
            # Still try HTTP — the server's view may have lagged and
            # HTTP can succeed. log at debug since both paths log on
            # their own failure modes.
            log.debug(
                "report over WS rejected (status=%s); falling back to HTTP",
                exc.status,
            )
            # fall through to HTTP

    # HTTP path / fallback. The result is already in our hands; failure
    # here means we couldn't deliver it. The daemon's caller logs a
    # single warning at the outer try/except.
    try:
        await http_client.report_task(
            task_id=task_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            status=status,
            session_id=session_id,
            resume_rejected=resume_rejected,
            artifacts=artifacts,
            outcome=outcome,
            outcome_payload=outcome_payload,
        )
        return True
    except Exception as exc:
        log.warning("HTTP report failed for task %s: %s", task_id, exc)
        return False


async def report_task_progress_ws_first(
    *,
    wsrpc: "WsRpcLink | None",
    http_client: "ServerClient",
    state: ClaimStateMachine,
    task_id: str,
    stdout_prefix: str,
) -> bool:
    """Report an incremental stdout prefix via WS first; HTTP fallback.

    Mirrors :func:`report_task_ws_first` but is deliberately fire-and-forget
    in semantics, because progress is best-effort by design:

    1. **Never touches the safety window** — on ``WSRPCUncertain`` we just
       drop this sample (the next sample, ``flush_interval`` later,
       supersedes it). Marking the window here would stall the *claim and
       terminal-report* paths, which is exactly the wrong trade: a lost
       progress frame is harmless, a lost report is not.
    2. **RPCRejected is swallowed** — a 404/403 mid-run means the server
       has already moved past this task (deleted / re-claimed); retrying
       via HTTP is pointless.

    Returns True if a path recorded the prefix (best-effort); the caller
    (executor sampler) ignores the result entirely.
    """
    body = {"task_id": task_id, "stdout_prefix": stdout_prefix}

    if wsrpc is not None and wsrpc.supports_rpc_v1 and not state.blocked:
        try:
            await wsrpc.call("tasks.progress", body, timeout_ms=5000)
            return True
        except WSRPCUnavailable:
            # Frame never left — HTTP fallback is safe.
            pass
        except WSRPCUncertain:
            # Outcome unknown; do NOT mark the window (best-effort). Drop
            # the sample — the next one supersedes it.
            return False
        except RPCRejected:
            # Permanent rejection; retrying over HTTP is futile.
            return False

    try:
        await http_client.report_progress(
            task_id=task_id, stdout_prefix=stdout_prefix,
        )
        return True
    except Exception as exc:
        log.debug("HTTP progress failed for task %s: %s", task_id, exc)
        return False


__all__ = [
    "ClaimStateMachine",
    "claim_tasks_ws_first",
    "report_task_ws_first",
    "report_task_progress_ws_first",
]
