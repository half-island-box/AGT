"""Tests for the daemon configuration API endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "daemon-config-routes-test-secret"


def _build_client(tmp_path, monkeypatch, environ=None):
    """Create a TestClient with an isolated HOME and optional environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if environ:
        for key, value in environ.items():
            monkeypatch.setenv(key, value)

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    app = create_app(cfg)
    return TestClient(app)


def test_show_returns_defaults_when_no_config_file(tmp_path, monkeypatch):
    client = _build_client(tmp_path, monkeypatch)
    resp = client.get("/api/daemon/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["config_file"] == str(tmp_path / ".agt" / "daemon.json")
    settings = data["settings"]
    assert settings["max_concurrent"]["value"] == 1
    assert settings["max_concurrent"]["source"] == "default"
    assert settings["agent_timeout"]["value"] == 600
    assert settings["work_dir"]["source"] == "default"


def test_set_persists_and_show_returns_file_source(tmp_path, monkeypatch):
    client = _build_client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/daemon/config",
        json={
            "max_concurrent": 4,
            "agent_timeout": 1800,
            "work_dir": str(tmp_path / "work"),
            "agent_cwd_allowlist": [str(tmp_path / "a"), str(tmp_path / "b")],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    settings = data["settings"]
    assert settings["max_concurrent"]["value"] == 4
    assert settings["max_concurrent"]["source"] == "file"
    assert settings["agent_timeout"]["value"] == 1800
    assert settings["work_dir"]["value"] == str(tmp_path / "work")
    assert settings["agent_cwd_allowlist"]["value"] == [
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]

    cfg_path = tmp_path / ".agt" / "daemon.json"
    assert cfg_path.exists()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["max_concurrent"] == 4


def test_env_overrides_file_in_show(tmp_path, monkeypatch):
    client = _build_client(
        tmp_path,
        monkeypatch,
        environ={"AGT_MAX_CONCURRENT": "8"},
    )
    # First persist a file value.
    client.post("/api/daemon/config", json={"max_concurrent": 4})
    resp = client.get("/api/daemon/config")
    data = resp.json()
    setting = data["settings"]["max_concurrent"]
    assert setting["value"] == 8
    assert setting["source"] == "env"
    assert setting["saved"] == 4
    assert setting["env_name"] == "AGT_MAX_CONCURRENT"


def test_set_rejects_invalid_value(tmp_path, monkeypatch):
    client = _build_client(tmp_path, monkeypatch)
    resp = client.post("/api/daemon/config", json={"max_concurrent": 0})
    assert resp.status_code == 400
    assert "positive integer" in resp.json()["detail"]


def test_set_rejects_relative_path(tmp_path, monkeypatch):
    client = _build_client(tmp_path, monkeypatch)
    resp = client.post("/api/daemon/config", json={"work_dir": "relative/path"})
    assert resp.status_code == 400
    assert "absolute" in resp.json()["detail"]


def test_set_refuses_to_overwrite_malformed_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".agt" / "daemon.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json", encoding="utf-8")
    client = _build_client(tmp_path, monkeypatch)
    resp = client.post("/api/daemon/config", json={"max_concurrent": 4})
    assert resp.status_code == 400
    assert "malformed" in resp.json()["detail"]
    assert cfg_path.read_text(encoding="utf-8") == "not json"


def test_set_no_fields_returns_400(tmp_path, monkeypatch):
    client = _build_client(tmp_path, monkeypatch)
    resp = client.post("/api/daemon/config", json={})
    assert resp.status_code == 400
    assert "at least one setting" in resp.json()["detail"]


def test_show_fails_on_malformed_config_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".agt" / "daemon.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json", encoding="utf-8")
    client = _build_client(tmp_path, monkeypatch)
    resp = client.get("/api/daemon/config")
    assert resp.status_code == 400
    assert "invalid JSON" in resp.json()["detail"]
