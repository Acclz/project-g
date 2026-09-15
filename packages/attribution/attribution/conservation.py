"""守恒断言：全项目唯一的浮点比较入口。

需求依据：docs/00-需求说明书.md §5.2 —— 每层强制
    |Σ贡献 − Δ目标| / max(|Δ目标|, 1) < 1e-9
失败即视为实现缺陷，不允许"约等于"通过。
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_TOLERANCE = 1e-9


class ConservationError(ArithmeticError):
    """守恒断言失败。"""

    def __init__(self, residual: float, relative: float, tolerance: float) -> None:
        self.residual = residual
        self.relative = relative
        self.tolerance = tolerance
        super().__init__(
            f"守恒断言失败：残差={residual:.12g}，相对误差={relative:.12g}，容差={tolerance:g}"
        )


def conservation_residual(contributions: Sequence[float], delta: float) -> tuple[float, float]:
    """返回 (绝对残差, 相对误差)。相对误差以 max(|Δ|, 1) 为分母，避免小基数放大。"""
    residual = abs(sum(contributions) - delta)
    relative = residual / max(abs(delta), 1.0)
    return residual, relative


def assert_conservation(
    contributions: Sequence[float],
    delta: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> float:
    """校验 Σ贡献 == Δ目标，返回**实际残差**（写入接口响应与报告）。"""
    residual, relative = conservation_residual(contributions, delta)
    if relative >= tolerance:
        raise ConservationError(residual=residual, relative=relative, tolerance=tolerance)
    return residual
