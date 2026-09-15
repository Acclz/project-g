"""统计检验与置信度合成测试（纯算法，不依赖数仓与框架）。"""

from __future__ import annotations

import numpy as np
import pytest

from attribution import (
    InferenceError,
    absolute_risk_difference,
    composite_confidence,
    coverage_score,
    direction_consistency,
    effect_score,
    permutation_test,
    significance_score,
    spearman_correlation,
    standardized_difference,
)

SEED = 20260915


def test_spearman_extremes_and_ties() -> None:
    assert spearman_correlation([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert spearman_correlation([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)
    # 并列值取平均秩：单调关系仍然给出 1.0
    assert spearman_correlation([1, 1, 2, 3], [5, 5, 6, 7]) == pytest.approx(1.0)


def test_spearman_rejects_constant_series() -> None:
    with pytest.raises(InferenceError):
        spearman_correlation([1, 1, 1, 1, 1], [1, 2, 3, 4, 5])


def test_permutation_test_is_reproducible() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=30)
    y = 2.5 * x + rng.normal(scale=0.5, size=30)
    first = permutation_test(x, y, permutations=500, seed=SEED)
    second = permutation_test(x, y, permutations=500, seed=SEED)
    assert first == second
    assert first.p_value == pytest.approx(second.p_value)
    assert first.seed == SEED and first.permutations == 500


def test_permutation_detects_strong_signal_and_ignores_noise() -> None:
    rng = np.random.default_rng(7)
    base = rng.normal(size=60)
    strong = 3.0 * base + rng.normal(scale=0.3, size=60)
    assert permutation_test(base, strong, permutations=2000, seed=SEED).p_value < 0.01

    independent = rng.normal(size=60)
    assert permutation_test(base, independent, permutations=2000, seed=SEED).p_value > 0.05


def test_permutation_p_value_bounds() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=12)
    y = rng.normal(size=12)
    result = permutation_test(x, y, permutations=200, seed=SEED)
    assert 1 / 201 <= result.p_value <= 1.0


def test_mean_difference_permutation() -> None:
    left = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0]
    right = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    result = permutation_test(left, right, statistic="mean_difference", permutations=500, seed=SEED)
    assert result.statistic == pytest.approx(9.0)
    assert result.p_value < 0.01
    assert result.statistic_kind == "mean_difference"


def test_small_sample_is_rejected_as_insufficient_evidence() -> None:
    with pytest.raises(InferenceError, match="样本量不足"):
        permutation_test([1.0, 2.0, 3.0], [1.0, 2.0, 4.0], permutations=200, seed=SEED)


def test_too_few_permutations_rejected() -> None:
    with pytest.raises(InferenceError, match="置换次数过少"):
        permutation_test(list(range(20)), list(range(20)), permutations=50, seed=SEED)


def test_unknown_statistic_rejected() -> None:
    with pytest.raises(InferenceError, match="未知的统计量口径"):
        permutation_test(list(range(20)), list(range(20)), statistic="ttest", seed=SEED)


def test_effect_sizes() -> None:
    close = standardized_difference([10.0, 11.0, 12.0], [10.5, 11.5, 12.5])
    far = standardized_difference([30.0, 31.0, 32.0], [10.0, 11.0, 12.0])
    assert abs(close) < abs(far)
    assert far > 0
    assert absolute_risk_difference(0.35, 0.10) == pytest.approx(0.25)
    with pytest.raises(InferenceError):
        absolute_risk_difference(1.4, 0.1)


def test_direction_consistency() -> None:
    assert direction_consistency([1, 2, 3, -1]) == pytest.approx(0.75)
    assert direction_consistency([1, -1]) == pytest.approx(0.5)
    assert direction_consistency([0, 0]) == 0.0


def test_significance_score_bands() -> None:
    assert significance_score(0.005) == 1.0
    assert significance_score(0.03) == 0.7
    assert significance_score(0.08) == 0.4
    assert significance_score(0.5) == 0.0
    with pytest.raises(InferenceError):
        significance_score(1.5)


def test_effect_score_mapping() -> None:
    assert effect_score(0.05) == pytest.approx(0.2)
    assert effect_score(0.2) == pytest.approx(0.2)
    assert effect_score(0.8) == pytest.approx(1.0)
    assert effect_score(3.0) == pytest.approx(1.0)
    midpoint = effect_score(0.5)
    assert 0.2 < midpoint < 1.0
    assert effect_score(-0.8) == pytest.approx(1.0), "效应量看绝对值，方向由符号单独表达"


def test_coverage_score_caps_at_one() -> None:
    assert coverage_score(21, 21) == pytest.approx(1.0)
    assert coverage_score(30, 21) == pytest.approx(1.0)
    assert coverage_score(10.5, 21) == pytest.approx(0.5)
    with pytest.raises(InferenceError):
        coverage_score(5, 0)


def test_composite_confidence_matches_spec_weights() -> None:
    breakdown = composite_confidence(p_value=0.004, effect=0.9, covered_days=21, required_days=21)
    assert breakdown.as_dict() == {
        "significance_score": 1.0,
        "effect_score": 1.0,
        "coverage_score": 1.0,
        "total": 1.0,
    }
    weak = composite_confidence(p_value=0.4, effect=0.01, covered_days=5, required_days=21)
    assert weak.total < 0.6
    assert weak.meets(0.6) is False
    assert breakdown.meets(0.6) is True


def test_composite_confidence_boundary_is_deterministic() -> None:
    """阈值判定必须稳定：同样的输入每次给出同样的结论。"""

    first = composite_confidence(p_value=0.03, effect=0.5, covered_days=21, required_days=21)
    second = composite_confidence(p_value=0.03, effect=0.5, covered_days=21, required_days=21)
    assert first == second
