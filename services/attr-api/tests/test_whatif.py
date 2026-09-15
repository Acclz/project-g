"""What-If 推演：可干预白名单、弹性区间、外推警告与可复现（需求说明书 §5.8）。"""

from __future__ import annotations

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.whatif import (
    WhatIfError,
    WhatIfRequest,
    intervenable_factors,
    resolve_knob,
    run_whatif,
)

BASE = Period("2026-06-01", "2026-06-04")
CURRENT = Period("2026-06-05", "2026-06-08")


@pytest.fixture(scope="module")
def engine(sandbox_settings: Settings) -> MetricEngine:
    return MetricEngine(sandbox_settings)


@pytest.fixture(scope="module")
def fmcg_engine(fmcg_injection_warehouse: SmallWarehouse) -> MetricEngine:
    settings = Settings(
        warehouse_db_path=fmcg_injection_warehouse.dw_path,
        app_db_path=fmcg_injection_warehouse.app_path,
    )
    return MetricEngine(settings)


def _request(**overrides) -> WhatIfRequest:
    payload = {
        "scenario": "ecom",
        "base": BASE,
        "current": CURRENT,
        "factor": "price_index",
        "slice_filter": SliceFilter({"channel": ("paid_ads",)}),
    }
    payload.update(overrides)
    return WhatIfRequest(**payload)


def test_intervenable_factors_come_from_metric_dictionary(engine: MetricEngine) -> None:
    items = {item["code"]: item for item in intervenable_factors(engine, "ecom")}
    assert set(items) == {"budget_share", "price_index", "commission_rate"}
    assert items["price_index"]["estimable"] is True
    assert items["commission_rate"]["estimable"] is False
    assert "佣金" in items["commission_rate"]["reject_reason"]


def test_non_intervenable_factor_is_rejected(engine: MetricEngine) -> None:
    for factor in ("uv", "competitor_price", "weather"):
        with pytest.raises(WhatIfError) as excinfo:
            resolve_knob(engine, "ecom", factor)
        assert "可干预" in str(excinfo.value)


def test_intervenable_but_not_estimable_is_rejected(engine: MetricEngine) -> None:
    with pytest.raises(WhatIfError) as excinfo:
        resolve_knob(engine, "ecom", "commission_rate")
    assert "佣金" in str(excinfo.value)


def test_run_whatif_on_short_history_degrades(engine: MetricEngine) -> None:
    """小样本数仓只有 8 天 → 必须走降级路径并把区间放宽（不许冒充精确值）。"""

    report = run_whatif(_request(), engine=engine)
    estimate = report.curve.elasticity
    assert estimate.method == "finite_difference"
    assert estimate.degraded is True
    assert estimate.confidence <= 0.5
    assert any("降级" in warning for warning in report.warnings)
    assert report.curve.base_value > 0
    root = engine.period_values("ecom", CURRENT, SliceFilter({"channel": ("paid_ads",)}))["gmv"]
    assert report.curve.base_value == pytest.approx(root)
    assert report.target_metric["code"] == "gmv"
    assert all(
        point.low <= point.expected <= point.high for point in report.curve.points
    ), "区间必须包住点估计"


def test_out_of_range_adjustment_is_flagged(engine: MetricEngine) -> None:
    report = run_whatif(_request(adjustments=(0.0, 0.1, 0.5)), engine=engine)
    assert report.curve.out_of_range is True
    assert any("超出历史观测区间" in warning for warning in report.warnings)
    assert all(point.out_of_range is False for point in report.curve.points[:2])


def test_whatif_is_deterministic(engine: MetricEngine) -> None:
    first = run_whatif(_request(), engine=engine)
    second = run_whatif(_request(), engine=engine)
    assert first.curve.as_dict() == second.curve.as_dict()
    assert first.assumptions == second.assumptions


def test_breakdown_estimates_per_channel(engine: MetricEngine) -> None:
    report = run_whatif(_request(dimensions=(("channel",),)), engine=engine)
    assert report.breakdown, "按渠道分别估弹性时要有分维度结果"
    estimated = [item for item in report.breakdown if item["curve"]]
    assert estimated, report.breakdown
    for item in estimated:
        assert item["sample_size"] >= 4
        assert item["curve"]["elasticity"]["value"] == pytest.approx(
            item["curve"]["elasticity"]["value"]
        )
        assert item["base_value"] > 0
    skipped = [item for item in report.breakdown if not item["curve"]]
    for item in skipped:
        assert item["reason"], "不估的组合必须说明原因"


def test_too_short_window_is_refused(engine: MetricEngine) -> None:
    with pytest.raises(WhatIfError) as excinfo:
        run_whatif(_request(window_days=2), engine=engine)
    assert "不能推演" in str(excinfo.value)


def test_fmcg_commission_rate_uses_warehouse_caliber(fmcg_engine: MetricEngine) -> None:
    report = run_whatif(
        WhatIfRequest(
            scenario="fmcg",
            base=Period("2026-04-01", "2026-04-05"),
            current=Period("2026-04-06", "2026-04-10"),
            factor="commission_rate",
        ),
        engine=fmcg_engine,
    )
    assert report.knob.proxy_label == "佣金率"
    assert report.target_metric["code"] == "gross_profit"
    assert report.curve.points
    assert any("单因子局部均衡" in item for item in report.assumptions)
    assert any("不是因果保证" in item for item in report.assumptions)
    assert "What-If" in report.render()
