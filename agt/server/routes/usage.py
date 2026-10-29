"""Usage dashboard aggregation endpoint (``GET /api/usage/summary``)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import UsageSummaryResponse
from ..services.usage_service import usage_summary

router = APIRouter(prefix="/api/usage", tags=["usage"])

_TS_FMT = "%Y-%m-%d %H:%M:%S"
_BUCKETS = {"hour", "day", "month"}


def _parse_ts(value: str) -> None:
    try:
        datetime.strptime(value, _TS_FMT)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"timestamps must be formatted as {_TS_FMT}",
        ) from None


@router.get("/summary", response_model=UsageSummaryResponse)
async def usage_summary_route(
    from_: str = Query(
        ..., alias="from", description="Inclusive start (YYYY-MM-DD HH:MM:SS)"
    ),
    to: str = Query(..., description="Exclusive end (YYYY-MM-DD HH:MM:SS)"),
    agent: str | None = Query(
        None, description="Provider filter; omit or 'all' for every agent"
    ),
    daemon_id: str | None = Query(None, description="Optional daemon filter"),
    bucket: str = Query("day", description="Series granularity: hour | day | month"),
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryResponse:
    """Aggregate token/cost usage into totals, a time series, and a
    ``daemon_id × provider × model`` breakdown for the console dashboard."""
    _parse_ts(from_)
    _parse_ts(to)
    if from_ >= to:
        raise HTTPException(status_code=400, detail="'from' must be earlier than 'to'")
    if bucket not in _BUCKETS:
        raise HTTPException(status_code=400, detail="bucket must be one of hour|day|month")

    data = await usage_summary(
        db,
        agent=agent,
        daemon_id=daemon_id,
        from_ts=from_,
        to_ts=to,
        bucket=bucket,
    )
    return UsageSummaryResponse(
        from_=data["from"],
        to=data["to"],
        totals=data["totals"],
        series=data["series"],
        breakdown=data["breakdown"],
    )


__all__ = ["router"]
