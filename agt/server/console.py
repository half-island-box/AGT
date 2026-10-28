"""Browser console hosting and its narrow workspace-file APIs."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .config import ServerConfig

router = APIRouter(tags=["console"])

_PAGES = {"chat", "settings", "sop", "goal", "projects"}
_PROJECT_PAGES = {"chat", "sop", "goal"}
_MAX_SOP_BYTES = 2 * 1024 * 1024
_SOP_NAME_RE = re.compile(r"^[^\0]+\.ya?ml$", re.IGNORECASE)
_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}


class SopFileWriteRequest(BaseModel):
    path: str
    text: str


def _config(request: Request) -> ServerConfig:
    return request.app.state.config


def _assets_dir(config: ServerConfig) -> Path:
    return Path(config.console_assets_dir).expanduser().resolve()


def _allowed_roots(config: ServerConfig) -> list[Path]:
    raw = config.console_workspace_roots or [str(Path.home())]
    return [Path(item).expanduser().resolve() for item in raw]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_allowed_path(config: ServerConfig, raw: str) -> Path:
    try:
        path = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from None
    if not any(_is_relative_to(path, root) for root in _allowed_roots(config)):
        raise HTTPException(status_code=403, detail="path is outside console workspace roots")
    return path


def _console_html(page: str, config: ServerConfig) -> str:
    assets = _assets_dir(config)
    script = assets / f"webview-console-{page}.js"
    css = assets / f"webview-console-{page}.css"
    if not script.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="web console assets are missing; run `npm run build:console`",
        )
    css_link = (
        f'<link rel="stylesheet" href="/console/assets/{css.name}">'
        if css.exists()
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark light">
<title>AGT Console</title>
{css_link}
</head>
<body>
<div id="agt-root"></div>
<script src="/console/assets/{script.name}"></script>
</body>
</html>"""


def _security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _page_response(page: str, request: Request) -> HTMLResponse:
    response = HTMLResponse(
        _console_html(page, _config(request)),
        headers={"Cache-Control": "no-store"},
    )
    return _security_headers(response)


@router.get("/", include_in_schema=False)
async def root(request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse("/console/", status_code=status.HTTP_302_FOUND)


@router.get("/console", include_in_schema=False)
async def console_no_slash(request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse("/console/", status_code=status.HTTP_302_FOUND)


@router.get("/console/", include_in_schema=False)
async def console_index(request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse("/console/projects", status_code=status.HTTP_302_FOUND)


@router.get("/console/projects", include_in_schema=False)
async def console_projects_page(request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return _page_response("projects", request)


@router.get("/console/projects/{project_id}", include_in_schema=False)
async def console_project_page(project_id: str, request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(
        f"/console/projects/{project_id}/chat",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/console/projects/{project_id}/{page}", include_in_schema=False)
async def console_project_mode_page(project_id: str, page: str, request: Request):
    if not _config(request).console_enabled or page not in _PROJECT_PAGES:
        raise HTTPException(status_code=404, detail="Not Found")
    return _page_response(page, request)


@router.get("/console/{page}", include_in_schema=False)
async def console_page(page: str, request: Request):
    if not _config(request).console_enabled or page not in _PAGES:
        raise HTTPException(status_code=404, detail="Not Found")
    if page in _PROJECT_PAGES:
        return RedirectResponse("/console/projects", status_code=status.HTTP_302_FOUND)
    return _page_response(page, request)


@router.get("/console/assets/{filename}", include_in_schema=False)
async def console_asset(filename: str, request: Request):
    if not _config(request).console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="Not Found")
    target = _assets_dir(_config(request)) / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    response = FileResponse(target)
    response.headers["Cache-Control"] = "no-cache"
    return _security_headers(response)


@router.get("/api/console/bootstrap")
async def console_bootstrap(request: Request) -> dict:
    config = _config(request)
    if not config.console_enabled:
        raise HTTPException(status_code=404, detail="console disabled")
    roots = _allowed_roots(config)
    cwd = Path.cwd().resolve()
    default_project = str(cwd) if any(_is_relative_to(cwd, root) for root in roots) else str(roots[0])
    return {
        "console_url": "/console/",
        "server_version": request.app.version,
        "default_project": default_project,
        "workspace_roots": [str(root) for root in roots],
        "capabilities": {
            "lifecycle_control": True,
            "daemon_config": True,
            "identity_management": True,
            "sop_files": True,
        },
    }


@router.get("/api/console/sop/templates")
async def sop_templates(
    request: Request,
    root: str = Query(...),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    config = _config(request)
    root_path = _resolve_allowed_path(config, root)
    if not root_path.is_dir():
        raise HTTPException(status_code=400, detail="template root must be a directory")
    found: list[dict] = []
    for path in root_path.rglob("*.y*ml"):
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root_path).parts):
            continue
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_SOP_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        looks_like_sop = path.name.lower().endswith((".sop.yaml", ".sop.yml"))
        if not looks_like_sop:
            looks_like_sop = bool(
                re.search(r"(?m)^name\s*:", text)
                and re.search(r"(?m)^tasks\s*:", text)
            )
        if not looks_like_sop:
            continue
        found.append(
            {
                "path": str(path.resolve()),
                "label": path.name,
                "nodeCount": max(0, len(re.findall(r"(?m)^\s*-\s+name\s*:", text))),
            }
        )
        if len(found) >= limit:
            break
    found.sort(key=lambda item: item["label"].lower())
    return {"templates": found}


@router.get("/api/console/sop/file")
async def read_sop_file(request: Request, path: str = Query(...)) -> dict:
    resolved = _resolve_allowed_path(_config(request), path)
    if not _SOP_NAME_RE.match(resolved.name):
        raise HTTPException(status_code=400, detail="only YAML files are supported")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="SOP file not found")
    if resolved.stat().st_size > _MAX_SOP_BYTES:
        raise HTTPException(status_code=413, detail="SOP file is too large")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="SOP file must be UTF-8") from None
    return {"path": str(resolved), "text": text}


@router.put("/api/console/sop/file")
async def write_sop_file(payload: SopFileWriteRequest, request: Request) -> dict:
    resolved = _resolve_allowed_path(_config(request), payload.path)
    if not _SOP_NAME_RE.match(resolved.name):
        raise HTTPException(status_code=400, detail="only YAML files are supported")
    data = payload.text.encode("utf-8")
    if len(data) > _MAX_SOP_BYTES:
        raise HTTPException(status_code=413, detail="SOP file is too large")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        with NamedTemporaryFile(
            "wb",
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            delete=False,
        ) as temp:
            temp.write(data)
            temp_path = Path(temp.name)
        os.replace(temp_path, resolved)
    finally:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


__all__ = ["router"]
