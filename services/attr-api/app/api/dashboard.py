"""大盘与异动路由（技术规格 §4.3）。

两个接口就是需求说明书 §4.1 的两块数据：异动清单（按严重度排序）与"序列 + 基线带"。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.anomaly import (
    AnomalyError,
    dashboard_anomalies,
    dashboard_series,
    resolve_period,
)
from app.services.decomposition import MetricEngine

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/anomalies")
def anomalies(
    domain: str = Query(default="ecom", description="场景编码：ecom / fmcg"),
    period: str | None = Query(default=None, description="现期，格式 起~止；省略取数仓末尾 7 天"),
    threshold: float | None = Query(default=None, description="变动幅度阈值，默认取指标字典口径"),
) -> dict[str, Any]:
    """异动清单：幅度 + Z 分数 + 是否落在正常波动区间 + 是否已发起归因。"""

    engine = MetricEngine()
    if domain not in engine.scenarios():
        raise HTTPException(status_code=404, detail=f"未知场景：{domain}")
    try:
        active_period = resolve_period(engine, period)
        kwargs = {} if threshold is None else {"change_threshold": threshold}
        return dashboard_anomalies(engine, domain, period=active_period, **kwargs)
    except AnomalyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/series")
def series(
    domain: str = Query(default="ecom"),
    metric: str = Query(description="指标编码，例如 gmv / gross_profit"),
    days: int = Query(default=60, ge=7, le=400),
) -> dict[str, Any]:
    """指标日序列 + 基线带（大盘卡片与趋势图共用）。"""

    engine = MetricEngine()
    try:
        return dashboard_series(engine, domain, metric, days=days)
    except AnomalyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
