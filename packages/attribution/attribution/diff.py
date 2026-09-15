"""差额分析：加法结构（Y = Σ xᵢ）的精确分解。

需求依据：docs/00-需求说明书.md §5.2、docs/01-技术规格.md §5.1。
数学性质：ΔY = Σ Δxᵢ，天然守恒（无交叉残差）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .conservation import assert_conservation


def additive_contributions(
    base: Mapping[str, float],
    current: Mapping[str, float],
    tolerance: float = 1e-9,
) -> dict[str, float]:
    """加法结构分解，返回 {因子: 贡献量}。

    base/current 的键集合必须一致（缺项按 0 处理会掩盖数据问题，因此直接报错）。
    当 ΔY == 0 时仍返回各因子贡献（此时调用方不应计算贡献率）。
    """
    if set(base) != set(current):
        missing = (set(base) ^ set(current))
        raise ValueError(f"base 与 current 的因子集合不一致：{sorted(missing)}")
    contributions = {key: float(current[key]) - float(base[key]) for key in base}
    delta = sum(float(v) for v in current.values()) - sum(float(v) for v in base.values())
    # 加法结构下守恒是恒等式，断言在此只起"实现自检"作用
    assert_conservation(list(contributions.values()), delta, tolerance)
    return contributions


def contribution_rates(
    contributions: Mapping[str, float],
    delta: float,
    *,
    undefined_marker: float | None = None,
) -> dict[str, float | None]:
    """贡献率 = 因子贡献 ÷ 总变动。

    ΔY == 0 时贡献率**没有定义**（分母为 0），返回 undefined_marker（默认 None），
    调用方必须据此在报告中说明"只输出绝对贡献"，不允许用 0 或近似值蒙混。
    """
    if delta == 0:
        return {key: undefined_marker for key in contributions}
    return {key: value / delta for key, value in contributions.items()}
