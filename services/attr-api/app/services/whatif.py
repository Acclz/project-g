"""What-If 推演（需求说明书 §5.8、技术规格 §5.6）——L3 链路的前半段。

一句话：**只允许对可干预因子调参，输出区间与前提条件，不输出"预计提升 12.3%"式的承诺值。**

三条规则在这里落地：

1. **可干预白名单来自指标字典**（``corpus/warehouse/metrics.yaml`` 的 ``intervenable_factors``）。
   名单外的因子（宏观、天气、竞品动作）一律拒绝作为推演输入——它们只能作为情景注释。
2. **弹性的分子分母都有出处**：目标指标用指标树根节点的 SQL，因子用本文档下方登记的
   "可观测代理"（每个代理都写明口径与为什么可以代理），估弹性本身在
   ``packages/attribution/elasticity.py``（纯算法，固定种子，可复现）。
3. **估不出来就说估不出来**：某个可干预因子如果数仓里没有对应口径（例如电商没有佣金入仓），
   直接拒绝并给出理由，而不是编一条曲线。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.config import Settings
from app.services.decomposition import (
    MAX_DRILLDOWN_DIMENSIONS,
    DecompositionError,
    MetricEngine,
    Period,
    SliceFilter,
)
from attribution import (
    ElasticityError,
    ElasticityEstimate,
    ScenarioCurve,
    estimate_elasticity,
    simulate_curve,
)


class WhatIfError(ValueError):
    """推演请求不合法（因子不可干预、无可观测代理、参数越界等）。"""


@dataclass(frozen=True)
class Knob:
    """一个可干预因子在数仓里的**可观测代理**：口径、SQL 与说明都写在这里。"""

    code: str
    name: str
    proxy_label: str
    expression: str
    proxy_note: str
    extra_joins: tuple[str, ...] = ()
    estimable: bool = True
    reject_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "proxy_label": self.proxy_label,
            "proxy_note": self.proxy_note,
            "estimable": self.estimable,
            "reject_reason": self.reject_reason,
        }


PAID_CHANNEL_JOIN = (
    "LEFT JOIN dw.dim_channel AS proxy_channel ON fact.channel_id = proxy_channel.channel_id"
)

#: 可干预因子 → 可观测代理。代理 SQL 与指标字典同构（都用 ``fact.`` 前缀的聚合表达式）。
KNOBS: dict[tuple[str, str], Knob] = {
    ("ecom", "price_index"): Knob(
        code="price_index",
        name="价格水平",
        proxy_label="件单价（元）",
        expression="1.0 * SUM(fact.gmv_cents) / NULLIF(SUM(fact.units), 0)",
        proxy_note="件单价 = GMV ÷ 件数，是价格水平在数仓里唯一可观测的对应量",
    ),
    ("ecom", "budget_share"): Knob(
        code="budget_share",
        name="预算分配",
        proxy_label="付费渠道访客占比",
        expression=(
            "1.0 * SUM(CASE WHEN proxy_channel.is_paid = 1 THEN fact.visitors ELSE 0 END)"
            " / NULLIF(SUM(fact.visitors), 0)"
        ),
        proxy_note="投放预算没进仓，用付费渠道访客占比代理：预算投向哪里，流量结构就往哪里偏",
        extra_joins=(PAID_CHANNEL_JOIN,),
    ),
    ("ecom", "commission_rate"): Knob(
        code="commission_rate",
        name="渠道佣金率",
        proxy_label="",
        expression="",
        proxy_note="",
        estimable=False,
        reject_reason="电商事实表没有佣金口径（佣金只在快消场景入仓），没有历史序列就不能估弹性",
    ),
    ("fmcg", "price_index"): Knob(
        code="price_index",
        name="价格水平",
        proxy_label="平均单价（元）",
        expression="1.0 * SUM(fact.revenue_cents) / NULLIF(SUM(fact.units), 0)",
        proxy_note="平均单价 = 营收 ÷ 销量，对应快消的出厂价水平",
    ),
    ("fmcg", "commission_rate"): Knob(
        code="commission_rate",
        name="渠道佣金率",
        proxy_label="佣金率",
        expression=(
            "1.0 * SUM(fact.channel_commission_cents) / NULLIF(SUM(fact.revenue_cents), 0)"
        ),
        proxy_note="佣金率 = 渠道佣金 ÷ 营收，与指标树的 commission_rate 节点同口径",
    ),
    ("fmcg", "logistics_mode"): Knob(
        code="logistics_mode",
        name="物流模式",
        proxy_label="单件物流成本（元）",
        expression="1.0 * SUM(fact.logistics_cents) / NULLIF(SUM(fact.units), 0)",
        proxy_note="物流模式没有直接字段，用单件物流成本代理：换模式最终体现在单件履约成本上",
    ),
}

#: 区间扫描的默认档位（-30% ~ +30%，步长 5%；0% 是基准点）
DEFAULT_ADJUSTMENTS: tuple[float, ...] = (
    -0.30,
    -0.20,
    -0.10,
    0.0,
    0.10,
    0.20,
    0.30,
)


@dataclass(frozen=True)
class WhatIfRequest:
    """一次推演的输入：可干预因子 + 目标切片 + 调整档位。"""

    scenario: str
    base: Period
    current: Period
    factor: str
    slice_filter: SliceFilter = field(default_factory=SliceFilter)
    adjustments: tuple[float, ...] = DEFAULT_ADJUSTMENTS
    dimensions: tuple[tuple[str, ...], ...] = ()
    window_days: int | None = None
    max_adjustment: float | None = None
    title: str = ""
    actor: str = "analyst"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "base": self.base.as_dict(),
            "current": self.current.as_dict(),
            "factor": self.factor,
            "slice": json.loads(self.slice_filter.as_json()),
            "adjustments": list(self.adjustments),
            "dimensions": [list(item) for item in self.dimensions],
            "window_days": self.window_days,
            "max_adjustment": self.max_adjustment,
        }


@dataclass
class WhatIfReport:
    """一次推演的产出：聚合曲线 + 分维度曲线 + 前提条件 + 把握度 + 输入回显。"""

    request: WhatIfRequest
    knob: Knob
    target_metric: dict[str, Any]
    window: Period
    curve: ScenarioCurve
    breakdown: list[dict[str, Any]]
    assumptions: list[str]
    warnings: list[str]
    steps: list[dict[str, Any]]
    duration_ms: int

    @property
    def confidence(self) -> float:
        return self.curve.elasticity.confidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.as_dict(),
            "factor": self.knob.as_dict(),
            "target_metric": self.target_metric,
            "window": self.window.as_dict(),
            "curve": self.curve.as_dict(),
            "breakdown": self.breakdown,
            "assumptions": self.assumptions,
            "warnings": self.warnings,
            "confidence": self.confidence,
            "steps": self.steps,
            "duration_ms": self.duration_ms,
        }

    def render(self) -> str:
        slice_text = _show(self.request.slice_filter)
        lines = [
            f"【What-If】{self.request.scenario} · {self.knob.name}"
            f"（代理口径：{self.knob.proxy_label}）",
            f"目标指标 {self.target_metric['name']}（{self.target_metric['code']}），"
            f"切片 {slice_text}",
            f"弹性窗口 {self.window.start}~{self.window.end}（{self.window.days} 天）",
            self.curve.render(),
        ]
        for item in self.breakdown:
            curve = item.get("curve")
            if curve is None:
                lines.append(
                    f"  分维度（{'／'.join(item['dimensions'])}：{item['key']}）："
                    f"未估计（{item['reason']}）"
                )
                continue
            lines.append(
                f"  分维度（{'／'.join(item['dimensions'])}：{item['key']}）"
                f"基准 {item['base_value']:,.0f}，弹性 {curve['elasticity']['value']:+.3f}"
                f"（{curve['elasticity']['method']}，n={curve['elasticity']['sample_size']}，"
                f"把握度 {curve['elasticity']['confidence']:.0%}）"
            )
        lines.append("前提条件：")
        lines.extend(f"  · {item}" for item in self.assumptions)
        if self.warnings:
            lines.append("警示：")
            lines.extend(f"  ⚠ {item}" for item in self.warnings)
        return "\n".join(lines)


def intervenable_factors(engine: MetricEngine, scenario: str) -> list[dict[str, Any]]:
    """列出该场景的可干预因子与可估性（前端 What-If 面板直接用这份清单）。"""

    tree = engine.tree(scenario)
    declared = list(tree.intervenable_factors)
    if not declared:
        return []
    items: list[dict[str, Any]] = []
    for code in declared:
        knob = KNOBS.get((scenario, code))
        if knob is None:
            items.append(
                {
                    "code": code,
                    "name": code,
                    "proxy_label": "",
                    "proxy_note": "",
                    "estimable": False,
                    "reject_reason": "指标字典声明它可干预，但数仓里没有登记可观测代理，无法估弹性",
                }
            )
            continue
        items.append(knob.as_dict())
    return items


def resolve_knob(engine: MetricEngine, scenario: str, factor: str) -> Knob:
    """校验因子是否可干预、是否有可观测代理；不允许就直说为什么。"""

    tree = engine.tree(scenario)
    declared = list(tree.intervenable_factors)
    if factor not in declared:
        raise WhatIfError(
            f"{factor} 不在 {scenario} 的可干预白名单 {declared} 里："
            "不可干预因子（宏观、天气、竞品动作）只能作为情景注释，不能作为推演输入"
            "（需求说明书 §5.8）"
        )
    knob = KNOBS.get((scenario, factor))
    if knob is None:
        raise WhatIfError(
            f"{factor} 可干预，但数仓里没有登记可观测代理，无法估弹性："
            "要么补一条入仓口径，要么改用别的因子"
        )
    if not knob.estimable:
        raise WhatIfError(f"{factor}：{knob.reject_reason}")
    return knob


def run_whatif(
    request: WhatIfRequest,
    *,
    engine: MetricEngine | None = None,
    settings: Settings | None = None,
) -> WhatIfReport:
    """跑一次推演：估弹性 → 生成区间曲线 → 附前提条件与把握度（是否落库由会话层负责）。"""

    active_engine = engine or MetricEngine(settings)
    active_settings = settings or active_engine.settings
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []
    knob = resolve_knob(active_engine, request.scenario, request.factor)
    tree = active_engine.tree(request.scenario)
    active_slice = request.slice_filter or SliceFilter()
    window_days = request.window_days or active_settings.attr_whatif_window_days
    max_adjustment = (
        active_settings.attr_whatif_max_adjustment
        if request.max_adjustment is None
        else float(request.max_adjustment)
    )
    window = _window(request.current, window_days)

    # ① 取两条序列：目标指标（按天）与因子代理（按天）
    marker = time.perf_counter()
    target_series = active_engine.daily_series(
        request.scenario,
        window,
        slice_filter=active_slice,
    ).get((), [])
    factor_series = active_engine.daily_series(
        request.scenario,
        window,
        expression=knob.expression,
        slice_filter=active_slice,
        extra_joins=knob.extra_joins,
    ).get((), [])
    paired = _pair(target_series, factor_series)
    if len(paired) < 4:
        raise WhatIfError(
            f"窗口 {window.start}~{window.end} 内对齐后的观测点只有 {len(paired)} 个（少于 4 个）："
            "样本太少，不能推演（宁可拒绝，也不给假曲线）"
        )
    steps.append(
        _step(
            "whatif",
            "series",
            {
                "paired_days": len(paired),
                "window": window.as_dict(),
                "factor_proxy": knob.proxy_label,
            },
            marker,
        )
    )

    # ② 估弹性（主路径对数回归，样本不足降级为有限差分；区间用固定种子的 bootstrap）
    marker = time.perf_counter()
    elasticity = estimate_elasticity(
        [item[1] for item in paired],
        [item[2] for item in paired],
        iterations=active_settings.attr_whatif_bootstrap_iterations,
        seed=active_settings.attr_whatif_seed,
    )
    steps.append(
        _step(
            "whatif",
            "elasticity",
            {"factor": knob.code, **elasticity.as_dict()},
            marker,
        )
    )

    # ③ 基准值：目标指标在"现期"（锁定切片）上的取值——曲线的锚点
    marker = time.perf_counter()
    root_code = tree.root.code
    base_values = active_engine.metric_totals(
        request.scenario, request.current, slice_filter=active_slice
    )
    base_value = base_values.get(())
    factor_base_values = active_engine.metric_totals(
        request.scenario,
        request.current,
        slice_filter=active_slice,
        expression=knob.expression,
        extra_joins=knob.extra_joins,
    )
    factor_base = factor_base_values.get(())
    if base_value is None or base_value <= 0:
        raise WhatIfError(
            f"目标指标 {root_code} 在 {request.current.start}~{request.current.end} "
            f"（切片 {active_slice.as_json()}）没有正取值，没有基准就没法推演"
        )
    if factor_base is None or factor_base <= 0:
        raise WhatIfError(
            f"因子代理（{knob.proxy_label}）在同一期间没有正取值，弹性没有定义"
        )
    curve = simulate_curve(
        base_value=float(base_value),
        factor_base=float(factor_base),
        elasticity=elasticity,
        adjustments=request.adjustments,
        max_adjustment=max_adjustment,
    )
    steps.append(
        _step(
            "whatif",
            "curve",
            {
                "base_value": float(base_value),
                "factor_base": float(factor_base),
                "points": [point.as_dict() for point in curve.points],
                "out_of_range": curve.out_of_range,
            },
            marker,
        )
    )

    # ④ 分维度（可选）：按渠道/品类分别估弹性（需求说明书 §5.8「单点弹性」）
    breakdown: list[dict[str, Any]] = []
    if request.dimensions:
        marker = time.perf_counter()
        for dimensions in request.dimensions:
            breakdown.extend(
                _breakdown(active_engine, request, knob, dimensions, window, max_adjustment)
            )
        steps.append(
            _step(
                "whatif",
                "breakdown",
                {"combinations": len(breakdown)},
                marker,
            )
        )

    assumptions = _assumptions(request, knob, window, curve, active_settings)
    warnings = list(curve.warnings)
    warnings.extend(elasticity.notes)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return WhatIfReport(
        request=request,
        knob=knob,
        target_metric={
            "code": root_code,
            "name": tree.root.name,
            "unit": tree.root.unit,
            "caliber": tree.root.caliber,
            "caliber_version": 1,
        },
        window=window,
        curve=curve,
        breakdown=breakdown,
        assumptions=assumptions,
        warnings=list(dict.fromkeys(warnings)),
        steps=steps,
        duration_ms=duration_ms,
    )


def _step(kind: str, status: str, payload: dict[str, Any], since: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "duration_ms": int((time.perf_counter() - since) * 1000),
        "payload": payload,
    }


def _show(slice_filter: SliceFilter) -> str:
    """切片的人话展示（中文不转义，键顺序稳定）。"""

    return json.dumps(json.loads(slice_filter.as_json()), ensure_ascii=False)


def _window(current: Period, days: int) -> Period:
    """弹性估计窗口：现期结束日往前推 ``days`` 天（不足则从数仓最早一天开始）。"""

    end = date.fromisoformat(current.end)
    start = end - timedelta(days=max(days, 1) - 1)
    return Period(start.isoformat(), end.isoformat(), label="弹性估计窗口")


def _pair(
    target: list[tuple[str, float]], factor: list[tuple[str, float]]
) -> list[tuple[str, float, float]]:
    """按天对齐两条序列：只保留两边都有真实观测的日期。"""

    lookup = dict(factor)
    return [
        (day, value, lookup[day])
        for day, value in target
        if day in lookup and lookup[day] is not None
    ]


def _breakdown(
    engine: MetricEngine,
    request: WhatIfRequest,
    knob: Knob,
    dimensions: tuple[str, ...],
    window: Period,
    max_adjustment: float,
) -> list[dict[str, Any]]:
    """按维度组合分别估弹性：同一张曲线上每个组合都有自己的点估计与区间。"""

    names = tuple(str(item) for item in dimensions)
    if len(names) > MAX_DRILLDOWN_DIMENSIONS:
        raise DecompositionError(
            f"单次交叉最多 {MAX_DRILLDOWN_DIMENSIONS} 个维度（需求说明书 §5.3）：{list(names)}"
        )
    active_slice = request.slice_filter or SliceFilter()
    target = engine.daily_series(
        request.scenario, window, slice_filter=active_slice, dimensions=names
    )
    factor = engine.daily_series(
        request.scenario,
        window,
        expression=knob.expression,
        slice_filter=active_slice,
        dimensions=names,
        extra_joins=knob.extra_joins,
    )
    bases = engine.metric_totals(
        request.scenario, request.current, slice_filter=active_slice, dimensions=names
    )
    items: list[dict[str, Any]] = []
    for key in sorted(target):
        entry: dict[str, Any] = {
            "dimensions": list(names),
            "key": list(key),
            "base_value": bases.get(key),
        }
        paired = _pair(target.get(key, []), factor.get(key, []))
        entry["sample_size"] = len(paired)
        base_value = bases.get(key)
        if len(paired) < 4 or base_value is None or base_value <= 0:
            entry["curve"] = None
            entry["reason"] = (
                f"对齐后只有 {len(paired)} 个观测点，或基准值非正：该组合不估弹性"
            )
            items.append(entry)
            continue
        try:
            estimate = estimate_elasticity(
                [item[1] for item in paired], [item[2] for item in paired]
            )
            curve = simulate_curve(
                base_value=float(base_value),
                factor_base=float(paired[-1][2]),
                elasticity=estimate,
                adjustments=request.adjustments,
                max_adjustment=max_adjustment,
            )
        except ElasticityError as error:
            entry["curve"] = None
            entry["reason"] = str(error)
            items.append(entry)
            continue
        entry["curve"] = curve.as_dict()
        items.append(entry)
    return items


def _assumptions(
    request: WhatIfRequest,
    knob: Knob,
    window: Period,
    curve: ScenarioCurve,
    settings: Settings,
) -> list[str]:
    """前提条件：推演结论只在这些条件成立时有效，必须与区间一起给出。"""

    estimate: ElasticityEstimate = curve.elasticity
    slice_text = _show(request.slice_filter or SliceFilter())
    items = [
        "单因子局部均衡：只动这一个因子，其他因子与竞争环境保持不变",
        f"弹性窗口 {window.start}~{window.end}（{window.days} 天，n={estimate.sample_size}）内"
        "目标与因子代理的历史共动关系，在未来同样成立",
        f"因子代理口径：{knob.proxy_label}（{knob.proxy_note}）",
        f"目标指标口径：{request.scenario} 根指标的现期取值，"
        f"切片 {slice_text}",
        f"区间为 bootstrap {estimate.iterations} 次（种子 {estimate.seed}，"
        f"置信水平 {estimate.level:.0%}）的百分位区间，可复现：同一输入必得同一输出",
        f"调整档位限制在 ±{curve.max_adjustment:.0%}（配置项 attr_whatif_max_adjustment="
        f"{settings.attr_whatif_max_adjustment:g}），超出的档位只算外推",
        "弹性是历史共动的映射，不是因果保证：它回答「调了会怎样」，不回答「为什么会这样」",
    ]
    if estimate.degraded:
        items.append("本次走了降级路径（有限差分）：样本不足，区间已放宽，把握度上限 0.5")
    return items


__all__ = [
    "DEFAULT_ADJUSTMENTS",
    "KNOBS",
    "Knob",
    "WhatIfError",
    "WhatIfReport",
    "WhatIfRequest",
    "intervenable_factors",
    "resolve_knob",
    "run_whatif",
]
