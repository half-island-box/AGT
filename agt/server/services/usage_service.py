"""Token/cost usage aggregation for the web-console dashboard.

The per-Step usage columns on ``StepModel`` (table ``tasks``) are already
written at report time (see ``report_service._apply_usage``). This module only
reads them back, aggregating across the ``daemon_id × provider × model``
dimensions and bucketing by time for the dashboard's trend line.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import StepModel

# ``tasks.created_at`` is stored as ``"%Y-%m-%d %H:%M:%S"`` (naive UTC, see
# ``job_service._now``), so string comparison is a valid time-range filter and
# ``substr`` gives clean buckets with no timezone/parse overhead.
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_BUCKET_ORDER = ("hour", "day", "month")
_MAX_BUCKETS = 4000


def _usage_condition():
    """Only usage-bearing Steps count — legacy daemons / unreported runs have
    NULL across every usage column and would otherwise inflate ``steps``."""
    return (
        StepModel.input_tokens.is_not(None)
        | StepModel.output_tokens.is_not(None)
        | StepModel.cost_usd.is_not(None)
    )


def _base_filters(*, from_ts: str, to_ts: str, agent: str | None, daemon_id: str | None):
    conds = [
        StepModel.created_at >= from_ts,
        StepModel.created_at < to_ts,
        _usage_condition(),
    ]
    if agent and agent != "all":
        conds.append(StepModel.provider == agent)
    if daemon_id:
        conds.append(StepModel.daemon_id == daemon_id)
    return conds


def _sum_columns():
    """The SUM/COUNT column list, shared by the totals and GROUP BY queries."""
    return (
        func.coalesce(func.sum(StepModel.input_tokens), 0),
        func.coalesce(func.sum(StepModel.output_tokens), 0),
        func.coalesce(func.sum(StepModel.cache_read_tokens), 0),
        func.coalesce(func.sum(StepModel.cache_creation_tokens), 0),
        func.coalesce(func.sum(StepModel.cost_usd), 0.0),
        func.count(),
        func.coalesce(func.sum(StepModel.num_turns), 0),
    )


def _bucket_key(dt: datetime, bucket: str) -> str:
    if bucket == "month":
        return dt.strftime("%Y-%m")
    if bucket == "hour":
        return dt.strftime("%Y-%m-%d %H")
    return dt.strftime("%Y-%m-%d")


def _bucket_count(bucket: str, from_dt: datetime, to_dt: datetime) -> float:
    days = max(0.0, (to_dt - from_dt).total_seconds() / 86400.0)
    if bucket == "hour":
        return days * 24
    if bucket == "month":
        return days / 30.0
    return days


def _resolve_bucket(bucket: str, from_dt: datetime, to_dt: datetime) -> str:
    """Coarsen the requested granularity if it would overflow ``_MAX_BUCKETS``."""
    start = _BUCKET_ORDER.index(bucket)
    for candidate in _BUCKET_ORDER[start:]:
        if _bucket_count(candidate, from_dt, to_dt) <= _MAX_BUCKETS:
            return candidate
    return "month"


def _bucket_sequence(bucket: str, from_dt: datetime, to_dt: datetime) -> list[str]:
    """Contiguous bucket keys in ``[from_dt, to_dt)`` at the given granularity."""
    keys: list[str] = []
    if bucket == "month":
        dt = from_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while dt < to_dt:
            keys.append(_bucket_key(dt, bucket))
            dt = (dt.replace(year=dt.year + 1, month=1) if dt.month == 12
                  else dt.replace(month=dt.month + 1))
    elif bucket == "hour":
        dt = from_dt.replace(minute=0, second=0, microsecond=0)
        while dt < to_dt:
            keys.append(_bucket_key(dt, bucket))
            dt += timedelta(hours=1)
    else:  # day
        dt = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while dt < to_dt:
            keys.append(_bucket_key(dt, bucket))
            dt += timedelta(days=1)
    return keys


_EMPTY = (0, 0, 0, 0, 0.0, 0, 0)


def _zero_filled_series(rows: dict[str, tuple], keys: list[str]) -> list[dict]:
    return [
        {
            "bucket": key,
            "input_tokens": rows.get(key, _EMPTY)[0],
            "output_tokens": rows.get(key, _EMPTY)[1],
            "cache_read_input_tokens": rows.get(key, _EMPTY)[2],
            "cache_creation_input_tokens": rows.get(key, _EMPTY)[3],
            "cost_usd": rows.get(key, _EMPTY)[4],
            "steps": rows.get(key, _EMPTY)[5],
        }
        for key in keys
    ]


async def usage_summary(
    db: AsyncSession,
    *,
    agent: str | None = None,
    daemon_id: str | None = None,
    from_ts: str,
    to_ts: str,
    bucket: str = "day",
) -> dict:
    """Aggregate usage into totals + time series + daemon×agent breakdown.

    Returns a dict shaped like ``UsageSummaryResponse`` (the route wraps it in
    the Pydantic model), with ``series`` zero-filled across the full window.
    """
    from_dt = datetime.strptime(from_ts, _TS_FMT)
    to_dt = datetime.strptime(to_ts, _TS_FMT)
    bucket = _resolve_bucket(bucket, from_dt, to_dt)

    conds = _base_filters(
        from_ts=from_ts, to_ts=to_ts, agent=agent, daemon_id=daemon_id
    )

    # ── totals ──
    totals_row = (await db.execute(select(*_sum_columns()).where(*conds))).one()

    # ── series ──
    bucket_expr = func.substr(StepModel.created_at, 1, {
        "hour": 13, "day": 10, "month": 7,
    }[bucket])
    series_stmt = (
        select(bucket_expr, *_sum_columns())
        .where(*conds)
        .group_by(bucket_expr)
    )
    series_rows = {
        row[0]: tuple(row[1:])
        for row in (await db.execute(series_stmt)).all()
    }
    keys = _bucket_sequence(bucket, from_dt, to_dt)
    series = _zero_filled_series(series_rows, keys)

    # ── breakdown (daemon × agent × model) ──
    breakdown_stmt = (
        select(
            StepModel.daemon_id,
            StepModel.provider,
            StepModel.model,
            *_sum_columns(),
        )
        .where(*conds)
        .group_by(StepModel.daemon_id, StepModel.provider, StepModel.model)
        .order_by(func.coalesce(func.sum(StepModel.cost_usd), 0.0).desc())
    )
    breakdown = [
        {
            "daemon_id": row[0],
            "agent": row[1],
            "model": row[2],
            "input_tokens": row[3],
            "output_tokens": row[4],
            "cache_read_input_tokens": row[5],
            "cache_creation_input_tokens": row[6],
            "cost_usd": row[7],
            "steps": row[8],
        }
        for row in (await db.execute(breakdown_stmt)).all()
    ]

    return {
        "from": from_ts,
        "to": to_ts,
        "totals": {
            "input_tokens": totals_row[0],
            "output_tokens": totals_row[1],
            "cache_read_input_tokens": totals_row[2],
            "cache_creation_input_tokens": totals_row[3],
            "cost_usd": totals_row[4],
            "steps": totals_row[5],
            "turns": totals_row[6],
        },
        "series": series,
        "breakdown": breakdown,
    }


__all__ = ["usage_summary"]
