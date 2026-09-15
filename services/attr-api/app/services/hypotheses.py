"""假设生成与沙箱验证（技术规格 §5.7/§5.8、需求说明书 §5.5）。

链路：分解结果 → 生成 3~5 条候选假设（五要素）→ 在沙箱里取数验证 → 统计检验 →
置信度合成 → 低于阈值则标记"已排除"并**保留原因**（不隐藏、不静默丢弃）。

两个刻意的设计：

* **数值只来自沙箱查询与算法包**：草案里的数值一律不由写作者（模板或模型）提供；
* **取数封装成探针**：``FactorProbe`` 负责"某因子的日序列"，验证与伪相关四步都复用它，
  这样统计口径只有一处，测试也能用假的探针替换沙箱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.sandbox.runner import SandboxRunner
from app.services.decomposition import (
    DecompositionReport,
    MetricEngine,
    Period,
    SliceFilter,
)
from app.services.llm import HypothesisDraft, draft_with_fallback
from attribution import (
    ConfidenceBreakdown,
    InferenceError,
    composite_confidence,
    permutation_test,
    standardized_difference,
)

DEFAULT_MAX_HYPOTHESES = 5
#: 反例成立时的置信度下调幅度（技术规格 §5.9 给出时间先行性 -0.3，反例幅度由本项目定为 0.15）
COUNTEREXAMPLE_PENALTY = 0.15
#: 样本充分性门槛：低于它直接判"证据不足"，不允许标成"已验证"（技术规格 §5.9 第 4 条）
INSUFFICIENT_SAMPLE_SIZE = 60


@dataclass
class Hypothesis:
    """一条候选假设：五要素 + 因子与切片上下文（数值不在其中）。"""

    statement: str
    expected_direction: str
    verification: str
    sql_draft: str
    counter_check: str
    factor: str
    factor_name: str
    scope_label: str
    source: str
    scenario: str = ""
    model: str = ""
    note: str = ""
    slice_filter: SliceFilter = field(default_factory=SliceFilter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "expected_direction": self.expected_direction,
            "verification": self.verification,
            "sql_draft": self.sql_draft,
            "counter_check": self.counter_check,
            "factor": self.factor,
            "factor_name": self.factor_name,
            "scope_label": self.scope_label,
            "source": self.source,
            "scenario": self.scenario,
            "model": self.model,
            "note": self.note,
            "slice": self.slice_filter.filters and {
                key: list(values) for key, values in self.slice_filter.filters.items()
            },
        }


@dataclass
class EvidenceRecord:
    """一条证据：类型、SQL 摘要、样本量、p 值、效应量与沙箱审计号。"""

    kind: str
    sql_digest: str
    sample_size: int
    p_value: float | None
    effect_size: float | None
    detail: dict[str, Any]
    audit_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sql_digest": self.sql_digest,
            "sample_size": self.sample_size,
            "p_value": self.p_value,
            "effect_size": self.effect_size,
            "audit_id": self.audit_id,
            "detail": self.detail,
        }


@dataclass
class HypothesisResult:
    """一条假设的验证结果：状态、置信度三部分、证据与理由。"""

    hypothesis: Hypothesis
    status: str
    confidence: ConfidenceBreakdown | None
    final_confidence: float
    reason: str
    evidence: EvidenceRecord | None = None
    penalties: list[dict[str, Any]] = field(default_factory=list)
    direction_ok: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis.as_dict(),
            "status": self.status,
            "confidence": None if self.confidence is None else self.confidence.as_dict(),
            "final_confidence": round(self.final_confidence, 4),
            "reason": self.reason,
            "direction_ok": self.direction_ok,
            "penalties": self.penalties,
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
        }


class FactorProbe:
    """因子日序列探针：把"取某因子在某切片下的逐日取值"封成可替换的小对象。"""

    def __init__(self, engine: MetricEngine, sandbox: SandboxRunner | None = None) -> None:
        self.engine = engine
        self.sandbox = sandbox

    def series(
        self,
        factor_sql: str,
        period: Period,
        slice_filter: SliceFilter | None = None,
        *,
        scenario: str,
        actor: str = "analysis",
    ) -> tuple[list[dict[str, Any]], EvidenceRecord]:
        """返回逐日序列与证据记录；取数**必须**经沙箱执行。"""

        if self.sandbox is None:
            raise RuntimeError("因子探针必须带沙箱执行器：任何由模型产出的 SQL 都只允许在沙箱里跑")
        conn = None
        try:

            from app.db import connect_warehouse_readonly

            conn = connect_warehouse_readonly(self.engine.settings.warehouse_db)
            where = self.engine._slice_clause(conn, scenario, slice_filter or SliceFilter())
        finally:
            if conn is not None:
                conn.close()
        sql = (
            f"SELECT day, {factor_sql} AS value FROM {self.engine.fact_table(scenario)} AS fact"
            f" WHERE fact.day BETWEEN '{period.start}' AND '{period.end}'{where} GROUP BY day"
            " ORDER BY day"
        )
        result = self.sandbox.run_sql(sql, actor=actor)
        if not result.ok:
            raise InferenceError(f"沙箱取数失败：{result.error or result.blocked_reason}")
        rows = [
            {"day": row[0], "value": float(row[1]) if row[1] is not None else None}
            for row in (result.rows or [])
        ]
        rows = [row for row in rows if row["value"] is not None]
        evidence = EvidenceRecord(
            kind="factor_series",
            sql_digest=result.statement_digest,
            sample_size=len(rows),
            p_value=None,
            effect_size=None,
            detail={"slice": _slice_payload(slice_filter), "period": period.as_dict()},
            audit_id=result.audit_id,
        )
        return rows, evidence


def _slice_payload(slice_filter: SliceFilter | None) -> dict[str, Any]:
    if slice_filter is None or slice_filter.empty:
        return {}
    return {key: list(values) for key, values in slice_filter.filters.items()}


def build_context(
    engine: MetricEngine,
    report: DecompositionReport,
    *,
    slice_filter: SliceFilter | None = None,
) -> dict[str, Any]:
    """把分解结果整理成写作者（模板或模型）需要的上下文。

    只给**因子清单、维度与表名**，不给任何"看起来像答案"的数字汇总以外的信息——
    数值本来就该由系统算，模型拿来也没用。
    """

    tree = engine.tree(report.scenario)
    active_slice = slice_filter or SliceFilter()
    scope_label = _scope_label(active_slice)
    factors: list[dict[str, Any]] = []
    for item in report.root.as_dict()["contributions"]:
        node = tree.find(item["factor"])
        if node is None:
            continue
        factors.append(
            {
                "code": node.code,
                "name": node.name,
                "contribution": item["contribution"],
                "sql": node.sql,
                "scope_label": scope_label,
            }
        )
    factors.sort(key=lambda entry: abs(entry["contribution"]), reverse=True)
    return {
        "scenario": report.scenario,
        "scenario_name": report.scenario_name,
        "metric_code": report.metric_code,
        "metric_name": report.metric_name,
        "base_period": report.base.as_dict(),
        "current_period": report.current.as_dict(),
        "slice": _slice_payload(active_slice),
        "scope_label": scope_label,
        "dimensions": tree.dimensions,
        "tables": [
            engine.fact_table(report.scenario),
            "dw.dim_channel",
            "dw.dim_category",
            "dw.dim_region",
            "dw.dim_segment",
            "dw.dim_sku",
        ],
        "factors": factors,
        "top_contributors": [
            {
                "factor": item["factor"],
                "contribution": item["contribution"],
                "rate": item["rate"],
            }
            for item in report.root.as_dict()["contributions"][:3]
        ],
    }


def _scope_label(slice_filter: SliceFilter) -> str:
    if slice_filter.empty:
        return "全量口径"
    parts = [f"{key}={'/'.join(values)}" for key, values in slice_filter.filters.items()]
    return "切片 " + "、".join(parts)


def generate_hypotheses(
    context: dict[str, Any],
    *,
    settings: Settings | None = None,
    limit: int | None = None,
) -> tuple[list[Hypothesis], str]:
    """生成候选假设：有密钥走模型，没有则走模板；两条路径的产出结构完全一致。"""

    active = settings or get_settings()
    max_items = limit or active.attr_max_hypotheses
    drafts, fallback_reason = draft_with_fallback(context, max_items, active)
    factor_index = {factor["code"]: factor for factor in context.get("factors", [])}
    # 假设必须带上本次分析的切片：否则"切片内 vs 切片外"会退化成自己比自己（P4 首轮踩过的坑）
    analysis_slice = SliceFilter(
        {key: tuple(values) for key, values in (context.get("slice") or {}).items()}
    )
    hypotheses: list[Hypothesis] = []
    for draft in drafts:
        factor = factor_index.get(draft.factor) or _infer_factor(draft, context)
        hypotheses.append(
            Hypothesis(
                statement=draft.statement,
                expected_direction=draft.expected_direction or "up",
                verification=draft.verification,
                sql_draft=draft.sql_draft,
                counter_check=draft.counter_check,
                factor=factor["code"],
                factor_name=factor["name"],
                scope_label=factor.get("scope_label", context.get("scope_label", "")),
                source=draft.source,
                scenario=context.get("scenario", ""),
                slice_filter=analysis_slice,
                model=draft.model,
                note=draft.note,
            )
        )
    return hypotheses, fallback_reason


def _infer_factor(draft: HypothesisDraft, context: dict[str, Any]) -> dict[str, Any]:
    """模型没写 factor 时按陈述里出现的因子名回填；都找不到就用头号因子。"""

    for factor in context.get("factors", []):
        if factor["name"] in draft.statement or factor["code"] in draft.statement:
            return factor
    factors = context.get("factors") or []
    if not factors:
        raise InferenceError("上下文里没有任何因子，无法生成假设")
    return factors[0]


@dataclass
class HypothesisVerifier:
    """在沙箱里验证假设：取序列 → 置换检验 → 效应量 → 置信度 → 状态。"""

    engine: MetricEngine
    sandbox: SandboxRunner
    settings: Settings

    def __post_init__(self) -> None:
        self.probe = FactorProbe(self.engine, self.sandbox)

    def verify(
        self,
        hypothesis: Hypothesis,
        *,
        scenario: str,
        current: Period,
        base: Period | None = None,
        observation_days: int | None = None,
        actor: str = "analysis",
    ) -> HypothesisResult:
        active_days = observation_days or self.settings.attr_observation_days
        window_end = date.fromisoformat(current.end)
        window_start = window_end - timedelta(days=active_days - 1)
        period = Period(window_start.isoformat(), window_end.isoformat(), label="观察窗口")
        node = self.engine.tree(scenario).find(hypothesis.factor)
        if node is None or not node.sql:
            return HypothesisResult(
                hypothesis=hypothesis,
                status="excluded",
                confidence=None,
                final_confidence=0.0,
                reason=f"指标字典里找不到因子 {hypothesis.factor} 或其取数 SQL",
            )
        try:
            inside, inside_evidence = self.probe.series(
                node.sql, period, hypothesis.slice_filter, scenario=scenario, actor=actor
            )
            outside, outside_evidence = self.probe.series(
                node.sql, period, None, scenario=scenario, actor=actor
            )
        except InferenceError as error:
            return HypothesisResult(
                hypothesis=hypothesis,
                status="excluded",
                confidence=None,
                final_confidence=0.0,
                reason=f"沙箱取证失败：{error}",
            )
        inside_values = [row["value"] for row in inside]
        outside_values = [row["value"] for row in outside]
        if len(inside_values) < 3 or len(outside_values) < 3:
            return HypothesisResult(
                hypothesis=hypothesis,
                status="insufficient",
                confidence=None,
                final_confidence=0.0,
                reason=(
                    f"样本不足（切片内 {len(inside_values)} 天 / 全量 {len(outside_values)} 天）："
                    "按技术规格 §5.9 第 4 条判为证据不足"
                ),
                evidence=inside_evidence,
            )
        try:
            test = permutation_test(
                inside_values,
                outside_values,
                statistic="mean_difference",
                permutations=self.settings.attr_permutations,
                seed=self.settings.warehouse_seed,
            )
            effect = standardized_difference(inside_values, outside_values)
        except InferenceError as error:
            return HypothesisResult(
                hypothesis=hypothesis,
                status="insufficient",
                confidence=None,
                final_confidence=0.0,
                reason=f"统计检验无法进行：{error}",
                evidence=inside_evidence,
            )
        required_coverage = min(self.settings.attr_required_coverage_days, active_days)
        if test.sample_size < INSUFFICIENT_SAMPLE_SIZE or len(inside_values) < required_coverage:
            return HypothesisResult(
                hypothesis=hypothesis,
                status="insufficient",
                confidence=None,
                final_confidence=0.0,
                reason=(
                    f"证据不足：样本量 {test.sample_size}（要求 ≥{INSUFFICIENT_SAMPLE_SIZE}）、"
                    f"切片内覆盖 {len(inside_values)} 天（要求 ≥{required_coverage}）——"
                    "按技术规格 §5.9 第 4 条不计入主结论"
                ),
                evidence=inside_evidence,
            )
        confidence = composite_confidence(
            p_value=test.p_value,
            effect=effect,
            covered_days=len(inside_values),
            required_days=required_coverage,
            effect_small=self.settings.attr_effect_small,
            effect_large=self.settings.attr_effect_large,
        )
        direction_ok = _direction_matches(
            inside_values, outside_values, hypothesis.expected_direction
        )
        share = self._contribution_share(
            scenario, base, current, hypothesis, actor
        )
        penalties: list[dict[str, Any]] = []
        final = confidence.total
        if direction_ok is False:
            penalties.append(
                {
                    "reason": "反例检查：切片内变化方向与预期相反，置信度下调",
                    "delta": -COUNTEREXAMPLE_PENALTY,
                }
            )
            final = max(0.0, final - COUNTEREXAMPLE_PENALTY)
        status = "verified" if final >= self.settings.attr_min_confidence else "excluded"
        if share is not None and share < self.settings.attr_min_contribution_share:
            status = "excluded"
        reason = (
            f"p={test.p_value:.4f}（{test.permutations} 次置换，种子 {test.seed}）、"
            f"效应量={effect:.3f}、覆盖 {len(inside_values)}/{required_coverage} 天"
        )
        if status == "excluded":
            if share is not None and share < self.settings.attr_min_contribution_share:
                reason += (
                    f"；该切片对总变动的贡献占比 {share:.2%} 低于门槛"
                    f" {self.settings.attr_min_contribution_share:.0%}："
                    "贡献可忽略的切片不能被称为主因（需求说明书 §5.11）"
                )
            if final < self.settings.attr_min_confidence:
                reason += (
                    f"；置信度 {final:.3f} 低于阈值 {self.settings.attr_min_confidence:g}，"
                    "标记为已排除"
                )
        evidence = EvidenceRecord(
            kind="permutation_test",
            sql_digest=inside_evidence.sql_digest,
            sample_size=test.sample_size,
            p_value=test.p_value,
            effect_size=effect,
            detail={
                "statistic_kind": test.statistic_kind,
                "permutations": test.permutations,
                "seed": test.seed,
                "statistic": test.statistic,
                "inside_days": len(inside_values),
                "outside_days": len(outside_values),
                "outside_sql_digest": outside_evidence.sql_digest,
                "observation_window": period.as_dict(),
                "contribution_share": share,
            },
            audit_id=inside_evidence.audit_id,
        )
        return HypothesisResult(
            hypothesis=hypothesis,
            status=status,
            confidence=confidence,
            final_confidence=final,
            reason=reason,
            evidence=evidence,
            penalties=penalties,
            direction_ok=direction_ok,
        )

    def _contribution_share(
        self,
        scenario: str,
        base: Period | None,
        current: Period,
        hypothesis: Hypothesis,
        actor: str,
    ) -> float | None:
        """该切片对总变动的贡献占比（确定性分解给出，不来自模型、也不来自统计推断）。

        依据需求说明书 §5.11：结论必须绑定量化贡献；贡献可忽略的切片不能被称为"主因"。
        """

        if base is None:
            return None
        try:
            sliced = self.engine.decompose(
                scenario, base, current, slice_filter=hypothesis.slice_filter, auto_targets=[]
            )
            overall = self.engine.decompose(scenario, base, current, auto_targets=[])
        except Exception:  # noqa: BLE001 - 占比只用于加一道门槛，算不出来就不加这道门槛
            return None
        denominator = abs(overall.root.delta)
        if denominator == 0:
            return None
        return abs(sliced.root.delta) / denominator


def _direction_matches(
    inside: list[float], outside: list[float], expected: str
) -> bool:
    """反例检查的第一步：切片内的变化方向是否与假设预期一致。"""

    inside_delta = sum(inside) / len(inside)
    outside_delta = sum(outside) / len(outside)
    observed = "up" if inside_delta >= outside_delta else "down"
    return observed == expected


__all__ = [
    "COUNTEREXAMPLE_PENALTY",
    "DEFAULT_MAX_HYPOTHESES",
    "EvidenceRecord",
    "FactorProbe",
    "Hypothesis",
    "HypothesisResult",
    "HypothesisVerifier",
    "build_context",
    "generate_hypotheses",
]
