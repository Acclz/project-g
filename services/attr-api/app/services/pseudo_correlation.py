"""伪相关四步检查（技术规格 §5.9、需求说明书 §5.6）。

四步的作用是把"两个量一起动"和"一个引起另一个"区分开：

1. **控制变量**：在关键维度切片内部重跑同一检验——切片内不显著 ⇒ 判为共同驱动；
2. **时间先行性**：原因必须早于或同步于结果，原因晚于结果 ⇒ 置信度下调 0.3；
3. **安慰剂维度**：在"理论上不应成立"的维度上做同样检验——同样显著 ⇒ 判为趋势性共同驱动；
4. **样本充分性**：样本量 < 60 或覆盖 < 21 天 ⇒ 结论降级为"证据不足"，不计入主结论。

工程近似（如实写在代码里，不藏在注释里）：

* "首次显著变动时点"用"当日取值偏离窗口均值超过 1.5 个标准差的首日"作判据；
* "最相关子切片"取子维度里均值差绝对值最大的那个，最多探 3 个候选以控制沙箱调用次数。

这两条都是可复算的确定性规则，且**只影响置信度调整与排除理由**，不影响贡献额分解。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.config import Settings
from app.sandbox.runner import SandboxRunner
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.hypotheses import INSUFFICIENT_SAMPLE_SIZE, Hypothesis, HypothesisResult
from attribution import InferenceError, permutation_test, standardized_difference

TIME_PRECEDENCE_PENALTY = 0.3
DEVIATION_SIGMA = 1.5
MAX_SUB_PROBES = 3
CONTROL_VARIABLE_ALPHA = 0.10
PLACEBO_ALPHA = 0.05
#: 安慰剂判据：不仅要求显著，还要求"强度相当"（安慰剂效应 ≥ 假设切片效应的 80%）
PLACEBO_EFFECT_RATIO = 0.8
MIN_SAMPLE_SIZE = INSUFFICIENT_SAMPLE_SIZE


@dataclass
class StepOutcome:
    """一步检查的结论：是否执行、判成什么、依据是什么、对置信度的影响。"""

    step: int
    name: str
    executed: bool
    verdict: str
    detail: str
    penalty: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "name": self.name,
            "executed": self.executed,
            "verdict": self.verdict,
            "detail": self.detail,
            "penalty": self.penalty,
        }


@dataclass
class PseudoCorrelationVerdict:
    """四步检查的汇总：结论、置信度调整总量与理由（每条排除都必须有理由）。"""

    factor: str
    verdict: str
    steps: list[StepOutcome] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def penalty_total(self) -> float:
        return sum(step.penalty for step in self.steps)

    @property
    def excluded(self) -> bool:
        return self.verdict in ("共同驱动", "趋势性共同驱动", "证据不足")

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "verdict": self.verdict,
            "penalty_total": round(self.penalty_total, 4),
            "reasons": self.reasons,
            "steps": [step.as_dict() for step in self.steps],
        }


class PseudoCorrelationChecker:
    """四步检查的执行者：所有取数都经沙箱，统计口径复用算法包。"""

    def __init__(self, engine: MetricEngine, sandbox: SandboxRunner, settings: Settings) -> None:
        self.engine = engine
        self.sandbox = sandbox
        self.settings = settings

    def check(
        self,
        hypothesis: Hypothesis,
        result: HypothesisResult,
        *,
        scenario: str,
        current: Period,
        actor: str = "analysis",
    ) -> PseudoCorrelationVerdict:
        verdict = PseudoCorrelationVerdict(factor=hypothesis.factor, verdict="通过四步检查")
        verdict.steps.append(self._step_sample_adequacy(result))
        verdict.steps.append(self._step_control_variable(hypothesis, scenario, current, actor))
        verdict.steps.append(self._step_time_precedence(hypothesis, scenario, current, actor))
        verdict.steps.append(self._step_placebo(hypothesis, result, scenario, current, actor))

        for step in verdict.steps:
            if step.verdict in ("共同驱动", "趋势性共同驱动", "证据不足"):
                verdict.verdict = step.verdict
                verdict.reasons.append(f"第 {step.step} 步（{step.name}）：{step.detail}")
        if verdict.verdict == "通过四步检查" and verdict.penalty_total > 0:
            verdict.verdict = "通过但已下调置信度"
            verdict.reasons.append(
                f"四步检查未发现硬伤，但累计下调置信度 {verdict.penalty_total:.2f}"
            )
        return verdict

    # ------------------------------------------------------------------ 第 4 步

    def _step_sample_adequacy(self, result: HypothesisResult) -> StepOutcome:
        evidence = result.evidence
        if evidence is None:
            return StepOutcome(
                step=4,
                name="样本充分性",
                executed=False,
                verdict="证据不足",
                detail="没有证据记录，无法判断样本充分性",
            )
        sample_size = evidence.sample_size
        coverage = int(evidence.detail.get("inside_days", 0))
        required = min(
            self.settings.attr_required_coverage_days, self.settings.attr_observation_days
        )
        if sample_size < MIN_SAMPLE_SIZE:
            return StepOutcome(
                step=4,
                name="样本充分性",
                executed=True,
                verdict="证据不足",
                detail=(
                    f"样本量 {sample_size} < {MIN_SAMPLE_SIZE}：只能记为「证据不足」，不计入主结论"
                ),
            )
        if coverage < required:
            return StepOutcome(
                step=4,
                name="样本充分性",
                executed=True,
                verdict="证据不足",
                detail=f"覆盖 {coverage} 天 < 要求 {required} 天：不计入主结论",
            )
        return StepOutcome(
            step=4,
            name="样本充分性",
            executed=True,
            verdict="通过",
            detail=f"样本量 {sample_size} ≥ {MIN_SAMPLE_SIZE}、覆盖 {coverage} 天 ≥ {required} 天",
        )

    # ------------------------------------------------------------------ 第 1 步

    def _step_control_variable(
        self, hypothesis: Hypothesis, scenario: str, current: Period, actor: str
    ) -> StepOutcome:
        sub_dimension = self._pick_sub_dimension(hypothesis)
        if sub_dimension is None:
            return StepOutcome(
                step=1,
                name="控制变量",
                executed=False,
                verdict="跳过",
                detail="切片已覆盖场景全部维度，没有更细的控制变量可用",
            )
        node = self.engine.tree(scenario).find(hypothesis.factor)
        if node is None or not node.sql:
            return StepOutcome(
                step=1,
                name="控制变量",
                executed=False,
                verdict="跳过",
                detail="因子缺少取数 SQL",
            )
        period = self._observation_period(current)
        best: tuple[float, float, str] | None = None
        for code in self._sub_dimension_values(sub_dimension):
            try:
                inside = self.probe_series(
                    node.sql, period, hypothesis, sub_dimension, code, scenario, actor
                )
                outside = self.probe_series(
                    node.sql, period, hypothesis, None, None, scenario, actor
                )
            except (InferenceError, RuntimeError):
                continue
            if len(inside) < 3 or len(outside) < 3:
                continue
            try:
                effect = standardized_difference(inside, outside)
                test = permutation_test(
                    inside,
                    outside,
                    statistic="mean_difference",
                    permutations=self.settings.attr_permutations,
                    seed=self.settings.warehouse_seed,
                )
            except InferenceError:
                continue
            if best is None or abs(effect) > abs(best[1]):
                best = (test.p_value, effect, code)
        if best is None:
            return StepOutcome(
                step=1,
                name="控制变量",
                executed=False,
                verdict="跳过",
                detail=f"子维度 {sub_dimension} 下没有足够样本可复核",
            )
        p_value, effect, code = best
        if p_value >= CONTROL_VARIABLE_ALPHA:
            return StepOutcome(
                step=1,
                name="控制变量",
                executed=True,
                verdict="共同驱动",
                detail=(
                    f"在 {sub_dimension}={code} 内部复核，p={p_value:.3f} 不显著"
                    f"（效应量 {effect:.2f}）：该关系更像共同驱动，判定为共同驱动"
                ),
            )
        return StepOutcome(
            step=1,
            name="控制变量",
            executed=True,
            verdict="通过",
            detail=(
                f"在 {sub_dimension}={code} 内部复核仍显著"
                f"（p={p_value:.3f}，效应量 {effect:.2f}）"
            ),
        )

    # ------------------------------------------------------------------ 第 2 步

    def _step_time_precedence(
        self, hypothesis: Hypothesis, scenario: str, current: Period, actor: str
    ) -> StepOutcome:
        tree = self.engine.tree(scenario)
        node = tree.find(hypothesis.factor)
        root = tree.root
        if node is None or not node.sql or not root.sql:
            return StepOutcome(
                step=2,
                name="时间先行性",
                executed=False,
                verdict="跳过",
                detail="缺少因子或目标指标的取数 SQL",
            )
        period = self._observation_period(current)
        try:
            factor_series = self.probe_series(
                node.sql, period, hypothesis, None, None, scenario, actor
            )
            target_series = self.probe_series(
                root.sql, period, hypothesis, None, None, scenario, actor
            )
        except (InferenceError, RuntimeError) as error:
            return StepOutcome(
                step=2,
                name="时间先行性",
                executed=False,
                verdict="跳过",
                detail=f"取序列失败：{error}",
            )
        factor_day = _first_significant_deviation(factor_series)
        target_day = _first_significant_deviation(target_series)
        if factor_day is None or target_day is None:
            return StepOutcome(
                step=2,
                name="时间先行性",
                executed=True,
                verdict="跳过",
                detail="窗口内没有出现超过 1.5σ 的显著偏离，无法比较先后",
            )
        if factor_day > target_day:
            return StepOutcome(
                step=2,
                name="时间先行性",
                executed=True,
                verdict="原因晚于结果",
                detail=(
                    f"因子首次显著偏离在 {factor_day}，目标指标在 {target_day}，"
                    "原因晚于结果 ⇒ 置信度下调 0.3（技术规格 §5.9 第 2 条）"
                ),
                penalty=TIME_PRECEDENCE_PENALTY,
            )
        return StepOutcome(
            step=2,
            name="时间先行性",
            executed=True,
            verdict="通过",
            detail=f"因子首次显著偏离 {factor_day} 不晚于目标指标 {target_day}",
        )

    # ------------------------------------------------------------------ 第 3 步

    def _step_placebo(
        self,
        hypothesis: Hypothesis,
        result: HypothesisResult,
        scenario: str,
        current: Period,
        actor: str,
    ) -> StepOutcome:
        tree = self.engine.tree(scenario)
        node = tree.find(hypothesis.factor)
        if node is None or not node.sql:
            return StepOutcome(
                step=3,
                name="安慰剂维度",
                executed=False,
                verdict="跳过",
                detail="因子缺少取数 SQL",
            )
        placebo = self._pick_placebo_slice(hypothesis, set(tree.dimensions))
        if placebo is None:
            return StepOutcome(
                step=3,
                name="安慰剂维度",
                executed=False,
                verdict="跳过",
                detail="找不到与假设无关的维度做安慰剂检验",
            )
        period = self._observation_period(current)
        try:
            inside = self.probe_series(
                node.sql, period, hypothesis, None, None, scenario, actor, override=placebo
            )
            outside = self.probe_series(node.sql, period, hypothesis, None, None, scenario, actor)
            test = permutation_test(
                inside,
                outside,
                statistic="mean_difference",
                permutations=self.settings.attr_permutations,
                seed=self.settings.warehouse_seed,
            )
            placebo_effect = standardized_difference(inside, outside)
            reference_effect = (
                abs(result.evidence.effect_size)
                if result.evidence is not None and result.evidence.effect_size is not None
                else None
            )
        except (InferenceError, RuntimeError) as error:
            return StepOutcome(
                step=3,
                name="安慰剂维度",
                executed=False,
                verdict="跳过",
                detail=f"安慰剂检验无法执行：{error}",
            )
        label = "、".join(f"{key}={','.join(values)}" for key, values in placebo.filters.items())
        comparable = reference_effect is None or abs(placebo_effect) >= (
            PLACEBO_EFFECT_RATIO * reference_effect
        )
        if test.p_value < PLACEBO_ALPHA and comparable:
            return StepOutcome(
                step=3,
                name="安慰剂维度",
                executed=True,
                verdict="趋势性共同驱动",
                detail=(
                    f"在理论上不应成立的维度（{label}）上同样显著（p={test.p_value:.3f}）："
                    f"且强度相当（|效应量| {abs(placebo_effect):.2f} ≥ "
                    f"{PLACEBO_EFFECT_RATIO:.0%}×假设切片）⇒ 判定为趋势性共同驱动"
                ),
            )
        return StepOutcome(
            step=3,
            name="安慰剂维度",
            executed=True,
            verdict="通过",
            detail=(
                f"安慰剂维度（{label}）p={test.p_value:.3f}、"
                f"|效应量| {abs(placebo_effect):.2f}"
                + (
                    "：未达到「同样显著且强度相当」的判据"
                    if test.p_value < PLACEBO_ALPHA and not comparable
                    else "：不显著"
                )
            ),
        )

    # ------------------------------------------------------------------- 工具

    def _observation_period(self, current: Period) -> Period:
        end = date.fromisoformat(current.end)
        start = date.fromisoformat(current.start)
        window_start = start - timedelta(days=self.settings.attr_observation_days - current.days)
        return Period(window_start.isoformat(), end.isoformat(), label="观察窗口")

    def probe_series(
        self,
        factor_sql: str,
        period: Period,
        hypothesis: Hypothesis,
        sub_dimension: str | None,
        sub_code: str | None,
        scenario: str,
        actor: str,
        *,
        override: SliceFilter | None = None,
    ) -> list[float]:
        """取某切片的因子日序列（复用 P3 的沙箱通道，不在这里另开取数口子）。"""

        from app.services.hypotheses import FactorProbe

        if override is not None:
            slice_filter = override
        elif sub_dimension and sub_code:
            filters = {
                key: tuple(values) for key, values in hypothesis.slice_filter.filters.items()
            }
            filters[sub_dimension] = (sub_code,)
            slice_filter = SliceFilter(filters)
        else:
            slice_filter = hypothesis.slice_filter
        probe = FactorProbe(self.engine, self.sandbox)
        rows, _ = probe.series(factor_sql, period, slice_filter, scenario=scenario, actor=actor)
        return [row["value"] for row in rows]

    def _pick_sub_dimension(self, hypothesis: Hypothesis) -> str | None:
        tree = self.engine.tree(self._scenario_of(hypothesis))
        for dimension in tree.dimensions:
            if dimension not in hypothesis.slice_filter.filters:
                return dimension
        return None

    def _scenario_of(self, hypothesis: Hypothesis) -> str:
        if hypothesis.scenario:
            return hypothesis.scenario
        for scenario in self.engine.scenarios():
            if self.engine.tree(scenario).find(hypothesis.factor) is not None:
                return scenario
        raise InferenceError(f"找不到含因子 {hypothesis.factor} 的场景")

    def _sub_dimension_values(self, dimension: str) -> list[str]:
        from app.db import connect_warehouse_readonly
        from app.services.decomposition import DIM_LOOKUP

        table, _ = DIM_LOOKUP[dimension]
        connection = connect_warehouse_readonly(self.engine.settings.warehouse_db)
        try:
            rows = connection.execute(
                f"SELECT code FROM {table} ORDER BY code LIMIT ?", (MAX_SUB_PROBES,)
            ).fetchall()
        finally:
            connection.close()
        return [str(row[0]) for row in rows]

    def _pick_placebo_slice(
        self, hypothesis: Hypothesis, scenario_dimensions: set[str]
    ) -> SliceFilter | None:
        candidates = [
            dim for dim in scenario_dimensions if dim not in hypothesis.slice_filter.filters
        ]
        if not candidates:
            return None
        dimension = sorted(candidates)[0]
        values = self._sub_dimension_values(dimension)
        if not values:
            return None
        return SliceFilter({dimension: (values[-1],)})


def _first_significant_deviation(series: list[float]) -> str | None:
    """首次"偏离窗口均值超过 1.5σ"的日期（工程近似，写在模块 docstring 里）。"""

    if len(series) < 5:
        return None
    import statistics

    mean = statistics.fmean(series)
    stdev = statistics.pstdev(series)
    if stdev == 0:
        return None
    threshold = DEVIATION_SIGMA * stdev
    for index, value in enumerate(series):
        if abs(value - mean) > threshold:
            return f"第 {index + 1} 天"
    return None


__all__ = [
    "CONTROL_VARIABLE_ALPHA",
    "PLACEBO_ALPHA",
    "TIME_PRECEDENCE_PENALTY",
    "PseudoCorrelationChecker",
    "PseudoCorrelationVerdict",
    "StepOutcome",
]
