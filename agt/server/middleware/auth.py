"""verify_task_token — FastAPI dependency for agent subprocess callbacks.

Validates the X-Task-Token + X-Task-Id headers against the Server's HMAC
secret. Returns the verified payload claims.

Wiring:
    In ``create_app``:
        bind_verify_task_token(app, cfg.secret)

    On a route:
        @router.post("/api/agent/callback")
        async def handler(claims: dict = Depends(verify_task_token)):
            ...

Errors map to:
  - missing token        → 401 "token required"
  - bad signature/expiry → 401 "invalid token"
  - X-Task-Id mismatch   → 403 "task_id mismatch"
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from ..services.auth_service import verify_task_token_claims


async def verify_task_token(
    x_task_token: str | None = Header(None, alias="X-Task-Token"),
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
) -> dict:
    """Default dependency — must be overridden via ``bind_verify_task_token``.

    Raises RuntimeError so misconfigured apps fail loudly.
    """
    raise RuntimeError(
        "verify_task_token is not bound — call bind_verify_task_token(app, secret)"
    )


def bind_verify_task_token(app, secret: str) -> None:
    """Install a closure that captures the Server's HMAC secret."""

    async def _bound(
        x_task_token: str | None = Header(None, alias="X-Task-Token"),
        x_task_id: str | None = Header(None, alias="X-Task-Id"),
    ) -> dict:
        if not x_task_token:
            raise HTTPException(status_code=401, detail="token required")
        try:
            claims = verify_task_token_claims(x_task_token, secret)
        except PermissionError:
            raise HTTPException(status_code=401, detail="invalid token") from None
        if x_task_id and claims.get("task_id") != x_task_id:
            raise HTTPException(status_code=403, detail="task_id mismatch")
        return claims

    app.dependency_overrides[verify_task_token] = _bound


__all__ = ["verify_task_token", "bind_verify_task_token"]
