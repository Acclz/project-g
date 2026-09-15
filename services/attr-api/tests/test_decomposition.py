"""分解链路测试：真实数仓取数 → 逐层分解 → 守恒，并对预埋真因做一次核对。"""

from __future__ import annotations

import json

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.db import connect_app
from app.services.decomposition import (
    DecompositionError,
    MetricEngine,
    Period,
    SliceFilter,
)


@pytest.fixture(scope="module")
def engine(sandbox_settings: Settings) -> MetricEngine:
    return MetricEngine(sandbox_settings)


@pytest.fixture(scope="module")
def fmcg_engine(fmcg_injection_warehouse: SmallWarehouse) -> MetricEngine:
    """快消场景要看快消那份小样本数仓（时间窗与 ecom 样本不同）。"""

    settings = Settings(
        warehouse_db_path=fmcg_injection_warehouse.dw_path,
        app_db_path=fmcg_injection_warehouse.app_path,
    )
    return MetricEngine(settings)


def test_metrics_dictionary_is_describable(engine: MetricEngine) -> None:
    described = engine.describe()
    assert set(described) == {"ecom", "fmcg"}
    assert described["ecom"]["root"] == "gmv"
    assert described["fmcg"]["root"] == "gross_profit"
    for scenario, payload in described.items():
        assert payload["metrics"], scenario
        assert all(metric["has_sql"] for metric in payload["metrics"]), "每个节点都必须能取数"


def test_period_values_are_positive_on_real_data(
    engine: MetricEngine, ecom_injection_warehouse: SmallWarehouse
) -> None:
    values = engine.period_values("ecom", Period("2026-06-01", "2026-06-04"))
    assert values["gmv"] > 0
    assert values["uv"] > 0 and values["cvr"] > 0 and values["aov"] > 0
    # 指标树的恒等式在取数层就成立（P2 已验证整库，这里再验一次两期取值）
    assert values["uv"] * values["cvr"] * values["aov"] == pytest.approx(values["gmv"], rel=1e-9)


def test_ecom_level1_decomposition_conserves(engine: MetricEngine) -> None:
    report = engine.decompose(
        "ecom", Period("2026-06-01", "2026-06-04"), Period("2026-06-05", "2026-06-08")
    )
    assert report.ok, report.render()
    assert set(report.root.contributions) == {"uv", "cvr", "aov"}
    assert report.root.method == "lmdi"
    assert len(report.nodes) == 1, "默认只拆根（一级拆解）"


def test_ecom_mixed_tree_drilldown(engine: MetricEngine) -> None:
    """电商场景下钻两级：外层乘法 + 内层乘法，逐层独立守恒。"""

    report = engine.decompose(
        "ecom",
        Period("2026-06-01", "2026-06-04"),
        Period("2026-06-05", "2026-06-08"),
        targets=["uv", "cvr", "aov"],
    )
    assert report.ok
    assert {node.code for node in report.nodes} == {"gmv", "uv", "cvr", "aov"}
    for node in report.nodes:
        # 判定口径是相对误差（分母 max(|Δ|,1)）；绝对残差要看量级才有意义
        assert node.relative < 1e-9, f"{node.code} 相对误差 {node.relative:.3e}"
        assert report.max_relative_residual < 1e-9


def test_fmcg_additive_root_with_multiplicative_children(fmcg_engine: MetricEngine) -> None:
    """快消场景：加法根（差额分析）+ 乘法子节点（LMDI）——真正的混合树。"""

    report = fmcg_engine.decompose(
        "fmcg",
        Period("2026-04-01", "2026-04-05"),
        Period("2026-04-06", "2026-04-10"),
        targets=["revenue", "raw_material_cost", "channel_commission"],
    )
    assert report.ok, report.render()
    methods = {node.method for node in report.nodes}
    assert methods == {"diff", "lmdi"}
    assert set(report.root.contributions) == {
        "revenue",
        "raw_material_cost",
        "logistics_cost",
        "channel_commission",
        "other_cost",
    }


def test_slice_filter_narrows_the_scope(engine: MetricEngine) -> None:
    whole = engine.period_values("ecom", Period("2026-06-01", "2026-06-04"))
    sliced = engine.period_values(
        "ecom",
        Period("2026-06-01", "2026-06-04"),
        SliceFilter({"channel": ("paid_ads",)}),
    )
    assert 0 < sliced["gmv"] < whole["gmv"]


def test_fmcg_category_slice_goes_through_dim_sku(fmcg_engine: MetricEngine) -> None:
    """快消事实表没有 category_id，必须经 dim_sku 折算（否则会静默算成全量）。"""

    whole = fmcg_engine.period_values("fmcg", Period("2026-04-01", "2026-04-05"))
    sliced = fmcg_engine.period_values(
        "fmcg",
        Period("2026-04-01", "2026-04-05"),
        SliceFilter({"category": ("grain_oil", "dairy")}),
    )
    assert 0 < sliced["revenue"] < whole["revenue"]


def test_unknown_slice_code_fails_fast(engine: MetricEngine) -> None:
    with pytest.raises(DecompositionError):
        engine.period_values(
            "ecom", Period("2026-06-01", "2026-06-04"), SliceFilter({"channel": ("nope",)})
        )


def test_period_previous_window(engine: MetricEngine) -> None:
    period = Period("2026-06-05", "2026-06-08")
    assert period.days == 4
    assert period.previous().as_dict() == {
        "start": "2026-06-01",
        "end": "2026-06-04",
        "days": 4,
        "label": "上一期",
    }


def test_ground_truth_factor_is_the_top_driver(
    engine: MetricEngine, ecom_injection_warehouse: SmallWarehouse
) -> None:
    """P3 核对（不是评测）：预埋真因窗口里，真值声明的因子应当是根节点头号贡献来源。

    注意口径差异：真值记录的是"注入本身造成的贡献"，而分解看到的是"两期全部变化"，
    因此这里只核对方向与排名；贡献额的严格比较由 P6 的评测集（配套对照窗口）负责。
    """

    app = connect_app(ecom_injection_warehouse.app_path)
    try:
        row = app.execute(
            "SELECT * FROM ground_truth WHERE scenario = 'ecom' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        app.close()
    assert row is not None
    slice_filter = SliceFilter.from_json(row["dimension_json"])
    report = engine.decompose(
        "ecom",
        Period("2026-06-01", "2026-06-04"),
        Period("2026-06-05", "2026-06-08"),
        slice_filter=slice_filter,
        targets=["uv", "cvr", "aov"],
    )
    top = report.top(limit=3)
    assert top[0]["factor"] == row["factor"], f"头号贡献应为 {row['factor']}，实际 {top[0]}"
    assert top[0]["contribution"] < 0, "注入是下滑，贡献必须为负"
    assert report.ok


def test_validate_sql_reports_every_node(engine: MetricEngine) -> None:
    checks = engine.validate_sql("ecom", Period("2026-06-01", "2026-06-04"))
    assert len(checks) == 11  # 电商场景 11 个指标节点
    assert all(check["ok"] for check in checks)


def test_slice_filter_json_roundtrip() -> None:
    payload = {"channel": ["paid_ads"], "region": ["east"]}
    restored = SliceFilter.from_json(json.dumps(payload, ensure_ascii=False))
    assert json.loads(restored.as_json()) == payload
    assert SliceFilter.from_json(None).empty
