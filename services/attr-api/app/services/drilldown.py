"""L2 下钻链路（需求说明书 §8.2 的可执行版本）。

链路：**继承锁定上下文 → 只收紧切片 → 指标树逐层分解 → 维度组合透视（精确优先）→
透视表与指标树交叉守恒 → 新假设回 L1 验证 → 覆盖率与关键变化特征 → 结论**。

三条硬规则：

1. **继承而不扩张**：下钻贴着上层锁定切片做，切片只能收紧；放宽由会话层直接拒绝
   （``ContextLocked``，见 ``sessions.py`` 与 ``tests/test_api_sessions.py``）。
2. **数值单源**：贡献额只有两个来源——数仓取数与算法包分解；大模型文本永不进数值链路。
3. **分层守恒**：指标树每一层、每一张透视表都过 ``assert_conservation``，
   任一失败即整次失败（不返回"部分正确"的下钻结果）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.sandbox.runner import SandboxRunner
from app.services.analysis import (
    DEFAULT_TARGETS,
    AnalysisReport,
    AnalysisRequest,
    run_analysis,
)
from app.services.decomposition import (
    MAX_DRILLDOWN_DIMENSIONS,
    DecompositionError,
    DimensionPivot,
    MetricEngine,
    Period,
    SliceFilter,
)


@dataclass(frozen=True)
class DrilldownRequest:
    """一次下钻的输入：收紧后的锁定切片 + 要透视的维度组合。"""

    scenario: str
    base: Period
    current: Period
    slice_filter: SliceFilter
    dimensions: tuple[tuple[str, ...], ...]
    top_n: int = 5
    title: str = ""
    actor: str = "analyst"
    message: str = ""
    observation_days: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "base": self.base.as_dict(),
            "current": self.current.as_dict(),
            "slice": json.loads(self.slice_filter.as_json()),
            "dimensions": [list(item) for item in self.dimensions],
            "top_n": self.top_n,
            "title": self.title,
            "message": self.message,
        }


@dataclass
class DrilldownReport:
    """一次下钻的完整产出：L1 复核 + 透视表 + 逐层守恒 + 关键变化特征。"""

    request: DrilldownRequest
    analysis: AnalysisReport
    pivots: list[DimensionPivot]
    conservation: list[dict[str, Any]]
    highlights: list[str]
    conclusion: dict[str, Any]
    steps: list[dict[str, Any]]
    duration_ms: int
    inherited_slice: SliceFilter
    narrowed: bool

    @property
    def conserved(self) -> bool:
        return all(item["passed"] for item in self.conservation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.as_dict(),
            "inherited_slice": json.loads(self.inherited_slice.as_json()),
            "narrowed": self.narrowed,
            "conserved": self.conserved,
            "conservation": self.conservation,
            "pivots": [item.as_dict() for item in self.pivots],
            "highlights": self.highlights,
            "conclusion": self.conclusion,
            "analysis": self.analysis.as_dict(),
            "steps": self.steps,
            "duration_ms": self.duration_ms,
        }

    def render(self) -> str:
        """人话版下钻报告（演示脚本与业务讲解卡共用）。"""

        locked = _show(self.request.slice_filter)
        before = _show(self.inherited_slice)
        lines = [
            f"【L2 下钻】{self.request.scenario}，锁定切片 {locked}",
            f"下钻前切片 {before}"
            + ("　（本次收紧）" if self.narrowed else "　（本次只做透视，未再收紧）"),
            f"耗时 {self.duration_ms} ms，逐层守恒：{'全部通过' if self.conserved else '存在失败'}",
        ]
        for pivot in self.pivots:
            lines.append("")
            lines.append(pivot.render())
        lines.append("")
        lines.append("关键变化特征：")
        lines.extend(f"  · {item}" for item in self.highlights)
        lines.append("")
        lines.append(f"结论：{self.conclusion.get('summary')}")
        for item in self.conclusion.get("next_steps", []):
            lines.append(f"  下一步：{item}")
        return "\n".join(lines)


def _show(slice_filter: SliceFilter) -> str:
    """切片的人话展示（中文不转义，顺序稳定）。"""

    return json.dumps(json.loads(slice_filter.as_json()), ensure_ascii=False)


def default_dimensions(
    engine: MetricEngine, scenario: str, slice_filter: SliceFilter
) -> tuple[tuple[str, ...], ...]:
    """默认透视维度：场景声明的维度里，去掉已被锁死的那些，各取一个单维组合。

    锁定切片已经固定的维度再透一遍没有信息量（它只有一个取值），所以默认跳过。
    """

    tree = engine.tree(scenario)
    remaining = [name for name in tree.dimensions if name not in slice_filter.filters]
    if not remaining:
        # 全部维度都被锁死：退化为对锁定切片本身做一次交叉透视（仍然只收紧、不扩张）
        remaining = list(tree.dimensions)
    return tuple((name,) for name in remaining[:MAX_DRILLDOWN_DIMENSIONS])


def run_drilldown(
    request: DrilldownRequest,
    *,
    engine: MetricEngine | None = None,
    sandbox: SandboxRunner | None = None,
    settings: Settings | None = None,
    inherited_slice: SliceFilter | None = None,
    narrowed: bool = True,
) -> DrilldownReport:
    """把一条 L2 链路跑完，返回结构化下钻报告（是否落库由会话层负责）。"""

    active_engine = engine or MetricEngine(settings)
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

    # ① 在收紧后的切片上复跑 L1：分解、假设、验证、事件（新假设自动回 L1 验证流程）
    marker = time.perf_counter()
    analysis = run_analysis(
        AnalysisRequest(
            scenario=request.scenario,
            base=request.base,
            current=request.current,
            slice_filter=request.slice_filter,
            auto_targets=DEFAULT_TARGETS.get(request.scenario, []),
            title=request.title,
            actor=request.actor,
            observation_days=request.observation_days,
            persist=False,
        ),
        engine=active_engine,
        sandbox=active_sandbox,
        settings=active_settings,
    )
    # L1 复核的步骤照搬进 L2 的步骤流：同一条会话里"下钻时又验了哪些假设"要能一条条看到
    steps.extend(dict(step) for step in analysis.steps)

    # ② 逐层守恒：指标树每一层都要留下残差证据
    conservation: list[dict[str, Any]] = [
        {
            "layer": f"指标树 · {node.name}（{node.code}）",
            "source": "decomposition",
            "residual": node.residual,
            "relative_residual": node.relative,
            "tolerance": analysis.decomposition.tolerance,
            "passed": node.relative < analysis.decomposition.tolerance,
        }
        for node in analysis.decomposition.nodes
    ]

    # ③ 维度组合透视（精确优先；缺数据才降级为按占比分摊并标注）
    pivots: list[DimensionPivot] = []
    for dimensions in request.dimensions:
        marker = time.perf_counter()
        pivot = active_engine.dimension_table(
            request.scenario,
            request.base,
            request.current,
            dimensions=dimensions,
            slice_filter=request.slice_filter,
            top_n=request.top_n,
        )
        pivots.append(pivot)
        conservation.append(
            {
                "layer": f"维度透视 · {'／'.join(dimensions)}",
                "source": "dimension_table",
                "residual": pivot.layer_residual,
                "relative_residual": pivot.table.relative_residual,
                "tolerance": pivot.table.tolerance,
                "passed": pivot.table.relative_residual < pivot.table.tolerance,
                "heuristic": pivot.table.heuristic,
                "coverage": pivot.table.coverage,
            }
        )
        record(
            "drilldown",
            "completed",
            {
                "dimensions": list(dimensions),
                "top_n": request.top_n,
                "coverage": pivot.table.coverage,
                "heuristic": pivot.table.heuristic,
                "layer_delta": pivot.layer_delta,
                "rows": pivot.table.as_dict()["rows"],
                "notes": list(pivot.table.notes),
            },
            marker,
        )

    highlights = _highlights(pivots, analysis)
    conclusion = _conclude(pivots, analysis, highlights)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return DrilldownReport(
        request=request,
        analysis=analysis,
        pivots=pivots,
        conservation=conservation,
        highlights=highlights,
        conclusion=conclusion,
        steps=steps,
        duration_ms=duration_ms,
        inherited_slice=inherited_slice or request.slice_filter,
        narrowed=narrowed,
    )


def _highlights(pivots: list[DimensionPivot], analysis: AnalysisReport) -> list[str]:
    """关键变化特征：只讲可核算的事实（贡献额、贡献率、覆盖率），不写形容词。"""

    lines: list[str] = []
    for pivot in pivots:
        rows = pivot.table.contributions
        if not rows:
            lines.append(f"{'／'.join(pivot.dimensions)}：锁定切片内没有可核算的组合")
            continue
        worst = rows[0]
        best = min(rows, key=lambda item: item.contribution)
        name = "／".join(worst.key)
        lines.append(
            f"{'／'.join(pivot.dimensions)}：绝对贡献最大的是 {name}"
            f"（{worst.contribution:+,.0f} 分）；层总变动 {pivot.layer_delta:+,.0f} 分；"
            f"TOP {pivot.table.top_n} 覆盖率 {pivot.table.coverage:.1%}"
        )
        if best.key != worst.key:
            lines.append(
                f"{'／'.join(pivot.dimensions)}：拖累最深的是 {'／'.join(best.key)}"
                f"（{best.contribution:+,.0f} 分）"
            )
        if pivot.table.heuristic:
            lines.append(f"{'／'.join(pivot.dimensions)}：该表是占比分摊口径，属启发式、非唯一解")
    verified = [item for item in analysis.hypotheses if item.status == "verified"]
    lines.append(
        f"在收紧后的切片上复跑 L1：生成假设 {len(analysis.hypotheses)} 条、"
        f"通过验证 {len(verified)} 条（新结论依赖新假设时，走的就是这条验证流程）"
    )
    return lines


def _conclude(
    pivots: list[DimensionPivot], analysis: AnalysisReport, highlights: list[str]
) -> dict[str, Any]:
    top_rows: list[dict[str, Any]] = []
    for pivot in pivots:
        if not pivot.table.contributions:
            continue
        item = pivot.table.contributions[0]
        top_rows.append(
            {
                "dimensions": list(pivot.dimensions),
                "key": list(item.key),
                "contribution": item.contribution,
                "rate": item.share_of_change,
            }
        )
    primary = top_rows[0] if top_rows else None
    return {
        "summary": (
            "锁定切片内没有可核算的维度组合"
            if primary is None
            else (
                f"下钻切片 {'／'.join(primary['key'])}（{primary['dimensions'][0]}）"
                f"贡献 {primary['contribution']:+,.0f} 分，"
                f"贡献率 {'—' if primary['rate'] is None else f'{primary['rate']:+.1%}'}，"
                "口径见透视表（精确计算优先，分摊必标注）"
            )
        ),
        "dimension_contributions": top_rows,
        "verified_hypotheses": [
            item.hypothesis.statement
            for item in analysis.hypotheses
            if item.status == "verified"
        ],
        "highlights": highlights,
        "next_steps": [
            "需要继续下钻时，在本会话上再收紧一次切片（只能收紧，放宽会被拒绝）",
            "要进入推演与报告：What-If 只接受可干预因子，报告会引用这里的透视表作为第 3 段证据",
        ],
        "disclaimer": (
            "透视表贡献由数仓分组取数精确计算，并与指标树在同一层交叉守恒；"
            "标记 heuristic 的表是按占比分摊的启发式结果，不唯一"
        ),
    }


def parse_dimensions(raw: Any) -> tuple[tuple[str, ...], ...]:
    """把接口层传来的维度组合解析成元组列表（1~3 维，形状错误直接报错）。"""

    if raw is None:
        return ()
    items = list(raw)
    parsed: list[tuple[str, ...]] = []
    for item in items:
        names = (item,) if isinstance(item, str) else tuple(str(name) for name in item)
        if not names:
            raise DecompositionError("维度组合不能为空")
        if len(names) > MAX_DRILLDOWN_DIMENSIONS:
            raise DecompositionError(
                f"单次交叉最多 {MAX_DRILLDOWN_DIMENSIONS} 个维度（需求说明书 §5.3）：{list(names)}"
            )
        parsed.append(names)
    return tuple(parsed)


__all__ = [
    "DrilldownReport",
    "DrilldownRequest",
    "default_dimensions",
    "parse_dimensions",
    "run_drilldown",
]
