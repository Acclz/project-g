"""弹性估计：对数回归、bootstrap 区间、降级路径与"只给区间不给承诺"（技术规格 §5.6）。"""

import numpy as np
import pytest

from attribution.elasticity import (
    FALLBACK_CONFIDENCE_CEILING,
    WEAK_FIT_CONFIDENCE_CEILING,
    ElasticityError,
    estimate_elasticity,
    finite_difference_elasticity,
    simulate_curve,
)


def _power_law(exponent: float, *, size: int = 90, seed: int = 7) -> tuple[list, list]:
    rng = np.random.default_rng(seed)
    x = np.exp(rng.normal(0.0, 0.35, size=size))
    y = 1234.0 * x**exponent
    return list(x), list(y)


def test_log_log_recovers_exact_power_law():
    x, y = _power_law(-1.35)
    estimate = estimate_elasticity(x, y)
    assert estimate.method == "log_log"
    assert estimate.sample_size == 90
    assert estimate.value == pytest.approx(-1.35, abs=1e-9)
    assert estimate.r_squared == pytest.approx(1.0, abs=1e-9)
    # 完美幂律下 bootstrap 重采样仍是同一条直线：区间必须收在点估计上
    assert estimate.low == pytest.approx(-1.35, abs=1e-6)
    assert estimate.high == pytest.approx(-1.35, abs=1e-6)
    assert estimate.confidence > 0.9


def test_estimate_is_deterministic_for_same_input():
    x, y = _power_law(0.8, seed=11)
    first = estimate_elasticity(x, y)
    second = estimate_elasticity(x, y)
    assert first.as_dict() == second.as_dict(), "同一输入必须给出完全一致的输出（§5.8）"


def test_noise_widens_the_interval():
    rng = np.random.default_rng(3)
    x = np.exp(rng.normal(0.0, 0.4, size=80))
    y = 500.0 * x**0.6 * np.exp(rng.normal(0.0, 0.25, size=80))
    estimate = estimate_elasticity(list(x), list(y))
    assert estimate.method == "log_log"
    assert estimate.low < estimate.value < estimate.high
    assert estimate.confidence < 1.0


def test_weak_fit_caps_confidence_and_warns():
    """拟合很弱时不许给过半把握度：宁可用不上，也不能把噪声说成规律。"""

    rng = np.random.default_rng(5)
    x = np.exp(rng.normal(0.0, 0.4, size=90))
    y = 1_000.0 * np.exp(rng.normal(0.0, 0.5, size=90))  # 与 x 无关
    estimate = estimate_elasticity(list(x), list(y))
    assert estimate.r_squared is not None and estimate.r_squared < 0.30
    assert estimate.confidence <= WEAK_FIT_CONFIDENCE_CEILING
    assert any("拟合偏弱" in note for note in estimate.notes)


def test_small_sample_degrades_to_finite_difference():
    x, y = _power_law(-0.5, size=20)
    estimate = estimate_elasticity(x, y)
    assert estimate.method == "finite_difference"
    assert estimate.degraded is True
    assert estimate.sample_size == 20
    assert estimate.confidence <= FALLBACK_CONFIDENCE_CEILING
    assert any("降级" in note for note in estimate.notes)
    # 区间是放宽的 ±50%，不是冒充精确值
    assert estimate.low == pytest.approx(estimate.value * 0.5)
    assert estimate.high == pytest.approx(estimate.value * 1.5)


def test_non_positive_values_fall_back_instead_of_crashing():
    x = [float(value) for value in range(1, 91)]
    y = [float(value) - 45.0 for value in range(1, 91)]  # 前半段有负值
    estimate = estimate_elasticity(x, y)
    assert estimate.method == "finite_difference"
    assert any("非正" in note for note in estimate.notes)


def test_flat_factor_has_no_elasticity():
    x = [5.0] * 80
    y = [float(value) for value in range(80)]
    with pytest.raises(ElasticityError):
        estimate_elasticity(x, y)
    with pytest.raises(ElasticityError):
        finite_difference_elasticity([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0])


def test_shape_and_configuration_guards():
    with pytest.raises(ElasticityError):
        estimate_elasticity([1.0, 2.0], [1.0])
    with pytest.raises(ElasticityError):
        estimate_elasticity([1.0, float("nan")], [1.0, 2.0])
    with pytest.raises(ElasticityError):
        estimate_elasticity([1.0, 2.0], [1.0, 2.0], iterations=10)
    with pytest.raises(ElasticityError):
        estimate_elasticity([1.0, 2.0], [1.0, 2.0], level=1.5)
    with pytest.raises(ElasticityError):
        finite_difference_elasticity([1.0, 2.0], [1.0, 2.0])


def test_curve_gives_ranges_and_flags_out_of_range():
    x, y = _power_law(-1.0)
    estimate = estimate_elasticity(x, y)
    curve = simulate_curve(
        base_value=1_000_000.0,
        factor_base=50.0,
        elasticity=estimate,
        adjustments=[0.0, -0.1, 0.1, 0.5],
        max_adjustment=0.30,
    )
    assert curve.out_of_range is True, "±30% 之外的档位必须被标出来"
    assert any("超出历史观测区间" in warning for warning in curve.warnings)
    flat = next(point for point in curve.points if point.adjustment == 0.0)
    assert flat.expected == pytest.approx(1_000_000.0)
    down = next(point for point in curve.points if point.adjustment == -0.1)
    # 弹性为 -1：因子降 10% ⇒ 目标升约 11.1%
    assert down.expected == pytest.approx(1_000_000.0 / 0.9, rel=1e-9)
    # 完美幂律下区间退化到点估计附近（区间宽度为 0，容差取相对误差）
    assert down.low <= down.high
    assert down.low == pytest.approx(down.expected, rel=1e-6)
    assert down.high == pytest.approx(down.expected, rel=1e-6)
    assert down.factor_value == pytest.approx(45.0)
    assert down.out_of_range is False
    over = next(point for point in curve.points if point.adjustment == 0.5)
    assert over.out_of_range is True


def test_interval_is_the_envelope_not_the_nominal_order():
    """弹性为负时，(1+调整)<1 会让上下界互换：区间必须取三端点包络，不能按名义顺序拼。"""

    rng = np.random.default_rng(9)
    x = np.exp(rng.normal(0.0, 0.4, size=80))
    y = 800.0 * x**-0.9 * np.exp(rng.normal(0.0, 0.2, size=80))
    estimate = estimate_elasticity(list(x), list(y))
    assert estimate.value < 0 and estimate.low < estimate.high
    curve = simulate_curve(
        base_value=500_000.0,
        factor_base=30.0,
        elasticity=estimate,
        adjustments=[-0.2, 0.2],
    )
    for point in curve.points:
        assert point.low <= point.expected <= point.high
        expected_low = min(
            500_000.0 * (1 + point.adjustment) ** estimate.low,
            500_000.0 * (1 + point.adjustment) ** estimate.high,
            point.expected,
        )
        assert point.low == pytest.approx(expected_low, rel=1e-12)


def test_curve_rejects_impossible_inputs():
    x, y = _power_law(1.2)
    estimate = estimate_elasticity(x, y)
    with pytest.raises(ElasticityError):
        simulate_curve(
            base_value=0.0, factor_base=10.0, elasticity=estimate, adjustments=[0.1]
        )
    with pytest.raises(ElasticityError):
        simulate_curve(
            base_value=10.0, factor_base=10.0, elasticity=estimate, adjustments=[]
        )
    with pytest.raises(ElasticityError):
        simulate_curve(
            base_value=10.0, factor_base=10.0, elasticity=estimate, adjustments=[-1.0]
        )
