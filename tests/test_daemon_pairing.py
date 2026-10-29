"""Invite codes, daemons-file identity and the loopback pairing API.

Covers the ``agt daemon invite`` / ``agt daemon init`` path end to end:
the payload round-trip, the ``id=`` field that decouples identity from
the URL spelling, the persisted daemon settings written by ``init``, and
the Server-side endpoints the settings panel calls.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from agt.daemons_file import (
    DaemonEntry,
    DaemonsFileError,
    format_daemons_entry,
    parse_daemons_line,
    read_daemons_entries,
    remove_daemons_entry,
    upsert_daemons_entry,
)
from agt.invite import InviteError, decode_invite, encode_invite


URL = "ws://1.2.3.4:8422"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGT_DAEMONS_FILE", raising=False)
    monkeypatch.delenv("AGT_SERVER_SECRET", raising=False)
    monkeypatch.delenv("AGT_DAEMON_ID", raising=False)
    monkeypatch.delenv("AGT_LISTEN_MODE", raising=False)
    monkeypatch.delenv("AGT_DAEMON_WS_PORT", raising=False)
    (tmp_path / ".agt").mkdir(exist_ok=True)
    return tmp_path


# ── invite codes ───────────────────────────────────────────────────


class TestInvite:
    def test_roundtrip(self) -> None:
        daemon_id = str(uuid.uuid4())
        code = encode_invite(
            secret="s3cret",
            url=URL,
            daemon_id=daemon_id,
            name="cloud-01",
            ws_port=8422,
        )
        payload = decode_invite(code)
        assert payload["secret"] == "s3cret"
        assert payload["url"] == URL
        assert payload["id"] == daemon_id
        assert payload["name"] == "cloud-01"
        assert payload["ws_port"] == 8422

    def test_code_is_shell_safe(self) -> None:
        code = encode_invite(secret="s3cret", url=URL, daemon_id="d")
        assert code.startswith("agt1.")
        body = code.split(".", 1)[1]
        assert set(body) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )

    def test_rejects_unknown_prefix(self) -> None:
        with pytest.raises(InviteError):
            decode_invite("agt9.abc")

    def test_rejects_bad_base64(self) -> None:
        with pytest.raises(InviteError):
            decode_invite("agt1.!!!!")

    def test_rejects_missing_fields(self) -> None:
        code = encode_invite(secret="s", url=URL, daemon_id="d")
        payload = decode_invite(code)  # sanity
        assert payload["v"] == 1

    def test_rejects_bad_url_scheme(self) -> None:
        code = encode_invite(secret="s", url="http://x", daemon_id="d")
        with pytest.raises(InviteError):
            decode_invite(code)

    def test_rejects_unsupported_version(self) -> None:
        import base64
        import json

        raw = json.dumps(
            {"v": 99, "secret": "s", "url": URL, "id": "d"}
        ).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with pytest.raises(InviteError):
            decode_invite(f"agt1.{body}")


# ── daemons file format ────────────────────────────────────────────


class TestDaemonsFileFormat:
    def test_parse_legacy_line(self) -> None:
        entry = parse_daemons_line("cloud-01 ws://1.2.3.4:8422")
        assert entry == DaemonEntry(name="cloud-01", url="ws://1.2.3.4:8422")

    def test_parse_id_and_caps(self) -> None:
        did = str(uuid.uuid4())
        entry = parse_daemons_line(f"cloud-01 {URL} id={did} containers,logs")
        assert entry is not None
        assert entry.daemon_id == did
        assert entry.capabilities == ("containers", "logs")

    def test_caps_before_id(self) -> None:
        did = str(uuid.uuid4())
        entry = parse_daemons_line(f"cloud-01 {URL} containers,logs id={did}")
        assert entry is not None
        assert entry.daemon_id == did
        assert entry.capabilities == ("containers", "logs")

    def test_non_uuid_id_is_a_capability(self) -> None:
        entry = parse_daemons_line(f"cloud-01 {URL} id=not-a-uuid")
        assert entry is not None
        assert entry.daemon_id is None
        assert entry.capabilities == ("id=not-a-uuid",)

    def test_comments_and_junk_skipped(self) -> None:
        assert parse_daemons_line("# hi") is None
        assert parse_daemons_line("") is None
        assert parse_daemons_line("only-one-column") is None

    def test_format_roundtrip(self) -> None:
        did = str(uuid.uuid4())
        entry = DaemonEntry("n", URL, did, ("a", "b"))
        assert parse_daemons_line(format_daemons_entry(entry)) == entry

    def test_format_rejects_non_uuid_id(self) -> None:
        with pytest.raises(DaemonsFileError) as exc:
            format_daemons_entry(DaemonEntry("n", URL, "not-a-uuid"))
        assert exc.value.code == "bad-id"


class TestUpsertDaemonsEntry:
    def test_add_then_unchanged(self, home: Path) -> None:
        path = home / ".agt" / "daemons"
        entry = DaemonEntry("cloud-01", URL, str(uuid.uuid4()))
        assert upsert_daemons_entry(entry) == "added"
        assert upsert_daemons_entry(entry) == "unchanged"
        assert len(read_daemons_entries(path)) == 1

    def test_preserves_comments_and_order(self, home: Path) -> None:
        path = home / ".agt" / "daemons"
        path.write_text("# keep me\nlocal ws://0.0.0.0:8422\n", encoding="utf-8")
        upsert_daemons_entry(DaemonEntry("cloud-01", URL))
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# keep me\nlocal ws://0.0.0.0:8422\n")

    def test_duplicate_url_with_other_name_refused(self, home: Path) -> None:
        upsert_daemons_entry(DaemonEntry("a", URL))
        with pytest.raises(DaemonsFileError) as exc:
            upsert_daemons_entry(DaemonEntry("b", URL))
        assert exc.value.code == "duplicate-url"

    def test_name_conflict_refused(self, home: Path) -> None:
        upsert_daemons_entry(DaemonEntry("a", URL))
        with pytest.raises(DaemonsFileError) as exc:
            upsert_daemons_entry(DaemonEntry("a", "ws://5.6.7.8:8422"))
        assert exc.value.code == "name-conflict"

    def test_replace_name_allowed(self, home: Path) -> None:
        upsert_daemons_entry(DaemonEntry("a", URL))
        upsert_daemons_entry(
            DaemonEntry("a", "ws://5.6.7.8:8422"), replace_name=True
        )
        entries = read_daemons_entries()
        assert [e.url for e in entries] == ["ws://5.6.7.8:8422"]


class TestRemoveDaemonsEntry:
    def _write(self, home: Path, text: str) -> Path:
        path = home / ".agt" / "daemons"
        path.write_text(text, encoding="utf-8")
        return path

    def test_remove_by_name_preserves_other_lines(self, home: Path) -> None:
        path = self._write(
            home,
            f"# keep me\ncloud-01 {URL}\ncloud-02 ws://5.6.7.8:8422\n",
        )
        removed = remove_daemons_entry(name="cloud-01")
        assert removed.name == "cloud-01"
        assert path.read_text(encoding="utf-8") == (
            "# keep me\ncloud-02 ws://5.6.7.8:8422\n"
        )

    def test_remove_by_url(self, home: Path) -> None:
        did = str(uuid.uuid4())
        path = self._write(home, f"cloud-01 {URL} id={did}\n")
        removed = remove_daemons_entry(url=URL)
        assert removed.daemon_id == did
        assert path.read_text(encoding="utf-8") == ""

    def test_remove_by_daemon_id(self, home: Path) -> None:
        did = str(uuid.uuid4())
        self._write(home, f"cloud-01 {URL} id={did}\n")
        assert remove_daemons_entry(daemon_id=did).name == "cloud-01"
        assert read_daemons_entries() == []

    def test_requires_selector(self, home: Path) -> None:
        path = self._write(home, f"cloud-01 {URL}\n")
        with pytest.raises(DaemonsFileError) as exc:
            remove_daemons_entry()
        assert exc.value.code == "bad-selector"
        assert path.read_text(encoding="utf-8") == f"cloud-01 {URL}\n"

    def test_unknown_target(self, home: Path) -> None:
        self._write(home, f"cloud-01 {URL}\n")
        with pytest.raises(DaemonsFileError) as exc:
            remove_daemons_entry(name="nope")
        assert exc.value.code == "not-found"

    def test_missing_file(self, home: Path) -> None:
        with pytest.raises(DaemonsFileError) as exc:
            remove_daemons_entry(name="cloud-01")
        assert exc.value.code == "not-found"


# ── identity precedence ────────────────────────────────────────────


class TestDaemonIdPrecedence:
    def test_invite_id_wins_over_url(self, home: Path) -> None:
        from agt.daemon.config import DaemonConfig

        did = str(uuid.uuid4())
        (home / ".agt" / "daemon.json").write_text(
            f'{{"version": 1, "daemon_id": "{did}", '
            f'"daemon_url": "{URL}", "listen_mode": true, "ws_port": 8422}}',
            encoding="utf-8",
        )
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_id == did
        assert cfg.daemon_url == URL
        assert cfg.listen_mode is True

    def test_env_beats_file(self, home: Path, monkeypatch) -> None:
        from agt.daemon.config import DaemonConfig

        (home / ".agt" / "daemon.json").write_text(
            '{"version": 1, "daemon_id": "from-file"}', encoding="utf-8"
        )
        monkeypatch.setenv("AGT_DAEMON_ID", "from-env")
        assert DaemonConfig.from_env().daemon_id == "from-env"

    def test_daemons_file_id_used_without_json(self, home: Path) -> None:
        from agt.daemon.config import DaemonConfig

        did = str(uuid.uuid4())
        (home / ".agt" / "daemons").write_text(
            f"local ws://127.0.0.1:8422 id={did}\n", encoding="utf-8"
        )
        assert DaemonConfig.from_env().daemon_id == did

    def test_legacy_uuid5_fallback(self, home: Path) -> None:
        from agt.daemon.config import DaemonConfig, AGT_DAEMON_NAMESPACE

        (home / ".agt" / "daemons").write_text(
            "local ws://127.0.0.1:8422\n", encoding="utf-8"
        )
        expected = str(uuid.uuid5(AGT_DAEMON_NAMESPACE, "ws://127.0.0.1:8422"))
        assert DaemonConfig.from_env().daemon_id == expected


# ── server secret bootstrap ────────────────────────────────────────


class TestEnsureServerSecret:
    def test_creates_file(self, home: Path) -> None:
        from agt.server_secret import default_server_secret_path, ensure_server_secret

        secret = ensure_server_secret({})
        path = default_server_secret_path()
        assert path.read_text(encoding="utf-8") == secret
        assert path.stat().st_mode & 0o777 == 0o600

    def test_env_does_not_touch_disk(self, home: Path) -> None:
        from agt.server_secret import default_server_secret_path, ensure_server_secret

        assert ensure_server_secret({"AGT_SERVER_SECRET": "from-env"}) == "from-env"
        assert not default_server_secret_path().exists()

    def test_existing_file_reused(self, home: Path) -> None:
        from agt.server_secret import default_server_secret_path, ensure_server_secret

        path = default_server_secret_path()
        path.write_text("already-here", encoding="utf-8")
        assert ensure_server_secret({}) == "already-here"


# ── CLI: daemon init ───────────────────────────────────────────────


class TestDaemonInitCli:
    def _invoke(self, args):
        from agt.cli.main import cli

        return CliRunner().invoke(cli, ["daemon", "init", *args])

    def test_init_from_invite_writes_everything(self, home: Path) -> None:
        did = str(uuid.uuid4())
        code = encode_invite(
            secret="shared-secret",
            url=URL,
            daemon_id=did,
            name="cloud-01",
            ws_port=8422,
        )
        result = self._invoke([code])
        assert result.exit_code == 0, result.output

        secret = (home / ".agt" / "server.secret").read_text(encoding="utf-8")
        assert secret == "shared-secret"
        assert (home / ".agt" / "server.secret").stat().st_mode & 0o777 == 0o600

        import json

        data = json.loads((home / ".agt" / "daemon.json").read_text(encoding="utf-8"))
        assert data["daemon_id"] == did
        assert data["daemon_url"] == URL
        assert data["listen_mode"] is True
        assert data["ws_port"] == 8422

        entries = read_daemons_entries()
        assert entries[0].name == "cloud-01"
        assert entries[0].daemon_id == did

    def test_init_refuses_different_secret_without_force(self, home: Path) -> None:
        (home / ".agt" / "server.secret").write_text("old", encoding="utf-8")
        code = encode_invite(secret="new", url=URL, daemon_id=str(uuid.uuid4()))
        result = self._invoke([code])
        assert result.exit_code == 1
        assert "different server secret" in result.output
        assert (home / ".agt" / "server.secret").read_text(encoding="utf-8") == "old"

    def test_init_force_overwrites(self, home: Path) -> None:
        (home / ".agt" / "server.secret").write_text("old", encoding="utf-8")
        code = encode_invite(secret="new", url=URL, daemon_id=str(uuid.uuid4()))
        result = self._invoke([code, "--force"])
        assert result.exit_code == 0, result.output
        assert (home / ".agt" / "server.secret").read_text(encoding="utf-8") == "new"

    def test_init_rejects_non_uuid_daemon_id(self, home: Path) -> None:
        result = self._invoke(
            ["--token", "tok", "--url", URL, "--daemon-id", "not-a-uuid"]
        )
        assert result.exit_code == 2
        assert "must be a UUID" in result.output
        assert not (home / ".agt" / "daemons").exists()

    def test_init_requires_token_without_invite(self, home: Path) -> None:
        result = self._invoke(["--url", URL])
        assert result.exit_code == 2
        assert "--token is required" in result.output

    def test_explicit_token_and_url(self, home: Path) -> None:
        result = self._invoke(["--token", "tok", "--url", URL, "--name", "me"])
        assert result.exit_code == 0, result.output
        from agt.ids import daemon_id_from_url

        import json

        data = json.loads((home / ".agt" / "daemon.json").read_text(encoding="utf-8"))
        assert data["daemon_id"] == daemon_id_from_url(URL)

    def test_paired_entry_wins_over_stale_local_entry(self, home: Path) -> None:
        """A leftover ``local ws://0.0.0.0:8422`` must not shadow the pairing."""
        from agt.daemon.config import DaemonConfig

        (home / ".agt" / "daemons").write_text(
            "local ws://0.0.0.0:8422\n", encoding="utf-8"
        )
        did = str(uuid.uuid4())
        code = encode_invite(
            secret="shared", url=URL, daemon_id=did, name="cloud-01", ws_port=8422
        )
        assert self._invoke([code]).exit_code == 0

        cfg = DaemonConfig.from_env()
        assert cfg.daemon_id == did
        assert cfg.daemon_name == "cloud-01"
        assert cfg.daemon_url == URL
        assert cfg.listen_mode is True
        assert cfg.ws_listen_port == 8422


class TestDaemonRemoveCli:
    def _invoke(self, args):
        from agt.cli.main import cli

        return CliRunner().invoke(cli, ["daemon", "remove", *args])

    def test_remove_by_name(self, home: Path) -> None:
        path = home / ".agt" / "daemons"
        path.write_text(
            f"cloud-01 {URL}\ncloud-02 ws://5.6.7.8:8422\n", encoding="utf-8"
        )
        result = self._invoke(["cloud-01"])
        assert result.exit_code == 0, result.output
        assert path.read_text(encoding="utf-8") == "cloud-02 ws://5.6.7.8:8422\n"
        assert "Removed cloud-01" in result.output

    def test_remove_by_url(self, home: Path) -> None:
        path = home / ".agt" / "daemons"
        path.write_text(f"cloud-01 {URL}\n", encoding="utf-8")
        result = self._invoke([URL])
        assert result.exit_code == 0, result.output
        assert read_daemons_entries() == []

    def test_unknown_target_exits_1(self, home: Path) -> None:
        path = home / ".agt" / "daemons"
        path.write_text(f"cloud-01 {URL}\n", encoding="utf-8")
        result = self._invoke(["nope"])
        assert result.exit_code == 1
        assert "no entry matching" in result.output
        assert path.read_text(encoding="utf-8") == f"cloud-01 {URL}\n"


# ── pairing API ────────────────────────────────────────────────────


def _build_client(tmp_path, daemons_text: str = "", peer=("127.0.0.1", 51234)):
    from starlette.testclient import TestClient

    from agt.server.config import ServerConfig
    from agt.server.main import create_app
    from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
    from agt.server.models import Base
    from agt.server.database import make_engine, make_session_factory

    daemons_path = tmp_path / "daemons"
    if daemons_text:
        daemons_path.write_text(daemons_text, encoding="utf-8")
    from agt.server.config import _parse_daemons_file

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "s.db"),
        secret="server-secret",
        sessions_json_path=str(tmp_path / "no.json"),
        daemons_path=str(daemons_path),
        daemons=_parse_daemons_file(str(daemons_path)),
    )
    engine = make_engine(cfg)
    make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
            await conn.run_sync(migrate_identities_and_sessions, tmp_path)

    asyncio.run(_setup())
    return TestClient(create_app(cfg), client=peer), engine, daemons_path


class TestPairingRoutes:
    def test_loopback_guard(self) -> None:
        from agt.server.routes.pairing import _is_loopback_address

        assert _is_loopback_address("127.0.0.1")
        assert _is_loopback_address("::1")
        assert _is_loopback_address("localhost")
        assert not _is_loopback_address("10.0.0.5")
        assert not _is_loopback_address("")

    def test_remote_peer_refused_even_with_console_remote(self, tmp_path) -> None:
        client, engine, _ = _build_client(tmp_path, peer=("10.0.0.5", 51234))
        try:
            for path in ("/api/pairing/state", "/api/pairing/secret"):
                r = client.get(path)
                assert r.status_code == 403, path
        finally:
            asyncio.run(engine.dispose())

    def test_secret_masked_by_default(self, tmp_path) -> None:
        client, engine, _ = _build_client(tmp_path)
        try:
            body = client.get("/api/pairing/secret").json()
            assert body["secret"] is None
            assert "…" in body["masked"]

            revealed = client.get("/api/pairing/secret?reveal=true").json()
            assert revealed["secret"] == "server-secret"
        finally:
            asyncio.run(engine.dispose())

    def test_add_daemon_returns_invite(self, tmp_path) -> None:
        client, engine, daemons_path = _build_client(tmp_path)
        try:
            r = client.post(
                "/api/pairing/daemons",
                json={"name": "cloud-01", "url": URL},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["restart_required"] is True
            payload = decode_invite(body["invite_code"])
            assert payload["id"] == body["daemon_id"]
            assert payload["secret"] == "server-secret"
            assert "id=" in daemons_path.read_text(encoding="utf-8")
        finally:
            asyncio.run(engine.dispose())

    def test_add_duplicate_url_conflicts(self, tmp_path) -> None:
        client, engine, _ = _build_client(tmp_path)
        try:
            client.post("/api/pairing/daemons", json={"name": "a", "url": URL})
            r = client.post("/api/pairing/daemons", json={"name": "b", "url": URL})
            assert r.status_code == 409
        finally:
            asyncio.run(engine.dispose())

    def test_invite_for_unregistered_target_is_409(self, tmp_path) -> None:
        client, engine, _ = _build_client(tmp_path)
        try:
            r = client.get(
                "/api/pairing/invite", params={"name": "x", "url": URL}
            )
            assert r.status_code == 409
        finally:
            asyncio.run(engine.dispose())

    def test_state_uses_explicit_id(self, tmp_path) -> None:
        did = str(uuid.uuid4())
        client, engine, _ = _build_client(
            tmp_path, daemons_text=f"cloud-01 {URL} id={did}\n"
        )
        try:
            body = client.get("/api/pairing/state").json()
            assert body["daemons"][0]["daemon_id"] == did
            assert body["secret_configured"] is True
        finally:
            asyncio.run(engine.dispose())

    def test_remove_entry_stops_live_link(self, tmp_path) -> None:
        did = str(uuid.uuid4())
        client, engine, daemons_path = _build_client(
            tmp_path,
            daemons_text=(
                f"cloud-01 {URL} id={did}\ncloud-02 ws://5.6.7.8:8422\n"
            ),
        )

        class _FakeLink:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        link = _FakeLink()
        client.app.state.daemon_links = {did: link}
        try:
            r = client.delete(
                "/api/pairing/daemons", params={"name": "cloud-01"}
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["removed"] is True
            assert body["daemon_id"] == did
            assert body["disconnected"] is True
            assert body["restart_required"] is True
            assert link.stopped is True
            assert did not in client.app.state.daemon_links
            assert daemons_path.read_text(encoding="utf-8") == (
                "cloud-02 ws://5.6.7.8:8422\n"
            )
        finally:
            asyncio.run(engine.dispose())

    def test_remove_unknown_entry_is_404(self, tmp_path) -> None:
        client, engine, daemons_path = _build_client(
            tmp_path, daemons_text=f"cloud-01 {URL}\n"
        )
        try:
            r = client.delete(
                "/api/pairing/daemons", params={"name": "nope"}
            )
            assert r.status_code == 404
            assert daemons_path.read_text(encoding="utf-8") == f"cloud-01 {URL}\n"
        finally:
            asyncio.run(engine.dispose())

    def test_remove_without_selector_is_400(self, tmp_path) -> None:
        client, engine, _ = _build_client(
            tmp_path, daemons_text=f"cloud-01 {URL}\n"
        )
        try:
            assert client.delete("/api/pairing/daemons").status_code == 400
        finally:
            asyncio.run(engine.dispose())

    def test_remote_peer_cannot_remove(self, tmp_path) -> None:
        client, engine, daemons_path = _build_client(
            tmp_path,
            daemons_text=f"cloud-01 {URL}\n",
            peer=("10.0.0.5", 51234),
        )
        try:
            r = client.delete(
                "/api/pairing/daemons", params={"name": "cloud-01"}
            )
            assert r.status_code == 403
            assert daemons_path.read_text(encoding="utf-8") == f"cloud-01 {URL}\n"
        finally:
            asyncio.run(engine.dispose())

