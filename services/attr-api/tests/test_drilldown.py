"""L2 下钻链路：切片只收紧、维度透视精确守恒、逐层守恒与覆盖率（需求说明书 §8.2）。"""

from __future__ import annotations

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.sandbox.runner import SandboxRunner
from app.services.decomposition import (
    DecompositionError,
    MetricEngine,
    Period,
    SliceFilter,
)
from app.services.drilldown import (
    DrilldownRequest,
    default_dimensions,
    parse_dimensions,
    run_drilldown,
)
from attribution import assert_conservation

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


def test_dimension_table_is_exact_and_cross_checks_metric_tree(engine: MetricEngine) -> None:
    pivot = engine.dimension_table("ecom", BASE, CURRENT, dimensions=("channel",))
    assert pivot.table.heuristic is False, "能精确计算就不许走分摊"
    assert pivot.conserved, "透视表只有过了守恒断言才会返回"
    # 两次独立取数（分组取数 vs 根指标取数）必须给出同一个层总变动
    assert_conservation([pivot.layer_delta], pivot.table.total_delta, pivot.table.tolerance)
    assert_conservation(
        [item.contribution for item in pivot.table.contributions],
        pivot.layer_delta,
        pivot.table.tolerance,
    )
    # 电商渠道共 8 个，小样本里也至少覆盖到 3 个以上
    assert len(pivot.table.contributions) >= 3
    assert 0.0 < pivot.table.coverage <= 1.0
    assert [item.rank for item in pivot.table.contributions] == list(
        range(1, len(pivot.table.contributions) + 1)
    )
    # 排序口径：按绝对贡献降序（讲解时第一行就是"最该被追问的那个"）
    magnitudes = [abs(item.contribution) for item in pivot.table.contributions]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_dimension_table_respects_locked_slice(engine: MetricEngine) -> None:
    """下钻贴着锁定切片走：切了渠道之后，透视表的层总变动就是该渠道的总变动。"""

    locked = SliceFilter({"channel": ("paid_ads",)})
    pivot = engine.dimension_table(
        "ecom", BASE, CURRENT, dimensions=("category",), slice_filter=locked
    )
    root = engine.period_values("ecom", CURRENT, locked)["gmv"] - engine.period_values(
        "ecom", BASE, locked
    )["gmv"]
    assert_conservation([pivot.layer_delta], root, pivot.table.tolerance)
    assert all(item.key for item in pivot.table.contributions)


def test_cross_dimension_pivot_still_conserves(engine: MetricEngine) -> None:
    pivot = engine.dimension_table(
        "ecom", BASE, CURRENT, dimensions=("channel", "category")
    )
    assert_conservation(
        [item.contribution for item in pivot.table.contributions],
        pivot.layer_delta,
        pivot.table.tolerance,
    )
    assert all(len(item.key) == 2 for item in pivot.table.contributions)


def test_fmcg_category_pivot_goes_through_sku_master(fmcg_engine: MetricEngine) -> None:
    """快消事实表没有 category_id：透视要经 dim_sku 折算，折算后仍必须守恒。"""

    pivot = fmcg_engine.dimension_table(
        "fmcg",
        Period("2026-04-01", "2026-04-05"),
        Period("2026-04-06", "2026-04-10"),
        dimensions=("category",),
    )
    assert_conservation(
        [item.contribution for item in pivot.table.contributions],
        pivot.layer_delta,
        pivot.table.tolerance,
    )
    assert len(pivot.table.contributions) >= 3
    assert "unknown" not in {key for item in pivot.table.contributions for key in item.key}


def test_dimension_table_rejects_bad_requests(engine: MetricEngine) -> None:
    with pytest.raises(DecompositionError):
        engine.dimension_table("ecom", BASE, CURRENT, dimensions=())
    with pytest.raises(DecompositionError):
        engine.dimension_table("ecom", BASE, CURRENT, dimensions=("channel", "channel"))
    with pytest.raises(DecompositionError):
        # 需求说明书 §5.3：单次交叉最多 3 个维度
        engine.dimension_table(
            "ecom", BASE, CURRENT, dimensions=("channel", "category", "region", "segment")
        )
    with pytest.raises(DecompositionError):
        # sku 是快消维度，电商事实表里没有
        engine.dimension_table("ecom", BASE, CURRENT, dimensions=("sku",))


def test_default_dimensions_skip_locked_ones(engine: MetricEngine) -> None:
    locked = SliceFilter({"channel": ("paid_ads",)})
    plan = default_dimensions(engine, "ecom", locked)
    flat = [name for item in plan for name in item]
    assert "channel" not in flat, "已被锁死的维度再透一遍没有信息量"
    assert flat, "至少要留下一个可下钻维度"
    assert all(len(item) == 1 for item in plan)


def test_parse_dimensions_shapes() -> None:
    assert parse_dimensions(None) == ()
    assert parse_dimensions(["channel"]) == (("channel",),)
    assert parse_dimensions([["channel", "category"], "region"]) == (
        ("channel", "category"),
        ("region",),
    )
    with pytest.raises(DecompositionError):
        parse_dimensions([[]])
    with pytest.raises(DecompositionError):
        parse_dimensions([["a", "b", "c", "d"]])


def test_run_drilldown_end_to_end(sandbox_settings: Settings) -> None:
    """整条 L2：收紧切片 → 透视 → 逐层守恒 → 新假设回 L1 验证。"""

    engine = MetricEngine(sandbox_settings)
    request = DrilldownRequest(
        scenario="ecom",
        base=BASE,
        current=CURRENT,
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        dimensions=(("category",), ("region",)),
        top_n=3,
        title="pytest 下钻",
        actor="pytest",
    )
    report = run_drilldown(
        request,
        engine=engine,
        sandbox=SandboxRunner(sandbox_settings),
        inherited_slice=SliceFilter(),
        narrowed=True,
    )
    assert report.conserved, report.conservation
    assert len(report.pivots) == 2
    assert {item["source"] for item in report.conservation} == {"decomposition", "dimension_table"}
    kinds = [step["kind"] for step in report.steps]
    assert kinds.count("drilldown") == 2
    assert "hypothesis" in kinds and "verify" in kinds, "新结论依赖新假设时必须回 L1 验证"
    payload = report.as_dict()
    assert payload["pivots"][0]["rows"] and payload["highlights"]
    assert "启发式" in payload["conclusion"]["disclaimer"]
    for item in report.pivots:
        assert item.table.top_n == 3
    assert report.analysis.decomposition.slice_filter.filters == {"channel": ("paid_ads",)}
    assert "只收紧" in report.render() or "下钻前切片" in report.render()
