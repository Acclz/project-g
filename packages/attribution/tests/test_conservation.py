"""守恒断言：全项目唯一的浮点比较入口（需求 §5.2、E2）。"""

import pytest

from attribution.conservation import (
    ConservationError,
    assert_conservation,
    conservation_residual,
)


def test_passes_on_exact_sum():
    assert assert_conservation([1.5, -0.5, 2.0], 3.0) < 1e-12


def test_returns_actual_residual():
    residual = assert_conservation([3.0, 7.0], 10.0)
    assert residual == pytest.approx(0.0, abs=1e-12)


def test_raises_on_visible_mismatch():
    with pytest.raises(ConservationError) as excinfo:
        assert_conservation([3.0, 6.0], 10.0)
    assert excinfo.value.residual == pytest.approx(1.0)


def test_relative_denominator_protects_small_delta():
    # 相对误差分母固定为 max(|Δ|, 1)：小基数不会被放大成假缺陷
    assert_conservation([0.00005, 0.00005], 0.0001)
    # Δ=0 时残差 5e-10 属于浮点噪声，按契约（相对误差 < 1e-9）通过
    assert_conservation([5e-10, 0.0], 0.0)
    # 但真的对不上（1e-6）时必须报错：不许用"分母很大"糊过去
    with pytest.raises(ConservationError):
        assert_conservation([1e-6, 0.0], 0.0)


def test_tolerance_is_configurable():
    with pytest.raises(ConservationError):
        assert_conservation([1.0], 1.0001, tolerance=1e-9)
    assert_conservation([1.0], 1.0001, tolerance=1e-3)


def test_residual_helper_matches_assertion():
    residual, relative = conservation_residual([2.0, 2.0], 3.0)
    assert residual == pytest.approx(1.0)
    assert relative == pytest.approx(1.0 / 3.0)
