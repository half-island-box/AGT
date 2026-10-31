"""Tests for the browser-console bundle helpers (``agt/web_console/build.py``)."""

from __future__ import annotations

import json
from pathlib import Path

from agt.web_console.build import (
    _lock_path,
    bundle_names,
    console_source_dir,
    ensure_console_assets,
    missing_bundles,
)


def _write_bundles(assets: Path) -> None:
    assets.mkdir(parents=True, exist_ok=True)
    for name in bundle_names():
        (assets / name).write_text("// bundle\n", encoding="utf-8")


def _source_tree(root: Path) -> Path:
    """A minimal checkout: ``root/vscode/package.json`` with the build script."""
    workspace = root / "vscode"
    workspace.mkdir(parents=True)
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"build": "node esbuild.js", "build:console": "node esbuild.js --console"}}),
        encoding="utf-8",
    )
    return workspace


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_missing_bundles_reports_every_page_when_empty(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    assert missing_bundles(assets) == bundle_names()


def test_missing_bundles_is_empty_when_all_present(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_bundles(assets)
    assert missing_bundles(assets) == []


def test_missing_bundles_lists_only_absent_files(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_bundles(assets)
    (assets / "webview-console-chat.js").unlink()
    assert missing_bundles(assets) == ["webview-console-chat.js"]


def test_console_source_dir_finds_vscode_workspace(tmp_path: Path) -> None:
    workspace = _source_tree(tmp_path)
    nested = tmp_path / "agt" / "web_console"
    nested.mkdir(parents=True)
    assert console_source_dir(nested) == workspace


def test_console_source_dir_ignores_package_without_script(tmp_path: Path) -> None:
    workspace = tmp_path / "vscode"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"build": "node esbuild.js"}}), encoding="utf-8"
    )
    assert console_source_dir(tmp_path) is None


def test_console_source_dir_honours_env_override(tmp_path: Path, monkeypatch) -> None:
    workspace = _source_tree(tmp_path / "elsewhere")
    monkeypatch.setenv("AGT_CONSOLE_SOURCE_DIR", str(workspace))
    # Even a tree with no vscode/ falls back to the explicit override.
    other = tmp_path / "no-sources"
    other.mkdir()
    assert console_source_dir(other) == workspace


def test_ensure_returns_present_without_running_npm(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _write_bundles(assets)
    calls: list[list[str]] = []

    def runner(*args, **kwargs):  # pragma: no cover - must not run
        calls.append(list(args[0]))
        return _Completed()

    result = ensure_console_assets(assets, runner=runner, npm="npm")
    assert result.status == "present"
    assert calls == []


def test_ensure_builds_when_source_and_npm_are_available(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        _write_bundles(assets)
        return _Completed(stdout="built 5 bundles")

    result = ensure_console_assets(assets, source_dir=workspace, npm="npm", runner=runner)
    assert result.status == "built"
    assert seen["command"] == ["npm", "run", "build:console"]
    assert seen["cwd"] == str(workspace)
    assert missing_bundles(assets) == []


def test_ensure_reports_failed_build_with_output_tail(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)

    def runner(command, **kwargs):
        return _Completed(returncode=1, stderr="esbuild: cannot find entry point")

    result = ensure_console_assets(assets, source_dir=workspace, npm="npm", runner=runner)
    assert result.status == "build-failed"
    assert "cannot find entry point" in result.detail


def test_ensure_reports_failed_build_when_bundles_still_missing(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)

    def runner(command, **kwargs):
        return _Completed(returncode=0, stdout="nothing to do")

    result = ensure_console_assets(assets, source_dir=workspace, npm="npm", runner=runner)
    assert result.status == "build-failed"
    assert "still missing" in result.detail


def test_ensure_respects_autobuild_opt_out(tmp_path: Path, monkeypatch) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)
    monkeypatch.setenv("AGT_CONSOLE_AUTOBUILD", "0")
    result = ensure_console_assets(assets, source_dir=workspace, npm="npm")
    assert result.status == "disabled"


def test_ensure_without_npm_or_sources(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)
    assert ensure_console_assets(assets, source_dir=workspace, npm="").status == "no-npm"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert ensure_console_assets(assets, source_dir=empty, npm="npm").status == "no-source"


def test_ensure_reports_locked_when_another_build_holds_the_lock(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    workspace = _source_tree(tmp_path)
    lock = _lock_path(assets)
    lock.write_text("12345", encoding="utf-8")
    try:
        result = ensure_console_assets(assets, source_dir=workspace, npm="npm")
        assert result.status == "locked"
    finally:
        lock.unlink(missing_ok=True)
