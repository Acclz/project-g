"""LMDI：乘法结构（Y = Π xᵢ）的分解，保证交叉残差为 0。

需求依据：docs/00-需求说明书.md §5.2、docs/01-技术规格.md §5.2。
公式：ΔY = Σ L(Y₁,Y₀) · ln(xᵢ₁/xᵢ₀)，L(a,b) = (a−b)/(ln a − ln b)，L(a,a) = a。
零值预案：A 替换为 ε 并标记 zero_substituted；B 由调用方改用分步差分（method=staged_diff）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .conservation import assert_conservation

EPSILON_FLOOR = 1e-9


class LmdiError(ValueError):
    """LMDI 前置条件不满足（因子非正、结构不可乘等）。"""


def _log_mean(a: float, b: float) -> float:
    """对数平均 L(a,b)；a == b 时取极限值 a。"""
    if a == b:
        return a
    if a <= 0 or b <= 0:
        raise LmdiError(f"对数平均要求参数为正：a={a}, b={b}")
    return (a - b) / (math.log(a) - math.log(b))


def lmdi_contributions(
    base: Mapping[str, float],
    current: Mapping[str, float],
    total_base: float,
    total_current: float,
    *,
    tolerance: float = 1e-9,
    zero_policy: str = "error",
    zero_replacements: Mapping[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, object]]:
    """乘法结构分解。

    参数
    ----
    base / current: 各因子在基期与现期的取值（必须全部 > 0，或按 zero_policy 处理）
    total_base / total_current: 目标指标在两期的总量（用于对数平均权重）
    zero_policy: "error"（默认，直接报错并引导调用方改用分步差分）或
        "substitute"（替换为零值 ε；**此时总量会按替换后的因子重算**，并在 meta 中
        同时给出原始总量与重算总量，避免"悄悄改掉目标值"）
    zero_replacements: 显式指定零值替换量（默认 ε = max(1e-9, 该因子非零最小值的 1%)）

    返回
    ----
    (contributions, meta)：contributions 为 {因子: 贡献量}；meta 含 residual、
    zero_substituted（是否发生替换）、substituted_factors（被替换的因子列表）、
    totals_recomputed（总量是否按替换后因子重算）、original_total_base/current。
    """
    if set(base) != set(current):
        raise LmdiError("base 与 current 的因子集合不一致")

    adjusted_base = {k: float(v) for k, v in base.items()}
    adjusted_current = {k: float(v) for k, v in current.items()}
    substituted: list[str] = []

    for factor in adjusted_base:
        for store in (adjusted_base, adjusted_current):
            value = store[factor]
            if value > 0:
                continue
            if zero_policy == "error":
                raise LmdiError(f"因子 {factor} 取值非正（{value}），zero_policy=error")
            replacement = (zero_replacements or {}).get(factor)
            if replacement is None:
                positive = [
                    abs(v) for v in (adjusted_base[factor], adjusted_current[factor]) if abs(v) > 0
                ]
                floor = min(positive) * 0.01 if positive else 1.0
                replacement = max(EPSILON_FLOOR, floor)
            store[factor] = float(replacement)
            if factor not in substituted:
                substituted.append(factor)

    totals_recomputed = False
    original_total_base, original_total_current = total_base, total_current
    if substituted:
        # 替换零值等价于"把该因子看作 ε"，因此总量必须按替换后的因子重算，
        # 否则守恒断言不可能通过（这会掩盖口径问题，属于必须显式暴露的事实）。
        total_base = math.prod(adjusted_base.values())
        total_current = math.prod(adjusted_current.values())
        totals_recomputed = True

    if total_base <= 0 or total_current <= 0:
        raise LmdiError(
            "LMDI 要求目标指标两期均为正（目标值为 0 或负数时请改用分步差分 staged_diff）"
        )

    factors: Sequence[str] = list(adjusted_base)
    weight = _log_mean(total_current, total_base)
    contributions: dict[str, float] = {}
    for factor in factors:
        ratio = adjusted_current[factor] / adjusted_base[factor]
        contributions[factor] = weight * math.log(ratio)

    delta = total_current - total_base
    residual = assert_conservation(list(contributions.values()), delta, tolerance)
    meta: dict[str, object] = {
        "residual": residual,
        "zero_substituted": bool(substituted),
        "substituted_factors": substituted,
        "totals_recomputed": totals_recomputed,
        "original_total_base": original_total_base,
        "original_total_current": original_total_current,
        "recomputed_total_base": total_base,
        "recomputed_total_current": total_current,
    }
    return contributions, meta
