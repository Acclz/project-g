"""评测套件（用例层）：E1 归因准确率 + E3 伪相关误纳率。

放在用例层而不是脚本里，是因为脚本不允许被服务代码 import（`02-流程与规范` §2.1）：
CLI（`scripts/eval_attribution.py`）与接口（`POST /api/eval/run`）都要调它。

口径说明：真值记录的是"注入本身造成的贡献"，分解看到的是"两期全部变化"，
所以 E1 的贡献额相对误差天然含自然波动；P6 会用配套对照窗口进一步收紧。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT, Settings, get_settings
from app.db import connect_app, connect_warehouse_readonly
from app.sandbox.runner import SandboxRunner
from app.services.analysis import DEFAULT_TARGETS
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.hypotheses import Hypothesis, HypothesisVerifier, build_context
from app.services.pseudo_correlation import PseudoCorrelationChecker

TRAPS_PATH = REPO_ROOT / "corpus" / "eval" / "correlation_traps.yaml"
MAX_ACCEPT_RATE = 0.10
DATASETS = ("attribution_eval", "correlation_traps")


@dataclass(frozen=True)
class EvalSuiteResult:
    """一次评测的结果：数据集、指标字典、逐条明细与是否达标。"""

    dataset: str
    metrics: dict[str, Any]
    cases: list[dict[str, Any]]
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "metrics": self.metrics,
            "cases": self.cases,
            "passed": self.passed,
        }


def ground_truth_rows() -> list[Any]:
    connection = connect_app(get_settings().app_db)
    try:
        return connection.execute("SELECT * FROM ground_truth ORDER BY scenario, id").fetchall()
    finally:
        connection.close()


def run_attribution_eval(engine: MetricEngine | None = None) -> EvalSuiteResult:
    """E1：逐条真因核对 Top-1 / Top-3 与贡献额相对误差。"""

    active = engine or MetricEngine()
    cases: list[dict[str, Any]] = []
    for row in ground_truth_rows():
        current = Period(row["day"], row["window_end"], label="注入窗口")
        slice_filter = SliceFilter.from_json(row["dimension_json"])
        report = active.decompose(
            row["scenario"],
            current.previous(),
            current,
            slice_filter=slice_filter,
            auto_targets=DEFAULT_TARGETS[row["scenario"]],
        )
        contributions = report.root.as_dict()["contributions"]
        ranking = [item["factor"] for item in contributions]
        observed = next((item for item in contributions if item["factor"] == row["factor"]), None)
        truth_cents = int(row["injected_contribution_cents"])
        observed_cents = int(round(observed["contribution"])) if observed else 0
        relative_error = (
            abs(observed_cents - truth_cents) / max(abs(truth_cents), 1) if truth_cents else None
        )
        cases.append(
            {
                "ground_truth_id": row["id"],
                "scenario": row["scenario"],
                "slice": json.loads(slice_filter.as_json()),
                "window": [row["day"], row["window_end"]],
                "expected_factor": row["factor"],
                "ranking": ranking[:3],
                "top1_hit": bool(ranking) and ranking[0] == row["factor"],
                "top3_hit": row["factor"] in ranking[:3],
                "truth_contribution_cents": truth_cents,
                "observed_contribution_cents": observed_cents,
                "relative_error": relative_error,
                "max_relative_residual": report.max_relative_residual,
                "conserved": report.ok,
            }
        )
    total = len(cases)
    errors = sorted(case["relative_error"] for case in cases if case["relative_error"] is not None)
    metrics = {
        "total": total,
        "top1_rate": (sum(1 for case in cases if case["top1_hit"]) / total) if total else 0.0,
        "top3_rate": (sum(1 for case in cases if case["top3_hit"]) / total) if total else 0.0,
        "median_relative_error": errors[len(errors) // 2] if errors else None,
        "max_relative_error": max(errors) if errors else None,
        "all_conserved": all(case["conserved"] for case in cases),
        "sample_note": f"首轮基线样本 {total} 条（预埋真因数量决定），P6 扩到 ≥20 条",
    }
    return EvalSuiteResult(
        dataset="attribution_eval",
        metrics=metrics,
        cases=cases,
        passed=bool(metrics["all_conserved"]),
    )


def load_traps(path: Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path is not None else TRAPS_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return list(payload.get("traps", []))


def run_trap_eval(
    engine: MetricEngine | None = None,
    sandbox: SandboxRunner | None = None,
    settings: Settings | None = None,
) -> EvalSuiteResult:
    """E3：陷阱集误纳率——被错误采纳的陷阱比例必须 ≤ 10%。"""

    active_settings = settings or get_settings()
    active_engine = engine or MetricEngine(active_settings)
    active_sandbox = sandbox or SandboxRunner(active_settings)
    verifier = HypothesisVerifier(active_engine, active_sandbox, active_settings)
    checker = PseudoCorrelationChecker(active_engine, active_sandbox, active_settings)
    cases: list[dict[str, Any]] = []
    for trap in load_traps():
        slice_filter = SliceFilter(
            {key: tuple(values) for key, values in (trap.get("slice") or {}).items()}
        )
        current, base = trap_windows(active_engine)
        node = active_engine.tree(trap["scenario"]).find(trap["factor"])
        context = build_context(
            active_engine,
            active_engine.decompose(
                trap["scenario"], base, current, slice_filter=slice_filter, auto_targets=[]
            ),
            slice_filter=slice_filter,
        )
        hypothesis = Hypothesis(
            statement=f"[陷阱] {node.name if node else trap['factor']} 的变化是主因",
            expected_direction="down",
            verification="与真因相同的验证口径（对照检验）",
            sql_draft=node.sql if node else "",
            counter_check="若为真，切片内子维度应同向变化",
            factor=trap["factor"],
            factor_name=node.name if node else trap["factor"],
            scope_label=context["scope_label"],
            source="trap",
            scenario=trap["scenario"],
            slice_filter=slice_filter,
        )
        result = verifier.verify(
            hypothesis,
            scenario=trap["scenario"],
            current=current,
            base=base,
            actor="eval:trap",
        )
        verdict = checker.check(
            hypothesis, result, scenario=trap["scenario"], current=current, actor="eval:trap"
        )
        accepted = not verdict.excluded and result.status == "verified"
        cases.append(
            {
                "id": trap["id"],
                "scenario": trap["scenario"],
                "factor": trap["factor"],
                "slice": trap.get("slice") or {},
                "note": trap.get("note", ""),
                "expected": trap.get("expected", "excluded"),
                "verifier_status": result.status,
                "final_confidence": round(result.final_confidence, 4),
                "pseudo_verdict": verdict.verdict,
                "reasons": verdict.reasons or [result.reason],
                "accepted": accepted,
            }
        )
    total = len(cases)
    accepted = sum(1 for case in cases if case["accepted"])
    accept_rate = accepted / total if total else 0.0
    metrics = {
        "total": total,
        "accepted": accepted,
        "accept_rate": accept_rate,
        "threshold": MAX_ACCEPT_RATE,
        "cases_with_reason": sum(1 for case in cases if case["reasons"]),
    }
    return EvalSuiteResult(
        dataset="correlation_traps",
        metrics=metrics,
        cases=cases,
        passed=accept_rate <= MAX_ACCEPT_RATE,
    )


def trap_windows(engine: MetricEngine) -> tuple[Period, Period]:
    """陷阱用例的观察窗口：取数仓末尾 28 天（与真因无关，纯对照）。"""

    connection = connect_warehouse_readonly(engine.settings.warehouse_db)
    try:
        end = connection.execute("SELECT MAX(day) FROM dw.dim_date").fetchone()[0]
        start = connection.execute(
            "SELECT MIN(day) FROM (SELECT day FROM dw.dim_date ORDER BY day DESC LIMIT 28)"
        ).fetchone()[0]
    finally:
        connection.close()
    current = Period(str(start), str(end), label="陷阱观察窗口")
    return current, current.previous()


def run_dataset(dataset: str, *, label: str = "api") -> EvalSuiteResult:
    """按数据集名分发（接口层用）。"""

    if dataset == "attribution_eval":
        return run_attribution_eval()
    if dataset == "correlation_traps":
        return run_trap_eval()
    raise ValueError(f"未知数据集：{dataset}（可用：{', '.join(DATASETS)}）")


__all__ = [
    "DATASETS",
    "MAX_ACCEPT_RATE",
    "TRAPS_PATH",
    "EvalSuiteResult",
    "ground_truth_rows",
    "load_traps",
    "run_attribution_eval",
    "run_dataset",
    "run_trap_eval",
    "trap_windows",
]
