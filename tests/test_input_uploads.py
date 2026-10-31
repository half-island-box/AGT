"""Chat attachments — user uploads stored by the Server.

Covers the four seams introduced by ``agt-upload://`` references:

1. URI helpers + filename sanitization (Unicode survives, paths don't).
2. ``input_service``: inline vs disk, sha256 dedupe, size cap, TTL sweep.
3. HTTP routes: create / metadata / content / delete.
4. Claim-time hints + daemon-side rendering (inline materialization and
   on-demand resolution by full URI).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from agt.a2a import (
    INLINE_MAX_BYTES,
    make_upload_uri,
    parse_upload_uri,
)
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, InputUploadModel
from agt.server.services.input_service import (
    create_upload,
    get_upload,
    read_upload_content,
    sanitize_upload_name,
    sweep_expired_uploads,
    uploads_dir_for,
)
from agt.task.message_render import render_task_message

SECRET = "upload-test-secret"


def _utc(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── URI helpers ─────────────────────────────────────────────────────


def test_upload_uri_roundtrip() -> None:
    upload_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    uri = make_upload_uri(upload_id)
    assert uri == f"agt-upload://{upload_id}"
    assert parse_upload_uri(uri) == upload_id


def test_upload_uri_rejects_other_namespaces() -> None:
    assert parse_upload_uri("agt-artifact://0f8fad5b-d9cb-469f-a165-70867728950e") is None
    assert parse_upload_uri("file:///etc/passwd") is None
    assert parse_upload_uri("agt-upload://../../etc/passwd") is None


def test_sanitize_upload_name_keeps_unicode_drops_paths() -> None:
    assert sanitize_upload_name("../../需求 报告.md") == "需求 报告.md"
    assert sanitize_upload_name("C:\\tmp\\a.txt") == "a.txt"
    assert sanitize_upload_name("bad\x00name.txt") == "bad_name.txt"
    assert sanitize_upload_name("") == "upload.bin"


# ── Service ─────────────────────────────────────────────────────────


def _factory_and_cfg(tmp_path: Path):
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "uploads.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    return engine, factory, cfg


def test_small_upload_is_inline_and_dedupes(tmp_path) -> None:
    engine, factory, cfg = _factory_and_cfg(tmp_path)

    async def _run():
        async with session_scope(factory) as db:
            first = await create_upload(
                db, name="note.txt", content=b"hello", db_path=cfg.db_path
            )
            await db.commit()
            second = await create_upload(
                db, name="renamed.txt", content=b"hello", db_path=cfg.db_path
            )
            await db.commit()
            assert second.upload_id == first.upload_id
            assert first.inline_bytes == b"hello"
            assert first.storage_path is None
            assert first.sha256 == hashlib.sha256(b"hello").hexdigest()
            assert await read_upload_content(second) == b"hello"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_large_upload_goes_to_disk(tmp_path) -> None:
    engine, factory, cfg = _factory_and_cfg(tmp_path)
    content = b"x" * (INLINE_MAX_BYTES + 1)

    async def _run():
        async with session_scope(factory) as db:
            row = await create_upload(
                db, name="big.bin", content=content, db_path=cfg.db_path
            )
            await db.commit()
            assert row.inline_bytes is None
            assert row.storage_path is not None
            assert Path(row.storage_path).parent == uploads_dir_for(cfg.db_path)
            assert await read_upload_content(row) == content

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_upload_over_limit_is_rejected(tmp_path) -> None:
    engine, factory, cfg = _factory_and_cfg(tmp_path)

    async def _run():
        async with session_scope(factory) as db:
            try:
                await create_upload(
                    db, name="big.bin", content=b"x" * 10,
                    db_path=cfg.db_path, max_bytes=5,
                )
            except ValueError as exc:
                assert exc.args[0]["type"] == "upload_too_large"
            else:  # pragma: no cover
                raise AssertionError("expected ValueError")

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_sweep_removes_expired_rows_and_blobs(tmp_path) -> None:
    engine, factory, cfg = _factory_and_cfg(tmp_path)
    content = b"y" * (INLINE_MAX_BYTES + 1)

    async def _run():
        async with session_scope(factory) as db:
            row = await create_upload(
                db, name="old.bin", content=content, db_path=cfg.db_path
            )
            await db.commit()
            blob = Path(row.storage_path)
            row.expires_at = _utc(-1)
            await db.commit()
            assert await sweep_expired_uploads(db) == 1
            await db.commit()
            assert await get_upload(db, row.upload_id) is None
            assert not blob.exists()

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


# ── HTTP routes ─────────────────────────────────────────────────────


def _build_app(tmp_path: Path) -> tuple[TestClient, object, ServerConfig]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "routes.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    return TestClient(create_app(cfg)), engine, cfg


def test_route_upload_roundtrip(tmp_path) -> None:
    client, engine, _cfg = _build_app(tmp_path)
    try:
        body = base64.b64encode("你好".encode()).decode()
        created = client.post("/api/uploads", json={
            "name": "笔记.txt", "mime_type": "text/plain",
            "content_b64": body, "source": "console",
        })
        assert created.status_code == 201
        data = created.json()
        assert data["uri"].startswith("agt-upload://")
        assert data["name"] == "笔记.txt"

        # Duplicate content reuses the same row.
        again = client.post("/api/uploads", json={
            "name": "copy.txt", "content_b64": body,
        })
        assert again.status_code == 201
        assert again.json()["upload_id"] == data["upload_id"]

        meta = client.get(f"/api/uploads/{data['upload_id']}")
        assert meta.status_code == 200

        content = client.get(f"/api/uploads/{data['upload_id']}/content")
        assert content.status_code == 200
        assert content.content == "你好".encode()
        assert content.headers["x-content-type-options"] == "nosniff"
        content.headers["content-disposition"].encode("latin-1")

        deleted = client.delete(f"/api/uploads/{data['upload_id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/uploads/{data['upload_id']}").status_code == 404
    finally:
        asyncio.run(engine.dispose())


def test_route_rejects_bad_base64_and_oversize(tmp_path) -> None:
    client, engine, cfg = _build_app(tmp_path)
    cfg.max_upload_bytes = 5
    try:
        bad = client.post("/api/uploads", json={"name": "x", "content_b64": "!!!"})
        assert bad.status_code == 400

        big = client.post("/api/uploads", json={
            "name": "big.bin",
            "content_b64": base64.b64encode(b"123456").decode(),
        })
        assert big.status_code == 413
    finally:
        asyncio.run(engine.dispose())


# ── Claim hints ─────────────────────────────────────────────────────


def _register(client: TestClient, daemon_id: str, provider: str = "claude") -> None:
    resp = client.post("/api/runtimes/register", json={
        "daemon_id": daemon_id,
        "runtimes": [{
            "name": provider, "type": provider, "version": "1.0",
            "status": "online", "device_name": "test-device",
        }],
    })
    assert resp.status_code == 201


def _create_chat_job(client: TestClient, upload_id: str, name: str) -> str:
    resp = client.post("/api/jobs", json={
        "prompt": "look at this",
        "provider": "claude",
        "target_daemon_id": "d1",
        "message": {
            "role": "user",
            "parts": [
                {"type": "text", "text": "look at this"},
                {"type": "file", "uri": make_upload_uri(upload_id), "name": name},
            ],
        },
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


def _claim(client: TestClient) -> dict:
    resp = client.post("/api/tasks/claim", json={
        "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 1,
        "runtime_ids": ["claude"],
    })
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    return tasks[0]


def test_claim_inlines_small_upload(tmp_path) -> None:
    client, engine, _cfg = _build_app(tmp_path)
    try:
        _register(client, "d1")
        created = client.post("/api/uploads", json={
            "name": "note.txt",
            "content_b64": base64.b64encode(b"hello upload").decode(),
        }).json()
        _create_chat_job(client, created["upload_id"], "note.txt")

        part = _claim(client)["message"]["parts"][1]
        assert part["uri"] == created["uri"]
        assert part["name"] == "note.txt"
        assert base64.b64decode(part["inline_b64"]) == b"hello upload"
        assert "local_path" not in part
    finally:
        asyncio.run(engine.dispose())


def test_claim_leaves_large_upload_uri_only(tmp_path) -> None:
    client, engine, _cfg = _build_app(tmp_path)
    try:
        _register(client, "d1")
        created = client.post("/api/uploads", json={
            "name": "big.bin",
            "content_b64": base64.b64encode(b"z" * (INLINE_MAX_BYTES + 1)).decode(),
        }).json()
        _create_chat_job(client, created["upload_id"], "big.bin")

        part = _claim(client)["message"]["parts"][1]
        assert "inline_b64" not in part
        assert "local_path" not in part
        assert part["name"] == "big.bin"
    finally:
        asyncio.run(engine.dispose())


# ── Daemon-side rendering ───────────────────────────────────────────


def test_render_materializes_inline_upload_under_original_name(tmp_path) -> None:
    message = {
        "parts": [
            {"type": "text", "text": "read it"},
            {
                "type": "file",
                "uri": make_upload_uri("0f8fad5b-d9cb-469f-a165-70867728950e"),
                "name": "需求 报告.md",
                "mime_type": "text/markdown",
                "inline_b64": base64.b64encode(b"# hi").decode(),
            },
        ],
    }
    prompt = asyncio.run(render_task_message(message, agent_cwd=tmp_path))
    target = tmp_path / ".agt" / "inputs" / "需求 报告.md"
    assert target.read_bytes() == b"# hi"
    assert str(target) in prompt
    assert prompt.startswith("read it")


def test_render_resolves_upload_by_full_uri(tmp_path) -> None:
    upload_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    seen: list[str] = []

    async def _resolver(uri: str):
        seen.append(uri)
        return {"content_b64": base64.b64encode(b"remote").decode(),
                "name": "remote.txt", "mime_type": "text/plain"}

    message = {"parts": [{
        "type": "file", "uri": make_upload_uri(upload_id),
        "name": "remote.txt", "mime_type": "text/plain",
    }]}
    prompt = asyncio.run(
        render_task_message(message, agent_cwd=tmp_path, resolver=_resolver)
    )
    assert seen == [make_upload_uri(upload_id)]
    target = tmp_path / ".agt" / "inputs" / "remote.txt"
    assert target.read_bytes() == b"remote"
    assert str(target) in prompt


def test_render_disambiguates_same_name_different_bytes(tmp_path) -> None:
    uploads = [
        ("0f8fad5b-d9cb-469f-a165-70867728950e", b"one"),
        ("1f8fad5b-d9cb-469f-a165-70867728950e", b"two"),
    ]
    message = {"parts": [
        {
            "type": "file",
            "uri": make_upload_uri(uid),
            "name": "same.txt",
            "inline_b64": base64.b64encode(data).decode(),
        }
        for uid, data in uploads
    ]}
    prompt = asyncio.run(render_task_message(message, agent_cwd=tmp_path))
    inputs = tmp_path / ".agt" / "inputs"
    assert (inputs / "same.txt").read_bytes() == b"one"
    assert (inputs / "same-1f8fad5b.txt").read_bytes() == b"two"
    assert "same-1f8fad5b.txt" in prompt


def test_render_fails_loudly_on_unresolvable_reference(tmp_path) -> None:
    message = {"parts": [{
        "type": "file",
        "uri": make_upload_uri("0f8fad5b-d9cb-469f-a165-70867728950e"),
    }]}

    async def _resolver(uri: str):
        return None

    try:
        asyncio.run(
            render_task_message(message, agent_cwd=tmp_path, resolver=_resolver)
        )
    except ValueError as exc:
        assert "unresolved input reference" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
