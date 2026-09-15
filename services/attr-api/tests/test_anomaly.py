"""异动判定与大盘：基线带、Z 分数与"是否落在正常波动区间"（需求说明书 §5.4）。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.anomaly import (
    AnomalyError,
    baseline_for,
    dashboard_anomalies,
    dashboard_series,
    judge,
    resolve_period,
)
from app.services.decomposition import MetricEngine, Period


@pytest.fixture(scope="module")
def engine(sandbox_settings: Settings) -> MetricEngine:
    return MetricEngine(sandbox_settings)


def test_period_resolution(engine: MetricEngine) -> None:
    period = resolve_period(engine, "2026-06-01~2026-06-04")
    assert (period.start, period.end) == ("2026-06-01", "2026-06-04")
    with pytest.raises(AnomalyError):
        resolve_period(engine, "2026-06-01")
    default = resolve_period(engine, None, default_days=3)
    assert default.days == 3


def test_baseline_band_uses_weekly_lags(engine: MetricEngine) -> None:
    """基线 = 过去 4 周同星期几的中位数：小样本只有 8 天，正好能对上 1 周前的同一天。"""

    period = Period("2026-06-05", "2026-06-08")
    band = baseline_for(engine, "ecom", period)
    assert band["value"] is not None and band["value"] > 0
    assert band["covered_days"] >= 1
    # 带宽必须包住基线本身（低 ≤ 中位 ≤ 高）
    assert band["low"] <= band["value"] <= band["high"]


def test_judge_reports_band_position(engine: MetricEngine) -> None:
    verdict = judge(engine, "ecom", Period("2026-06-05", "2026-06-08"), "gmv")
    assert verdict.code == "gmv"
    assert verdict.current_value > 0 and verdict.base_value > 0
    assert verdict.change_rate is not None
    assert isinstance(verdict.in_normal_band, bool)
    payload = verdict.as_dict()
    assert payload["baseline_low"] <= payload["baseline_high"]
    assert payload["period"]["start"] == "2026-06-05"
    assert payload["base_period"]["end"] == "2026-06-04"


def test_judge_rejects_unknown_metric(engine: MetricEngine) -> None:
    with pytest.raises(AnomalyError):
        judge(engine, "ecom", Period("2026-06-05", "2026-06-08"), "not_a_metric")


def test_dashboard_anomalies_sorts_by_severity(engine: MetricEngine) -> None:
    payload = dashboard_anomalies(
        engine, "ecom", period=Period("2026-06-05", "2026-06-08")
    )
    assert payload["items"], "异动清单不能为空"
    severities = [abs(item["severity"]) for item in payload["items"]]
    assert severities == sorted(severities, reverse=True), "异动清单按严重度降序"
    codes = {item["code"] for item in payload["items"]}
    assert {"gmv", "uv", "cvr", "aov"} <= codes
    for item in payload["items"]:
        assert "change_rate" in item and "z_score" in item
        # 每个指标都必须回答"是否落在正常波动区间"（§5.4 去噪要求）
        assert isinstance(item["in_normal_band"], bool)
        assert "started" in item["attribution"]
    assert all(item["is_anomaly"] for item in payload["anomalies"])


def test_dashboard_series_returns_points_and_band(engine: MetricEngine) -> None:
    payload = dashboard_series(engine, "ecom", "gmv", days=5)
    assert payload["code"] == "gmv"
    assert payload["points"] and all(point["value"] > 0 for point in payload["points"])
    assert payload["baseline"]["value"] is not None
    with pytest.raises(AnomalyError):
        dashboard_series(engine, "ecom", "not_a_metric")
