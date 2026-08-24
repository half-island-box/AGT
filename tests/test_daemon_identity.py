"""Phase 4 daemon identity — stable daemon_id from URL (uuid5) + daemon_name.

Tests ``agt/daemon/config.py`` identity derivation and the server-side
persistence of ``daemon_name`` on runtime rows.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from agt.daemon.config import (
    AGT_DAEMON_NAMESPACE,
    DaemonConfig,
    _find_self_daemon,
    _load_daemon_id,
)
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base


URL = "ws://203.0.113.10:8422"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".agt").mkdir(exist_ok=True)
    return tmp_path


# ── uuid5 derivation ───────────────────────────────────────────────


class TestUuid5:
    def test_deterministic(self) -> None:
        a = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))
        b = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))
        assert a == b

    def test_differs_from_name(self) -> None:
        did = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))
        assert did != "daemon-cloud-01"
        assert did != URL

    def test_changed_url_changes_id(self) -> None:
        a = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))
        b = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, "ws://9.9.9.9:8422"))
        assert a != b

    def test_load_daemon_id_uses_url(self) -> None:
        assert _load_daemon_id(URL) == str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))


# ── daemons file matching ──────────────────────────────────────────


class TestFindSelfDaemon:
    def test_single_remote_entry_is_not_self(self, home: Path) -> None:
        # The foot-gun this rewrite fixes: a machine whose daemons file
        # lists ONLY a cloud daemon must not adopt the cloud identity,
        # even though the port matches.
        (home / ".agt" / "daemons").write_text(
            "daemon-cloud-01 ws://203.0.113.10:8422\n", encoding="utf-8"
        )
        assert _find_self_daemon(8422) is None

    def test_single_local_entry_matches(self, home: Path) -> None:
        (home / ".agt" / "daemons").write_text(
            "laptop ws://127.0.0.1:8422\n", encoding="utf-8"
        )
        assert _find_self_daemon(8422) == ("laptop", "ws://127.0.0.1:8422")

    def test_cloud_and_local_same_port_prefers_local(self, home: Path) -> None:
        # Both entries on 8422 — only the local-host one is "self".
        (home / ".agt" / "daemons").write_text(
            "daemon-cloud-01 ws://203.0.113.10:8422\n"
            "local ws://0.0.0.0:8422\n",
            encoding="utf-8",
        )
        assert _find_self_daemon(8422) == ("local", "ws://0.0.0.0:8422")

    def test_multi_entry_port_match(self, home: Path) -> None:
        (home / ".agt" / "daemons").write_text(
            "daemon-a ws://2.2.2.2:8422\ndaemon-b ws://127.0.0.1:8422\n",
            encoding="utf-8",
        )
        assert _find_self_daemon(8422) == ("daemon-b", "ws://127.0.0.1:8422")

    def test_multi_entry_no_port_match(self, home: Path) -> None:
        # Local host present but the wrong port → no match (None).
        (home / ".agt" / "daemons").write_text(
            "daemon-a ws://1.1.1.1:8422\ndaemon-b ws://2.2.2.2:8422\n",
            encoding="utf-8",
        )
        assert _find_self_daemon(9999) is None

    def test_no_file(self, home: Path) -> None:
        assert _find_self_daemon(8422) is None

    def test_localhost_literal_matches(self, home: Path) -> None:
        (home / ".agt" / "daemons").write_text(
            "me ws://localhost:9000\n", encoding="utf-8"
        )
        assert _find_self_daemon(9000) == ("me", "ws://localhost:9000")


# ── transient-id fallback (no persistence) ─────────────────────────


class TestTransientId:
    def test_no_env_no_url_yields_id_without_file(self, home: Path) -> None:
        # The persistent ~/.agt/daemon_id fallback was removed; without
        # env or url we still get an id, but nothing is written to disk.
        got = _load_daemon_id(None)
        assert isinstance(got, str) and len(got) == 8
        assert not (home / ".agt" / "daemon_id").exists()

    def test_env_override(self, home: Path, monkeypatch) -> None:
        monkeypatch.setenv("AGT_DAEMON_ID", "custom-id")
        assert _load_daemon_id(URL) == "custom-id"


# ── from_env integration ───────────────────────────────────────────


class TestFromEnv:
    def test_daemon_name_and_id_from_local_entry(self, home: Path) -> None:
        # Cloud entry listed first, but identity must come from the
        # local entry (what `agt daemon start` auto-registers).
        (home / ".agt" / "daemons").write_text(
            "daemon-cloud-01 ws://203.0.113.10:8422\n"
            "local ws://0.0.0.0:8422\n",
            encoding="utf-8",
        )
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_name == "local"
        assert cfg.daemon_id == str(
            uuid.uuid5(AGT_DAEMON_NAMESPACE, "ws://0.0.0.0:8422")
        )

    def test_remote_only_file_yields_no_url_identity(
        self, home: Path, monkeypatch
    ) -> None:
        # No local entry → _find_self_daemon is None → transient uuid4 id
        # (no accidental adoption of the cloud identity).
        monkeypatch.delenv("AGT_DAEMON_ID", raising=False)
        (home / ".agt" / "daemons").write_text(
            "daemon-cloud-01 ws://203.0.113.10:8422\n", encoding="utf-8"
        )
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_id != str(uuid.uuid5(AGT_DAEMON_NAMESPACE, URL))
        assert cfg.daemon_name == "local"  # no self entry → default label

    def test_no_file_name_defaults_to_local(self, home: Path) -> None:
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_name == "local"  # local daemon default label


# ── auto-registration of the local entry ───────────────────────────


class TestEnsureLocalDaemonsEntry:
    def test_appends_when_missing(self, home: Path) -> None:
        from agt.daemon.main import _ensure_local_daemons_entry

        (home / ".agt" / "daemons").write_text(
            "daemon-cloud-01 ws://203.0.113.10:8422\n", encoding="utf-8"
        )
        assert _ensure_local_daemons_entry(8422) is True
        content = (home / ".agt" / "daemons").read_text(encoding="utf-8")
        assert content.endswith("local ws://0.0.0.0:8422\n")
        # Cloud line untouched.
        assert content.startswith("daemon-cloud-01 ws://203.0.113.10:8422\n")

    def test_idempotent_when_local_entry_exists(self, home: Path) -> None:
        from agt.daemon.main import _ensure_local_daemons_entry

        (home / ".agt" / "daemons").write_text(
            "local ws://127.0.0.1:8422\n", encoding="utf-8"
        )
        assert _ensure_local_daemons_entry(8422) is True
        assert (home / ".agt" / "daemons").read_text(
            encoding="utf-8"
        ) == "local ws://127.0.0.1:8422\n"  # nothing appended

    def test_creates_file_when_absent(self, home: Path) -> None:
        from agt.daemon.main import _ensure_local_daemons_entry

        assert _ensure_local_daemons_entry(8422) is True
        assert (home / ".agt" / "daemons").read_text(
            encoding="utf-8"
        ) == "local ws://0.0.0.0:8422\n"

    def test_readonly_file_returns_false_without_raising(
        self, home: Path, monkeypatch
    ) -> None:
        from agt.daemon.main import _ensure_local_daemons_entry

        target = home / ".agt" / "daemons"
        target.write_text("daemon-cloud-01 ws://203.0.113.10:8422\n")
        target.chmod(0o444)  # read-only for everyone incl. owner

        def _deny_append(self, *a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(
            "pathlib.Path.open", _deny_append, raising=True
        )
        assert _ensure_local_daemons_entry(8422) is False
        target.chmod(0o644)  # restore for tmp cleanup


# ── server persistence of daemon_name ──────────────────────────────


class TestServerDaemonName:
    def _build(self, tmp_path):
        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path=str(tmp_path / "s.db"), secret="x",
            sessions_json_path=str(tmp_path / "no.json"),
        )
        engine = make_engine(cfg)
        factory = make_session_factory(engine)

        async def _setup() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(ensure_schema)
                await conn.run_sync(migrate_identities_and_sessions, tmp_path)

        import asyncio
        asyncio.run(_setup())
        return cfg, engine, factory

    def test_register_persists_daemon_name(self, tmp_path) -> None:
        import asyncio

        from starlette.testclient import TestClient

        cfg, engine, _factory = self._build(tmp_path)
        client = TestClient(create_app(cfg))
        try:
            r = client.post("/api/runtimes/register", json={
                "daemon_id": "d1",
                "daemon_name": "daemon-cloud-01",
                "runtimes": [{
                    "name": "claude", "type": "claude", "version": "1.0",
                    "status": "online", "device_name": "dev",
                }],
            })
            assert r.status_code == 201

            listed = client.get("/api/runtimes").json()["runtimes"]
            assert listed[0]["daemon_name"] == "daemon-cloud-01"
            assert listed[0]["daemon_id"] == "d1"
        finally:
            asyncio.run(engine.dispose())

    def test_register_without_name_is_null(self, tmp_path) -> None:
        import asyncio

        from starlette.testclient import TestClient

        cfg, engine, _factory = self._build(tmp_path)
        client = TestClient(create_app(cfg))
        try:
            client.post("/api/runtimes/register", json={
                "daemon_id": "d-legacy",
                "runtimes": [{
                    "name": "claude", "type": "claude", "version": "1.0",
                    "status": "online", "device_name": "dev",
                }],
            })
            listed = client.get("/api/runtimes").json()["runtimes"]
            assert listed[0]["daemon_name"] is None  # legacy path
        finally:
            asyncio.run(engine.dispose())

    def test_daemon_list_aggregates_name(self, tmp_path) -> None:
        import asyncio

        from starlette.testclient import TestClient

        cfg, engine, _factory = self._build(tmp_path)
        client = TestClient(create_app(cfg))
        try:
            client.post("/api/runtimes/register", json={
                "daemon_id": "d1", "daemon_name": "daemon-cloud-01",
                "runtimes": [{"name": "claude", "type": "claude", "version": "1",
                              "status": "online", "device_name": "dev"}],
            })
            daemons = client.get("/api/daemons").json()["daemons"]
            assert daemons[0]["daemon_name"] == "daemon-cloud-01"
            assert daemons[0]["daemon_id"] == "d1"
        finally:
            asyncio.run(engine.dispose())
