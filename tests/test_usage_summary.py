"""Usage-dashboard aggregation endpoint (``GET /api/usage/summary``) tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.models import Base, StepModel


SECRET = "usage-summary-test-secret-0123456789"


def _app(tmp_path: Path):
    cfg = ServerConfig(
        db_path=str(tmp_path / "server.db"),
        sessions_json_path=str(tmp_path / "sessions.json"),
        secret=SECRET,
        console_enabled=False,
        console_assets_dir=str(tmp_path / "assets"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    return create_app(cfg), engine, make_session_factory(engine)


def _step(**kwargs) -> StepModel:
    base = {
        "step_id": "t0",
        "provider": "claude",
        "prompt": "hi",
        "status": "completed",
        "daemon_id": "d1",
        "created_at": "2026-10-20 10:00:00",
        "updated_at": "2026-10-20 10:00:00",
    }
    base.update(kwargs)
    return StepModel(**base)


def _seed(factory, steps) -> None:
    async def _run() -> None:
        async with factory() as db:
            for step in steps:
                db.add(step)
            await db.commit()

    asyncio.run(_run())


def test_usage_summary_totals_series_breakdown(tmp_path) -> None:
    app, engine, factory = _app(tmp_path)
    try:
        _seed(
            factory,
            [
                _step(
                    step_id="a", daemon_id="d1", provider="claude", model="m1",
                    created_at="2026-10-20 10:00:00", input_tokens=100,
                    output_tokens=10, cost_usd=0.1, num_turns=2,
                ),
                _step(
                    step_id="b", daemon_id="d1", provider="claude", model="m1",
                    created_at="2026-10-21 10:00:00", input_tokens=200,
                    output_tokens=20, cache_read_tokens=30, cost_usd=0.2,
                    num_turns=3,
                ),
                _step(
                    step_id="c", daemon_id="d2", provider="codex", model="m2",
                    created_at="2026-10-21 11:00:00", input_tokens=50,
                    output_tokens=5, cost_usd=0.05, num_turns=1,
                ),
                # usage-less step: must be excluded from totals/steps.
                _step(
                    step_id="d", daemon_id="d1", provider="claude", model="m1",
                    created_at="2026-10-21 12:00:00",
                ),
            ],
        )
        with TestClient(app) as client:
            resp = client.get(
                "/api/usage/summary",
                params={
                    "from": "2026-10-20 00:00:00",
                    "to": "2026-10-22 00:00:00",
                    "bucket": "day",
                },
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["totals"]["input_tokens"] == 350
            assert data["totals"]["output_tokens"] == 35
            assert data["totals"]["cache_read_input_tokens"] == 30
            assert data["totals"]["cache_creation_input_tokens"] == 0
            assert data["totals"]["cost_usd"] == pytest.approx(0.35)
            assert data["totals"]["steps"] == 3
            assert data["totals"]["turns"] == 6

            # two zero-filled day buckets.
            assert [p["bucket"] for p in data["series"]] == [
                "2026-10-20",
                "2026-10-21",
            ]
            assert data["series"][0]["input_tokens"] == 100
            assert data["series"][0]["steps"] == 1
            assert data["series"][1]["input_tokens"] == 250
            assert data["series"][1]["steps"] == 2

            # breakdown grouped by daemon×agent×model, ordered by cost desc.
            assert [b["agent"] for b in data["breakdown"]] == ["claude", "codex"]
            claude = data["breakdown"][0]
            assert claude["daemon_id"] == "d1"
            assert claude["model"] == "m1"
            assert claude["input_tokens"] == 300
            codex = data["breakdown"][1]
            assert codex["daemon_id"] == "d2"
            assert codex["input_tokens"] == 50
    finally:
        asyncio.run(engine.dispose())


def test_usage_summary_filters_and_validation(tmp_path) -> None:
    app, engine, factory = _app(tmp_path)
    try:
        _seed(
            factory,
            [
                _step(
                    step_id="a", daemon_id="d1", provider="claude", model="m1",
                    created_at="2026-10-20 10:00:00", input_tokens=100,
                    cost_usd=0.1,
                ),
                _step(
                    step_id="c", daemon_id="d2", provider="codex", model="m2",
                    created_at="2026-10-21 11:00:00", input_tokens=50,
                    cost_usd=0.05,
                ),
            ],
        )
        with TestClient(app) as client:
            # agent filter narrows totals and breakdown.
            r = client.get(
                "/api/usage/summary",
                params={
                    "from": "2026-10-20 00:00:00",
                    "to": "2026-10-22 00:00:00",
                    "agent": "claude",
                },
            ).json()
            assert r["totals"]["input_tokens"] == 100
            assert [b["agent"] for b in r["breakdown"]] == ["claude"]

            # daemon filter.
            r = client.get(
                "/api/usage/summary",
                params={
                    "from": "2026-10-20 00:00:00",
                    "to": "2026-10-22 00:00:00",
                    "daemon_id": "d2",
                },
            ).json()
            assert r["totals"]["input_tokens"] == 50
            assert r["breakdown"][0]["daemon_id"] == "d2"

            # from >= to is rejected.
            bad = client.get(
                "/api/usage/summary",
                params={
                    "from": "2026-10-22 00:00:00",
                    "to": "2026-10-20 00:00:00",
                },
            )
            assert bad.status_code == 400

            # malformed timestamp is rejected.
            bad2 = client.get(
                "/api/usage/summary",
                params={"from": "nope", "to": "2026-10-20 00:00:00"},
            )
            assert bad2.status_code == 400

            # unknown bucket is rejected.
            bad3 = client.get(
                "/api/usage/summary",
                params={
                    "from": "2026-10-20 00:00:00",
                    "to": "2026-10-22 00:00:00",
                    "bucket": "week",
                },
            )
            assert bad3.status_code == 400
    finally:
        asyncio.run(engine.dispose())
