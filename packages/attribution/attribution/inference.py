"""统计检验与置信度合成（纯算法，无 IO、无框架依赖）。

需求依据：`docs/01-技术规格.md` §5.7（统计检验）、§5.8（置信度合成）；
`docs/00-需求说明书.md` §5.5（置信度三部分）、§5.6（伪相关四步）。

统一约定（这几条是"可复现"的地基）：

* 任何随机过程都必须显式传种子，同一输入必须给出完全一致的输出；
* p 值口径固定为 (1 + 比观测更极端的置换次数) / (1 + 置换次数)，不允许换写法；
* 效应量按量的类型选口径：连续量用标准化差异（Cohen's d 形式），比率量用绝对风险差；
* 置信度权重固定 0.40 / 0.35 / 0.25；低于阈值时由调用方写入排除理由，禁止静默丢弃。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

DEFAULT_SEED = 20260915
DEFAULT_PERMUTATIONS = 10_000
#: 样本量下限：低于它直接判定"证据不足"（技术规格 §5.9 第 4 条）
MIN_SAMPLE_SIZE = 8
#: 显著性得分分档（p 值上界 → 得分），按顺序取第一条命中
SIGNIFICANCE_BANDS: tuple[tuple[float, float], ...] = ((0.01, 1.0), (0.05, 0.7), (0.10, 0.4))
#: 置信度三部分权重（技术规格 §5.8）
CONFIDENCE_WEIGHTS: dict[str, float] = {"significance": 0.40, "effect": 0.35, "coverage": 0.25}
#: 效应量分档默认阈值：小于等于 small 记 0.2 分，大于等于 large 记满分，中间线性映射
DEFAULT_EFFECT_SMALL = 0.2
DEFAULT_EFFECT_LARGE = 0.8
EFFECT_FLOOR = 0.2


class InferenceError(ValueError):
    """统计检验的前置条件不满足（样本量过小、方差为 0、口径写错等）。"""


@dataclass(frozen=True)
class PermutationResult:
    """置换检验结果：统计量、p 值、置换次数、样本量（全部落库，便于复现）。"""

    statistic: float
    p_value: float
    permutations: int
    seed: int
    sample_size: int
    statistic_kind: str

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05

    def as_dict(self) -> dict[str, Any]:
        return {
            "statistic": self.statistic,
            "statistic_kind": self.statistic_kind,
            "p_value": self.p_value,
            "permutations": self.permutations,
            "seed": self.seed,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """置信度三部分与总分：报告里必须逐项展示，不能只给一个数字。"""

    significance: float
    effect: float
    coverage: float

    @property
    def total(self) -> float:
        return (
            CONFIDENCE_WEIGHTS["significance"] * self.significance
            + CONFIDENCE_WEIGHTS["effect"] * self.effect
            + CONFIDENCE_WEIGHTS["coverage"] * self.coverage
        )

    def meets(self, threshold: float) -> bool:
        """是否达到阈值（默认 0.6）。这是配置驱动的业务门槛，与守恒断言无关。"""

        return self.total >= threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "significance_score": round(self.significance, 4),
            "effect_score": round(self.effect, 4),
            "coverage_score": round(self.coverage, 4),
            "total": round(self.total, 4),
        }


def _as_array(values: Sequence[float], name: str) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise InferenceError(f"{name} 必须是非空一维序列")
    if not np.all(np.isfinite(array)):
        raise InferenceError(f"{name} 含非有限值（NaN/Inf），请先清洗数据")
    return array


def _check_sample_size(*arrays: np.ndarray, minimum: int = MIN_SAMPLE_SIZE) -> None:
    for array in arrays:
        if array.size < minimum:
            raise InferenceError(
                f"样本量不足（{array.size} < {minimum}）：按技术规格 §5.9 第 4 条应判为证据不足"
            )


def ranks(values: Sequence[float]) -> np.ndarray:
    """平均秩（并列值取平均），Spearman 与秩检验共用。"""

    array = _as_array(values, "values")
    order = np.argsort(array, kind="stable")
    result = np.empty(array.size, dtype=float)
    result[order] = np.arange(1, array.size + 1, dtype=float)
    sorted_values = array[order]
    start = 0
    while start < sorted_values.size:
        stop = start
        while stop + 1 < sorted_values.size and sorted_values[stop + 1] == sorted_values[start]:
            stop += 1
        if stop > start:
            average = (start + stop + 2) / 2
            result[order[start : stop + 1]] = average
        start = stop + 1
    return result


def spearman_correlation(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman 秩相关：先取秩再做 Pearson。不假设正态分布（技术规格 §5.7）。"""

    left = _as_array(x, "x")
    right = _as_array(y, "y")
    if left.size != right.size:
        raise InferenceError(f"x 与 y 长度必须一致（{left.size} vs {right.size}）")
    _check_sample_size(left, right, minimum=3)
    rank_x = ranks(left)
    rank_y = ranks(right)
    std_x = float(np.std(rank_x, ddof=0))
    std_y = float(np.std(rank_y, ddof=0))
    if std_x == 0 or std_y == 0:
        raise InferenceError("有一侧取值完全恒定，秩相关没有定义")
    covariance = float(np.mean((rank_x - rank_x.mean()) * (rank_y - rank_y.mean())))
    return covariance / (std_x * std_y)


def permutation_test(
    x: Sequence[float],
    y: Sequence[float],
    *,
    statistic: str = "correlation",
    permutations: int = DEFAULT_PERMUTATIONS,
    seed: int = DEFAULT_SEED,
) -> PermutationResult:
    """置换检验（技术规格 §5.7 的统一口径）。

    ``statistic="correlation"``：x、y 是配对序列（同一天的两个量），检验相关是否显著；
    ``statistic="mean_difference"``：x、y 是两组样本（例如切片内 vs 切片外），检验均值差。
    """

    if permutations < 100:
        raise InferenceError(f"置换次数过少（{permutations}）：至少 100 次才有意义")
    left = _as_array(x, "x")
    right = _as_array(y, "y")
    generator = np.random.default_rng(seed)

    if statistic == "correlation":
        if left.size != right.size:
            raise InferenceError("相关检验要求 x 与 y 配对且长度一致")
        _check_sample_size(left, right)
        observed = spearman_correlation(left, right)
        extreme = 0
        for _ in range(permutations):
            shuffled = generator.permutation(right)
            try:
                candidate = spearman_correlation(left, shuffled)
            except InferenceError:
                continue
            if abs(candidate) >= abs(observed):
                extreme += 1
        sample_size = int(left.size)
    elif statistic == "mean_difference":
        _check_sample_size(left, right)
        observed = float(left.mean() - right.mean())
        pooled = np.concatenate([left, right])
        size_left = left.size
        extreme = 0
        for _ in range(permutations):
            shuffled = generator.permutation(pooled)
            candidate = float(shuffled[:size_left].mean() - shuffled[size_left:].mean())
            if abs(candidate) >= abs(observed):
                extreme += 1
        sample_size = int(pooled.size)
    else:
        raise InferenceError(f"未知的统计量口径：{statistic}")

    p_value = (1 + extreme) / (1 + permutations)
    return PermutationResult(
        statistic=observed,
        p_value=p_value,
        permutations=permutations,
        seed=seed,
        sample_size=sample_size,
        statistic_kind=statistic,
    )


def standardized_difference(group_a: Sequence[float], group_b: Sequence[float]) -> float:
    """连续量的效应量：标准化均值差（合并标准差），符号表示方向。"""

    a = _as_array(group_a, "group_a")
    b = _as_array(group_b, "group_b")
    _check_sample_size(a, b, minimum=3)
    variance_a = float(np.var(a, ddof=1))
    variance_b = float(np.var(b, ddof=1))
    pooled = np.sqrt(
        ((a.size - 1) * variance_a + (b.size - 1) * variance_b) / (a.size + b.size - 2)
    )
    if pooled == 0:
        raise InferenceError("两组方差都为 0，标准化差异没有定义")
    return float((a.mean() - b.mean()) / pooled)


def absolute_risk_difference(rate_a: float, rate_b: float) -> float:
    """比率量的效应量：绝对风险差（两个比率直接相减，不放大成倍数）。"""

    for rate in (rate_a, rate_b):
        if not 0.0 <= rate <= 1.0:
            raise InferenceError(f"比率必须落在 [0, 1]：{rate}")
    return rate_a - rate_b


def direction_consistency(values: Sequence[float]) -> float:
    """反例检查用：一组变化方向里"与多数方向一致"的比例（0.5 表示完全分裂）。"""

    array = _as_array(values, "values")
    positive = int(np.sum(array > 0))
    negative = int(np.sum(array < 0))
    total = positive + negative
    if total == 0:
        return 0.0
    return max(positive, negative) / total


def significance_score(p_value: float) -> float:
    """显著性得分（技术规格 §5.8 的分档表）。"""

    if p_value < 0.0 or p_value > 1.0:
        raise InferenceError(f"p 值必须在 [0, 1]：{p_value}")
    for upper, score in SIGNIFICANCE_BANDS:
        if p_value < upper:
            return score
    return 0.0


def effect_score(
    effect: float, *, small: float = DEFAULT_EFFECT_SMALL, large: float = DEFAULT_EFFECT_LARGE
) -> float:
    """效应量得分：绝对值从 small 到 large 线性映射到 0.2~1.0，超过 large 记满分。"""

    if small <= 0 or large <= small:
        raise InferenceError(f"效应量阈值不合法：small={small}, large={large}")
    magnitude = abs(float(effect))
    if magnitude <= small:
        return EFFECT_FLOOR
    if magnitude >= large:
        return 1.0
    ratio = (magnitude - small) / (large - small)
    return EFFECT_FLOOR + ratio * (1.0 - EFFECT_FLOOR)


def coverage_score(covered_days: float, required_days: float) -> float:
    """数据覆盖度：实际覆盖天数 ÷ 要求覆盖天数，上限 1.0。"""

    if required_days <= 0:
        raise InferenceError(f"要求覆盖天数必须为正：{required_days}")
    return min(1.0, max(0.0, float(covered_days) / float(required_days)))


def composite_confidence(
    *,
    p_value: float,
    effect: float,
    covered_days: float,
    required_days: float,
    effect_small: float = DEFAULT_EFFECT_SMALL,
    effect_large: float = DEFAULT_EFFECT_LARGE,
) -> ConfidenceBreakdown:
    """置信度合成：0.40×显著性 + 0.35×效应量 + 0.25×数据覆盖度（技术规格 §5.8）。"""

    return ConfidenceBreakdown(
        significance=significance_score(p_value),
        effect=effect_score(effect, small=effect_small, large=effect_large),
        coverage=coverage_score(covered_days, required_days),
    )


__all__ = [
    "CONFIDENCE_WEIGHTS",
    "DEFAULT_EFFECT_LARGE",
    "DEFAULT_EFFECT_SMALL",
    "DEFAULT_PERMUTATIONS",
    "DEFAULT_SEED",
    "MIN_SAMPLE_SIZE",
    "ConfidenceBreakdown",
    "InferenceError",
    "PermutationResult",
    "absolute_risk_difference",
    "composite_confidence",
    "coverage_score",
    "direction_consistency",
    "effect_score",
    "permutation_test",
    "ranks",
    "significance_score",
    "spearman_correlation",
    "standardized_difference",
]
