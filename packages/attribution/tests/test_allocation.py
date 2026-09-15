"""维度贡献：精确优先、降级分摊必须标注（技术规格 §5.5、需求说明书 §5.3）。"""

import pytest

from attribution.allocation import (
    AllocationError,
    allocate_by_share,
    exact_contributions,
)
from attribution.conservation import ConservationError


def test_exact_contributions_conserve_and_rank():
    base = {("paid_ads",): 100.0, ("natural_search",): 80.0, ("livestream",): 20.0}
    current = {("paid_ads",): 60.0, ("natural_search",): 85.0, ("livestream",): 20.0}
    table = exact_contributions(base, current, dimensions=("channel",), top_n=2)
    assert table.heuristic is False
    # 精确路径的第一条硬要求：Σ贡献 == ΔY（守恒断言内部已过一遍）
    assert table.residual == pytest.approx(0.0, abs=1e-12)
    assert table.total_delta == pytest.approx(-35.0)
    assert [item.key for item in table.contributions] == [
        ("paid_ads",),
        ("natural_search",),
        ("livestream",),
    ]
    # TOP 2 覆盖率 = (40 + 5) / (40 + 5 + 0) = 1.0
    assert table.coverage == pytest.approx(1.0)
    assert table.contributions[0].share_of_change == pytest.approx(-40 / -35)
    assert table.contributions[0].in_top is True
    assert table.contributions[2].in_top is False


def test_exact_contributions_handles_new_and_vanished_combos():
    table = exact_contributions(
        {("a",): 10.0, ("b",): 5.0},
        {("a",): 4.0, ("c",): 3.0},
        dimensions=("channel",),
    )
    rows = {item.key: item for item in table.contributions}
    # 只在现期出现的组合：基期按 0 计，贡献就是它自己的现期值
    assert rows[("c",)].contribution == pytest.approx(3.0)
    assert "新出现" in rows[("c",)].note
    # 只在基期出现的组合：现期按 0 计
    assert rows[("b",)].contribution == pytest.approx(-5.0)
    assert "已消失" in rows[("b",)].note
    assert table.total_delta == pytest.approx(-8.0)


def test_exact_contributions_cross_check_against_metric_tree_delta():
    base = {("east",): 100.0, ("west",): 50.0}
    current = {("east",): 120.0, ("west",): 55.0}
    # 指标树给出的同切片总变动是 25，透视图算出来也是 25 → 通过
    exact_contributions(base, current, dimensions=("region",), expected_delta=25.0)
    # 两个独立来源对不上时必须报错，不允许"看起来差不多"就放过
    with pytest.raises(ConservationError):
        exact_contributions(base, current, dimensions=("region",), expected_delta=30.0)


def test_zero_delta_has_no_contribution_rate():
    table = exact_contributions(
        {("a",): 10.0, ("b",): 10.0},
        {("a",): 12.0, ("b",): 8.0},
        dimensions=("channel",),
    )
    assert table.total_delta == 0.0
    assert all(item.share_of_change is None for item in table.contributions)
    assert any("总变动为 0" in note for note in table.notes)


def test_share_allocation_is_marked_heuristic_and_still_conserves():
    table = allocate_by_share(
        -100.0,
        {("paid_ads",): 3.0, ("natural_search",): 1.0},
        dimensions=("channel",),
    )
    assert table.heuristic is True
    assert table.residual == pytest.approx(0.0, abs=1e-12)
    check = {item.key: item.contribution for item in table.contributions}
    assert check[("paid_ads",)] == pytest.approx(-75.0)
    assert check[("natural_search",)] == pytest.approx(-25.0)
    assert all("启发式" in note for note in table.notes)


def test_share_allocation_rejects_bad_weights():
    with pytest.raises(AllocationError):
        allocate_by_share(-1.0, {("a",): -1.0, ("b",): 2.0}, dimensions=("channel",))
    with pytest.raises(AllocationError):
        allocate_by_share(-1.0, {("a",): 0.0}, dimensions=("channel",))
    with pytest.raises(AllocationError):
        allocate_by_share(-1.0, {}, dimensions=("channel",))


def test_dimension_guards():
    with pytest.raises(AllocationError):
        exact_contributions({}, {}, dimensions=())
    with pytest.raises(AllocationError):
        exact_contributions({}, {}, dimensions=("channel", "channel"))
    with pytest.raises(AllocationError):
        exact_contributions(
            {}, {}, dimensions=("channel", "category", "region", "segment")
        )
    with pytest.raises(AllocationError):
        # 键宽度必须与维度数一致，否则会把"渠道"错当成"渠道×品类"
        exact_contributions({("east", "north"): 1.0}, {}, dimensions=("region",))
    with pytest.raises(AllocationError):
        exact_contributions({("east",): float("nan")}, {}, dimensions=("region",))


def test_render_marks_top_and_heuristic():
    table = exact_contributions(
        {("paid_ads",): 100.0, ("livestream",): 10.0},
        {("paid_ads",): 40.0, ("livestream",): 9.0},
        dimensions=("channel",),
        top_n=1,
    )
    text = table.render()
    assert "TOP 1" in text and "[TOP]" in text and "按组合精确计算" in text
