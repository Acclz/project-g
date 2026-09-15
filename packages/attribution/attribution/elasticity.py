"""弹性估计与参数曲线（技术规格 §5.6、需求说明书 §5.8）。

纯算法模块：无 IO、无框架依赖。它只回答一个问题——"把某个可干预因子调 X%，
目标指标大概会怎么动"，并且**只给区间，不给精确承诺**。

口径（写死在代码里，避免每个调用方各解释一遍）：

* **主路径**：对数—对数最小二乘 ``ln(Y) = a + e·ln(x)``，斜率 e 就是弹性；要求观测点 ≥ 60
  （技术规格 §5.6）。x、y 必须严格为正，否则对数没有定义——不硬凑，直接走降级路径。
* **降级路径**：样本不足时用前后半段均值算有限差分 ``e = (ΔY/Y) ÷ (Δx/x)``，
  区间按"点估计 ± 50%"放宽，并把把握度压到 0.5 以下（降级可以，假装精确不行）。
* **区间**：bootstrap 百分位区间（固定种子，默认 1000 次，默认 90%），同一输入必须给出
  完全一致的输出（需求说明书 §5.8「可复现」）。
* **把握度**：样本量、拟合优度（r²）、区间相对宽度三项加权，逐项都留在结果里，不是拍一个数字。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .inference import DEFAULT_SEED

#: 主路径要求的最少观测点（技术规格 §5.6）
MIN_OBSERVATIONS = 60
#: bootstrap 默认次数与置信水平（技术规格 §5.6）
DEFAULT_ITERATIONS = 1000
DEFAULT_LEVEL = 0.90
#: 区间宽度相对弹性估计值的上限：超过它，把握度按比例打折
WIDE_INTERVAL_RATIO = 1.0
#: 把握度权重（样本量 / 拟合优度 / 区间宽度）
CONFIDENCE_WEIGHTS = {"sample": 0.40, "fit": 0.30, "width": 0.30}
#: 降级路径的把握度上限：样本不足时不许给高把握度
FALLBACK_CONFIDENCE_CEILING = 0.5
#: 弱拟合提示门槛（r² 低于它就提醒"这条曲线只能当参考"）
WEAK_FIT_R2 = 0.30
#: 弱拟合下的把握度上限：拟合差就不许给过半的把握度
WEAK_FIT_CONFIDENCE_CEILING = 0.40


class ElasticityError(ValueError):
    """弹性估计的前置条件不满足（长度不一致、取值非有限、方差为 0 等）。"""


@dataclass(frozen=True)
class ElasticityEstimate:
    """一次弹性估计：点估计、区间、方法与可复现参数（全部落库与进报告）。"""

    value: float
    low: float
    high: float
    method: str
    sample_size: int
    r_squared: float | None
    seed: int
    iterations: int
    level: float
    confidence: float
    confidence_parts: dict[str, float]
    notes: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return self.method != "log_log"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "low": self.low,
            "high": self.high,
            "method": self.method,
            "degraded": self.degraded,
            "sample_size": self.sample_size,
            "r_squared": self.r_squared,
            "seed": self.seed,
            "iterations": self.iterations,
            "level": self.level,
            "confidence": self.confidence,
            "confidence_parts": self.confidence_parts,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class CurvePoint:
    """曲线上的一个点：因子调整幅度 → 目标指标的区间估计。"""

    adjustment: float
    factor_value: float
    expected: float
    low: float
    high: float
    out_of_range: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "adjustment": self.adjustment,
            "factor_value": self.factor_value,
            "expected": self.expected,
            "low": self.low,
            "high": self.high,
            "out_of_range": self.out_of_range,
        }


@dataclass(frozen=True)
class ScenarioCurve:
    """一条参数曲线：基准值 + 弹性 + 各调整档位的区间（区间而非承诺值）。"""

    base_value: float
    factor_base: float
    elasticity: ElasticityEstimate
    points: tuple[CurvePoint, ...]
    max_adjustment: float
    out_of_range: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_value": self.base_value,
            "factor_base": self.factor_base,
            "max_adjustment": self.max_adjustment,
            "out_of_range": self.out_of_range,
            "warnings": list(self.warnings),
            "elasticity": self.elasticity.as_dict(),
            "points": [point.as_dict() for point in self.points],
        }

    def render(self) -> str:
        style = "有限差分（降级）" if self.elasticity.degraded else "对数回归"
        lines = [
            f"基准值 {self.base_value:,.0f}，因子基准 {self.factor_base:,.4f}，"
            f"弹性 {self.elasticity.value:+.3f}"
            f"（{self.elasticity.level:.0%} 区间 {self.elasticity.low:+.3f} ~ "
            f"{self.elasticity.high:+.3f}，{style}，n={self.elasticity.sample_size}，"
            f"把握度 {self.elasticity.confidence:.0%}）"
        ]
        for point in self.points:
            flag = "　⚠超出历史观测区间" if point.out_of_range else ""
            lines.append(
                f"  调 {point.adjustment:+.0%}：因子 {point.factor_value:,.4f} → "
                f"目标 {point.expected:,.0f}"
                f"（区间 {point.low:,.0f} ~ {point.high:,.0f}）{flag}"
            )
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        return "\n".join(lines)


def _as_arrays(x: Sequence[float], y: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    left = np.asarray(list(x), dtype=float)
    right = np.asarray(list(y), dtype=float)
    if left.ndim != 1 or right.ndim != 1:
        raise ElasticityError("x 与 y 必须是一维序列")
    if left.size != right.size:
        raise ElasticityError(f"x 与 y 长度必须一致（{left.size} vs {right.size}）")
    if left.size == 0:
        raise ElasticityError("x 与 y 不能为空")
    if not (np.all(np.isfinite(left)) and np.all(np.isfinite(right))):
        raise ElasticityError("x 与 y 含非有限值（NaN/Inf），请先清洗数据")
    return left, right


def _log_log_fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """对数—对数最小二乘：返回 ``(斜率, 截距, r²)``。"""

    if np.any(x <= 0) or np.any(y <= 0):
        raise ElasticityError("对数—对数回归要求 x 与 y 严格为正")
    log_x = np.log(x)
    log_y = np.log(y)
    variance = float(np.var(log_x))
    if variance == 0:
        raise ElasticityError("x 在观测窗口内没有变化，弹性没有定义（不要用 0 蒙混）")
    slope = float(np.cov(log_x, log_y, bias=True)[0, 1] / variance)
    intercept = float(log_y.mean() - slope * log_x.mean())
    residuals = log_y - (intercept + slope * log_x)
    total = float(np.sum((log_y - log_y.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residuals**2)) / total if total > 0 else 0.0
    return slope, intercept, r_squared


def _bootstrap_slope(x: np.ndarray, y: np.ndarray, *, iterations: int, seed: int) -> np.ndarray:
    """bootstrap 重采样斜率：固定种子，同一输入必得同一结果。"""

    generator = np.random.default_rng(seed)
    slopes: list[float] = []
    size = x.size
    for _ in range(iterations):
        index = generator.integers(0, size, size=size)
        try:
            slope, _, _ = _log_log_fit(x[index], y[index])
        except ElasticityError:
            continue
        if math.isfinite(slope):
            slopes.append(slope)
    return np.asarray(slopes, dtype=float)


def _confidence(
    *, sample_size: int, r_squared: float | None, low: float, high: float, value: float
) -> tuple[float, dict[str, float]]:
    """把握度 = 样本量 + 拟合优度 + 区间宽度；三项都写进结果，便于逐条解释。"""

    sample_part = min(1.0, sample_size / MIN_OBSERVATIONS)
    fit_part = 0.0 if r_squared is None else max(0.0, min(1.0, r_squared))
    width = high - low
    relative_width = width / max(abs(value), 1e-6)
    width_part = 1.0 / (1.0 + max(0.0, relative_width - WIDE_INTERVAL_RATIO))
    total = (
        CONFIDENCE_WEIGHTS["sample"] * sample_part
        + CONFIDENCE_WEIGHTS["fit"] * fit_part
        + CONFIDENCE_WEIGHTS["width"] * width_part
    )
    parts = {
        "sample": round(sample_part, 4),
        "fit": round(fit_part, 4),
        "width": round(width_part, 4),
    }
    return round(total, 4), parts


def finite_difference_elasticity(x: Sequence[float], y: Sequence[float]) -> float:
    """降级口径：用前后半段均值算 ``(ΔY/Y) ÷ (Δx/x)``。"""

    values_x, values_y = _as_arrays(x, y)
    if values_x.size < 4:
        raise ElasticityError(f"有限差分至少需要 4 个观测点：{values_x.size}")
    half = values_x.size // 2
    base_x, current_x = float(values_x[:half].mean()), float(values_x[half:].mean())
    base_y, current_y = float(values_y[:half].mean()), float(values_y[half:].mean())
    if base_x == 0.0 or base_y == 0.0:
        raise ElasticityError("有限差分要求前半段均值非 0（否则相对变化没有定义）")
    relative_x = (current_x - base_x) / base_x
    relative_y = (current_y - base_y) / base_y
    if relative_x == 0.0:
        raise ElasticityError("x 前后半段没有差异，弹性没有定义（不要用 0 蒙混）")
    return relative_y / relative_x


def estimate_elasticity(
    x: Sequence[float],
    y: Sequence[float],
    *,
    min_observations: int = MIN_OBSERVATIONS,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = DEFAULT_LEVEL,
    seed: int = DEFAULT_SEED,
    fallback_note: str = "",
) -> ElasticityEstimate:
    """估计目标指标对某因子的弹性（主路径对数回归，样本不足走有限差分）。"""

    if not 0.0 < level < 1.0:
        raise ElasticityError(f"置信水平必须在 (0,1)：{level}")
    if iterations < 100:
        raise ElasticityError(f"bootstrap 次数过少（{iterations}）：至少 100 次才有意义")
    values_x, values_y = _as_arrays(x, y)
    notes: list[str] = [fallback_note] if fallback_note else []

    usable = values_x.size >= min_observations and np.all(values_x > 0) and np.all(values_y > 0)
    if usable:
        try:
            slope, _, r_squared = _log_log_fit(values_x, values_y)
        except ElasticityError as error:
            notes.append(f"对数回归不可用，改走有限差分：{error}")
        else:
            slopes = _bootstrap_slope(values_x, values_y, iterations=iterations, seed=seed)
            if slopes.size >= iterations // 2:
                alpha = (1.0 - level) / 2.0
                low = float(np.quantile(slopes, alpha))
                high = float(np.quantile(slopes, 1.0 - alpha))
            else:
                low, high = slope * 0.5, slope * 1.5
                notes.append("bootstrap 有效重采样过少，区间按点估计 ±50% 放宽")
            low, high = min(low, slope), max(high, slope)
            confidence, parts = _confidence(
                sample_size=int(values_x.size),
                r_squared=r_squared,
                low=low,
                high=high,
                value=slope,
            )
            if r_squared < WEAK_FIT_R2:
                confidence = min(confidence, WEAK_FIT_CONFIDENCE_CEILING)
                parts["weak_fit_ceiling"] = WEAK_FIT_CONFIDENCE_CEILING
                notes.append(
                    f"拟合偏弱（r²={r_squared:.3f} < {WEAK_FIT_R2}）：曲线只能当方向参考，"
                    f"把握度上限 {WEAK_FIT_CONFIDENCE_CEILING:g}"
                )
            return ElasticityEstimate(
                value=slope,
                low=low,
                high=high,
                method="log_log",
                sample_size=int(values_x.size),
                r_squared=r_squared,
                seed=seed,
                iterations=iterations,
                level=level,
                confidence=confidence,
                confidence_parts=parts,
                notes=tuple(notes),
            )

    if values_x.size < min_observations:
        notes.append(
            f"样本量 {values_x.size} < {min_observations}："
            "按技术规格 §5.6 降级为有限差分，区间放宽"
        )
    else:
        notes.append("存在非正取值，对数没有定义：按技术规格 §5.6 降级为有限差分")
    slope = finite_difference_elasticity(values_x, values_y)
    low, high = slope * 0.5, slope * 1.5
    confidence, parts = _confidence(
        sample_size=int(values_x.size), r_squared=None, low=low, high=high, value=slope
    )
    confidence = min(confidence, FALLBACK_CONFIDENCE_CEILING)
    parts["ceiling"] = FALLBACK_CONFIDENCE_CEILING
    return ElasticityEstimate(
        value=slope,
        low=low,
        high=high,
        method="finite_difference",
        sample_size=int(values_x.size),
        r_squared=None,
        seed=seed,
        iterations=0,
        level=level,
        confidence=confidence,
        confidence_parts=parts,
        notes=tuple(notes),
    )


def simulate_curve(
    *,
    base_value: float,
    factor_base: float,
    elasticity: ElasticityEstimate,
    adjustments: Sequence[float],
    max_adjustment: float = 0.30,
) -> ScenarioCurve:
    """按弹性把"因子调 X%"翻译成目标指标的区间曲线（单因子局部均衡假设）。"""

    if factor_base <= 0:
        raise ElasticityError(f"因子基准值必须为正：{factor_base}")
    if base_value <= 0:
        raise ElasticityError(f"目标指标基准值必须为正：{base_value}")
    if max_adjustment <= 0:
        raise ElasticityError(f"最大调整幅度必须为正：{max_adjustment}")
    if not adjustments:
        raise ElasticityError("参数曲线至少要有一个调整档位")

    points: list[CurvePoint] = []
    for raw in adjustments:
        adjustment = float(raw)
        if not math.isfinite(adjustment):
            raise ElasticityError(f"调整幅度必须是有限数：{raw}")
        if adjustment <= -1.0:
            raise ElasticityError(f"调整幅度不能小于等于 -100%：{adjustment}")
        expected = _apply(base_value, adjustment, elasticity.value)
        # 区间要取三个端点（弹性下界、上界、点估计）的包络：当 (1+调整) < 1 时，
        # "弹性越大目标越小"会让上下界互换，直接按名义顺序取会得到一条假区间。
        candidates = (
            expected,
            _apply(base_value, adjustment, elasticity.low),
            _apply(base_value, adjustment, elasticity.high),
        )
        points.append(
            CurvePoint(
                adjustment=adjustment,
                factor_value=factor_base * (1.0 + adjustment),
                expected=expected,
                # 区间必须包住点估计：浮点的最后一位残差也要兜住（否则曲线自己不自洽）
                low=min(candidates),
                high=max(candidates),
                out_of_range=abs(adjustment) > max_adjustment + 1e-12,
            )
        )

    out_of_range = any(point.out_of_range for point in points)
    warnings: list[str] = []
    if out_of_range:
        warnings.append(
            f"有档位超出历史观测区间（默认 ±{max_adjustment:.0%}）：该档位是外推，"
            "必须显式标注，不能当成预测"
        )
    if elasticity.degraded:
        warnings.append("弹性来自降级路径（有限差分），区间已放宽，把握度有限")
    if elasticity.r_squared is not None and elasticity.r_squared < WEAK_FIT_R2:
        warnings.append(
            f"拟合偏弱（r²={elasticity.r_squared:.3f}）：只能看方向与量级，不能当承诺值"
        )
    return ScenarioCurve(
        base_value=base_value,
        factor_base=factor_base,
        elasticity=elasticity,
        points=tuple(points),
        max_adjustment=max_adjustment,
        out_of_range=out_of_range,
        warnings=tuple(warnings),
    )


def _apply(base_value: float, adjustment: float, elasticity: float) -> float:
    """目标 = 基准 × (1+调整)^弹性（弹性为正表示同向，为负表示反向）。"""

    return float(base_value) * (1.0 + adjustment) ** elasticity


__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_LEVEL",
    "FALLBACK_CONFIDENCE_CEILING",
    "MIN_OBSERVATIONS",
    "WEAK_FIT_CONFIDENCE_CEILING",
    "WEAK_FIT_R2",
    "CurvePoint",
    "ElasticityError",
    "ElasticityEstimate",
    "ScenarioCurve",
    "estimate_elasticity",
    "finite_difference_elasticity",
    "simulate_curve",
]
