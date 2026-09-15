"""分解算法：加法（差额分析）与乘法（LMDI）的守恒性质测试。

对应验收标准 E2：加法 / 乘法 / 混合树三种结构各 ≥100 组随机数据，通过率 100%。
"""

import math
import random

import pytest

from attribution.diff import additive_contributions, contribution_rates
from attribution.lmdi import LmdiError, lmdi_contributions


def _random_factors(rng: random.Random, count: int, low: float, high: float):
    return {f"f{i}": rng.uniform(low, high) for i in range(count)}


@pytest.mark.parametrize("count", [2, 3, 4, 6])
def test_additive_conservation_200_random_cases(count):
    rng = random.Random(20260915 + count)
    for _ in range(200):
        base = _random_factors(rng, count, -500.0, 500.0)  # 加法允许负项（如成本抵扣）
        current = {k: v + rng.uniform(-100.0, 100.0) for k, v in base.items()}
        contributions = additive_contributions(base, current)
        delta = sum(current.values()) - sum(base.values())
        assert abs(sum(contributions.values()) - delta) < 1e-9


def test_additive_rejects_mismatched_factor_sets():
    with pytest.raises(ValueError):
        additive_contributions({"a": 1.0}, {"b": 2.0})


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_lmdi_conservation_200_random_cases(count):
    rng = random.Random(19900101 + count)
    for _ in range(200):
        base = _random_factors(rng, count, 1.0, 500.0)
        current = {k: v * rng.uniform(0.5, 2.0) for k, v in base.items()}
        total_base = math.prod(base.values())
        total_current = math.prod(current.values())
        contributions, meta = lmdi_contributions(base, current, total_base, total_current)
        delta = total_current - total_base
        relative = abs(sum(contributions.values()) - delta) / max(abs(delta), 1.0)
        assert relative < 1e-9
        assert meta["zero_substituted"] is False
        # residual 是绝对残差；判定用相对误差（与需求 §5.2 一致）
        assert meta["residual"] / max(abs(delta), 1.0) < 1e-9


def test_lmdi_zero_substitution_is_marked():
    base = {"a": 0.0, "b": 2.0}
    current = {"a": 1.0, "b": 3.0}
    contributions, meta = lmdi_contributions(base, current, 0.0, 1.0, zero_policy="substitute")
    assert meta["zero_substituted"] is True
    assert meta["totals_recomputed"] is True
    assert meta["substituted_factors"] == ["a"]
    recomputed_delta = meta["recomputed_total_current"] - meta["recomputed_total_base"]
    assert abs(sum(contributions.values()) - recomputed_delta) < 1e-9
    # 原始总量必须保留在 meta 中，避免"悄悄改掉目标值"
    assert meta["original_total_base"] == 0.0
    assert meta["original_total_current"] == 1.0


def test_lmdi_zero_policy_defaults_to_error():
    with pytest.raises(LmdiError):
        lmdi_contributions({"a": 0.0}, {"a": 1.0}, 1.0, 1.0)


def test_lmdi_requires_positive_totals():
    with pytest.raises(LmdiError):
        lmdi_contributions({"a": 1.0}, {"a": 2.0}, 0.0, 2.0)


def test_lmdi_with_unchanged_factor_is_exactly_zero():
    base = {"a": 10.0, "b": 5.0}
    current = {"a": 10.0, "b": 8.0}
    contributions, _ = lmdi_contributions(base, current, 50.0, 80.0)
    assert contributions["a"] == pytest.approx(0.0, abs=1e-12)


def test_contribution_rates_undefined_when_delta_zero():
    rates = contribution_rates({"a": 1.0, "b": -1.0}, 0.0)
    assert rates == {"a": None, "b": None}
    normal = contribution_rates({"a": 3.0, "b": 7.0}, 10.0)
    assert normal["a"] == pytest.approx(0.3)
