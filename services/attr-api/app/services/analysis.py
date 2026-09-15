"""L1 分析链路编排（需求说明书 §8.1 的可执行版本）。

链路：**异动 → 一级拆解 → 假设生成 → 沙箱验证 → 统计检验 → 伪相关四步 → 事件匹配 → 结论**。

这个模块是 P5 会话层的地基：会话只需要给它一个"锁定好的上下文"（指标 + 口径版本 + 两期 +
切片），它负责把每一步跑完、把结果落库，并把步骤耗时写进 ``session_steps``（SSE 的事件源）。

两层结果刻意分开，避免混为一谈：

* **贡献额**（deterministic）：由指标树逐层分解给出，是"谁背多少责任"的唯一答案；
* **假设与置信度**（statistical）：由沙箱取证 + 置换检验 + 四步检查给出，回答"这个关系像不像巧合"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.config import Settings
from app.db import connect_app
from app.sandbox.runner import SandboxRunner
from app.services.decomposition import (
    DecompositionReport,
    MetricEngine,
    Period,
    SliceFilter,
)
from app.services.events import (
    BusinessEvent,
    EventMatch,
    causal_chain,
    load_events_from_db,
    match_events,
    sync_seed_events,
)
from app.services.hypotheses import (
    HypothesisResult,
    HypothesisVerifier,
    build_context,
    generate_hypotheses,
)
from app.services.pseudo_correlation import PseudoCorrelationChecker, PseudoCorrelationVerdict
from app.services.seed import seed_reference_data

DEFAULT_TARGETS = {
    "ecom": ["uv", "cvr", "aov"],
    "fmcg": ["revenue", "raw_material_cost", "channel_commission"],
}


@dataclass
class AnalysisRequest:
    """一次分析的输入：锁定的指标上下文 + 两期 + 切片。"""

    scenario: str
    base: Period
    current: Period
    slice_filter: SliceFilter = field(default_factory=SliceFilter)
    auto_targets: list[str] | None = None
    title: str = ""
    actor: str = "analyst"
    observation_days: int | None = None
    persist: bool = True

    def targets(self) -> list[str]:
        if self.auto_targets is not None:
            return list(self.auto_targets)
        return list(DEFAULT_TARGETS.get(self.scenario, []))


@dataclass
class AnalysisReport:
    """一次分析的完整产出：分解、假设、伪相关、事件、结论与落库 id。"""

    request: AnalysisRequest
    decomposition: DecompositionReport
    hypotheses: list[HypothesisResult]
    pseudo: list[PseudoCorrelationVerdict]
    events: list[EventMatch]
    chain: list[dict[str, Any]]
    conclusion: dict[str, Any]
    steps: list[dict[str, Any]]
    duration_ms: int
    session_id: int | None = None
    fallback_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.request.scenario,
            "title": self.request.title,
            "base": self.request.base.as_dict(),
            "current": self.request.current.as_dict(),
            "slice": json.loads(self.request.slice_filter.as_json()),
            "session_id": self.session_id,
            "duration_ms": self.duration_ms,
            "decomposition": self.decomposition.as_dict(),
            "hypotheses": [item.as_dict() for item in self.hypotheses],
            "pseudo_correlation": [item.as_dict() for item in self.pseudo],
            "events": [item.as_dict() for item in self.events],
            "causal_chain": self.chain,
            "conclusion": self.conclusion,
            "steps": self.steps,
            "fallback_note": self.fallback_note,
        }

    def render(self) -> str:
        lines = [self.decomposition.render()]
        verified = [item for item in self.hypotheses if item.status == "verified"]
        excluded = [item for item in self.hypotheses if item.status == "excluded"]
        insufficient = [item for item in self.hypotheses if item.status == "insufficient"]
        lines.append(
            f"\n假设：共 {len(self.hypotheses)} 条 —— 通过 {len(verified)}、"
            f"已排除 {len(excluded)}、证据不足 {len(insufficient)}"
        )
        for item in self.hypotheses:
            lines.append(
                f"  [{item.status}] {item.hypothesis.statement}"
                f"（最终置信度 {item.final_confidence:.2f}）"
            )
            lines.append(f"      依据：{item.reason}")
        if self.pseudo:
            lines.append("\n伪相关四步检查：")
            for verdict in self.pseudo:
                lines.append(
                    f"  {verdict.factor} → {verdict.verdict}"
                    f"（下调 {verdict.penalty_total:.2f}）"
                )
                for step in verdict.steps:
                    lines.append(
                        f"      第 {step.step} 步 {step.name}：{step.verdict} — {step.detail}"
                    )
        lines.append("\n事件匹配：")
        if not self.events:
            lines.append("  窗口内没有相关事件（不强行关联）")
        for match in self.events:
            lines.append(
                f"  [{match.tier}] {match.event.name}（相关度 {match.relevance:.2f}）"
                f"　{match.reason}"
            )
        conclusion = self.conclusion
        lines.append("\n结论：")
        lines.append(f"  主因：{conclusion.get('primary_cause')}")
        for item in conclusion.get("secondary_causes", []):
            lines.append(f"  次因：{item}")
        lines.append(f"  外生变量：{conclusion.get('exogenous_note')}")
        return "\n".join(lines)


def run_analysis(
    request: AnalysisRequest,
    *,
    engine: MetricEngine | None = None,
    sandbox: SandboxRunner | None = None,
    settings: Settings | None = None,
) -> AnalysisReport:
    """把一条 L1 链路跑完，返回结构化报告（可选落库）。"""

    active_engine = engine or MetricEngine(settings)
    # 配置必须跟着引擎走：否则会出现"引擎读临时库、落库写仓库库"的错配（P4 踩过）
    active_settings = settings or active_engine.settings
    active_sandbox = sandbox or SandboxRunner(active_settings)
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []

    def record(kind: str, status: str, payload: dict[str, Any], since: float) -> None:
        steps.append(
            {
                "seq": len(steps) + 1,
                "kind": kind,
                "status": status,
                "duration_ms": int((time.perf_counter() - since) * 1000),
                "payload": payload,
            }
        )

    # ① 一级拆解（+ 按场景默认下钻）
    marker = time.perf_counter()
    decomposition = active_engine.decompose(
        request.scenario,
        request.base,
        request.current,
        slice_filter=request.slice_filter,
        auto_targets=request.targets(),
    )
    record(
        "query",
        "completed",
        {
            "nodes": [node.code for node in decomposition.nodes],
            "max_relative_residual": decomposition.max_relative_residual,
            # 根节点摘要进步骤载荷：前端与报告都从这一份数据画瀑布图，不再各自重算
            "conserved": decomposition.ok,
            "root": decomposition.root.as_dict(),
            "skipped_targets": decomposition.skipped_targets,
        },
        marker,
    )

    # ② 假设生成
    marker = time.perf_counter()
    context = build_context(active_engine, decomposition, slice_filter=request.slice_filter)
    hypotheses, fallback_note = generate_hypotheses(context, settings=active_settings)
    record(
        "hypothesis",
        "completed",
        {"count": len(hypotheses), "source": hypotheses[0].source if hypotheses else "none"},
        marker,
    )

    # ③ 沙箱验证 + ④ 伪相关四步
    verifier = HypothesisVerifier(active_engine, active_sandbox, active_settings)
    checker = PseudoCorrelationChecker(active_engine, active_sandbox, active_settings)
    results: list[HypothesisResult] = []
    pseudo: list[PseudoCorrelationVerdict] = []
    for hypothesis in hypotheses:
        marker = time.perf_counter()
        result = verifier.verify(
            hypothesis,
            scenario=request.scenario,
            current=request.current,
            base=request.base,
            observation_days=request.observation_days,
            actor=request.actor,
        )
        verdict = checker.check(
            hypothesis,
            result,
            scenario=request.scenario,
            current=request.current,
            actor=request.actor,
        )
        pseudo.append(verdict)
        if verdict.excluded:
            result.status = "excluded"
            result.reason = result.reason + "；" + "；".join(verdict.reasons)
        elif verdict.penalty_total:
            result.final_confidence = max(0.0, result.final_confidence - verdict.penalty_total)
            result.penalties.extend(
                {"reason": step.detail, "delta": -step.penalty}
                for step in verdict.steps
                if step.penalty
            )
            if result.final_confidence < active_settings.attr_min_confidence:
                result.status = "excluded"
                result.reason += (
                    f"；四步检查下调后置信度 {result.final_confidence:.3f}"
                    f" 低于阈值 {active_settings.attr_min_confidence:g}"
                )
        results.append(result)
        record(
            "verify",
            result.status,
            {
                "factor": hypothesis.factor,
                "final_confidence": round(result.final_confidence, 4),
                "pseudo_verdict": verdict.verdict,
            },
            marker,
        )

    # ⑤ 事件匹配
    marker = time.perf_counter()
    events = _load_events(active_engine)
    tree = active_engine.tree(request.scenario)
    matches = match_events(
        events,
        window_start=date.fromisoformat(request.current.start),
        window_end=date.fromisoformat(request.current.end),
        slice_dimensions=set(request.slice_filter.filters),
        scenario_dimensions=set(tree.dimensions),
        window_days=active_settings.attr_event_window_days,
        slice_values={key: list(values) for key, values in request.slice_filter.filters.items()},
    )
    record(
        "event",
        "completed",
        {"matched": len(matches), "in_chain": sum(1 for m in matches if m.tier == "因果链")},
        marker,
    )

    # ⑥ 结论组装
    conclusion = _conclude(decomposition, results, matches)
    duration_ms = int((time.perf_counter() - started) * 1000)
    report = AnalysisReport(
        request=request,
        decomposition=decomposition,
        hypotheses=results,
        pseudo=pseudo,
        events=matches,
        chain=causal_chain(matches),
        conclusion=conclusion,
        steps=steps,
        duration_ms=duration_ms,
        fallback_note=fallback_note,
    )
    if request.persist:
        report.session_id = persist_analysis(report, engine=active_engine, settings=active_settings)
    return report


def _load_events(engine: MetricEngine) -> list[BusinessEvent]:
    connection = connect_app(engine.settings.app_db)
    try:
        sync_seed_events(connection)
        return load_events_from_db(connection)
    finally:
        connection.close()


def _conclude(
    decomposition: DecompositionReport,
    results: list[HypothesisResult],
    matches: list[EventMatch],
) -> dict[str, Any]:
    """结论：主因来自分解（确定性），佐证来自通过验证的假设，外生变量来自事件链。"""

    top = decomposition.top(limit=3)
    primary = top[0] if top else None
    verified = [item for item in results if item.status == "verified"]
    exogenous = [match.event.name for match in matches if match.tier == "因果链"]
    secondary = []
    for item in top[1:]:
        secondary.append(
            f"{item['factor']}：贡献 {item['contribution']:,.0f} 分"
            f"（贡献率 {'—' if item['rate'] is None else f'{item['rate']:.1%}'}）"
        )
    return {
        "primary_cause": (
            None
            if primary is None
            else f"{primary['factor']}：贡献 {primary['contribution']:,.0f} 分"
            f"（贡献率 {'—' if primary['rate'] is None else f'{primary['rate']:.1%}'}），"
            f"由分解直接给出，最大相对残差 {decomposition.max_relative_residual:.2e}"
        ),
        "secondary_causes": secondary,
        "verified_hypotheses": [item.hypothesis.statement for item in verified],
        "excluded_hypotheses": [
            {"statement": item.hypothesis.statement, "reason": item.reason}
            for item in results
            if item.status != "verified"
        ],
        "exogenous_note": (
            "、".join(exogenous) + "（外部因素，非内部操作问题）"
            if exogenous
            else "窗口内未命中事件，不强行关联"
        ),
        "disclaimer": (
            "贡献额来自确定性分解；假设与置信度来自沙箱证据与统计检验，两者口径不同，不混用"
        ),
    }


def persist_analysis(
    report: AnalysisReport,
    *,
    engine: MetricEngine,
    settings: Settings,
) -> int:
    """落库：会话、步骤流、假设、证据（数值全部来自分解与沙箱证据，不来自文本）。"""

    seed_reference_data(engine)
    connection = connect_app(settings.app_db)
    try:
        user_id = connection.execute(
            "SELECT id FROM users WHERE role = 'analyst' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        metric_code = f"{report.request.scenario}.{report.decomposition.metric_code}"
        metric_id = connection.execute(
            "SELECT id FROM metric_definitions WHERE code = ?", (metric_code,)
        ).fetchone()[0]
        title = report.request.title or (
            f"{report.decomposition.scenario_name}·{report.decomposition.metric_name}"
            f" {report.request.current.start}~{report.request.current.end}"
        )
        cursor = connection.execute(
            "INSERT INTO sessions (title, domain, metric_id, caliber_version, base_period,"
            " current_period, slice_json, status, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                title,
                report.request.scenario,
                metric_id,
                1,
                report.request.base.start,
                report.request.current.start,
                report.request.slice_filter.as_json(),
                "completed",
                user_id,
            ),
        )
        session_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO session_steps (session_id, seq, kind, status, payload_json, duration_ms)"
            " VALUES (?,?,?,?,?,?)",
            [
                (
                    session_id,
                    step["seq"],
                    step["kind"],
                    step["status"],
                    json.dumps(step["payload"], ensure_ascii=False),
                    step["duration_ms"],
                )
                for step in report.steps
            ],
        )
        for result in report.hypotheses:
            cursor = connection.execute(
                "INSERT INTO hypotheses (session_id, statement, expected_direction, code_draft,"
                " status, confidence, reason, evidence_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    result.hypothesis.statement,
                    result.hypothesis.expected_direction,
                    result.hypothesis.sql_draft,
                    result.status,
                    result.final_confidence,
                    result.reason,
                    json.dumps(
                        {
                            "source": result.hypothesis.source,
                            "model": result.hypothesis.model,
                            "confidence": (
                                None if result.confidence is None else result.confidence.as_dict()
                            ),
                            "penalties": result.penalties,
                            "direction_ok": result.direction_ok,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            hypothesis_id = int(cursor.lastrowid or 0)
            if result.evidence is not None:
                evidence = result.evidence
                connection.execute(
                    "INSERT INTO evidence (session_id, hypothesis_id, kind, sql_digest,"
                    " result_json, sample_size, p_value, effect_size) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        hypothesis_id,
                        evidence.kind,
                        evidence.sql_digest,
                        json.dumps(evidence.as_dict(), ensure_ascii=False),
                        evidence.sample_size,
                        evidence.p_value,
                        evidence.effect_size,
                    ),
                )
        connection.commit()
        return session_id
    finally:
        connection.close()


def analysis_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_TARGETS",
    "AnalysisReport",
    "AnalysisRequest",
    "analysis_timestamp",
    "persist_analysis",
    "run_analysis",
]
