"""分解链路：从数仓取数 → 逐层分解 → 守恒断言（技术规格 §5）。

分层边界（`02-流程与规范` §2.1）：

* **算法**在 ``packages/attribution``：差额分析、LMDI、守恒断言、逐层调度；
* **本模块是用例层**：把指标字典里的 SQL 跑成两期取值、把结果整理成可读报告；
* 路由层只做参数校验与响应组装，禁止在这里写 SQL 文本或分解公式。

三条硬约束：

1. 数值只能来自数仓与算法包——大模型输出永远不进数值链路；
2. 每层独立守恒，断言失败即整次失败（不返回"部分正确"的结果）；
3. 取数 SQL 只来自 ``corpus/warehouse/metrics.yaml``，此处不另写一份。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, Settings, get_settings
from app.db import connect_warehouse_readonly
from attribution import (
    ContributionTable,
    MetricNode,
    MetricTree,
    TreeResult,
    allocate_by_share,
    assert_conservation,
    conservation_residual,
    contribution_rates,
    decompose_tree,
    exact_contributions,
    load_metrics_file,
)
from attribution import DecompositionError as AttributionError

METRICS_PATH = REPO_ROOT / "corpus" / "warehouse" / "metrics.yaml"

#: 维度编码 → （维度表, 主键列）：用于把"编码"翻译成事实表里的 id
DIM_LOOKUP: dict[str, tuple[str, str]] = {
    "channel": ("dw.dim_channel", "channel_id"),
    "category": ("dw.dim_category", "category_id"),
    "region": ("dw.dim_region", "region_id"),
    "segment": ("dw.dim_segment", "segment_id"),
    "sku": ("dw.dim_sku", "sku_id"),
}

#: 各场景事实表里直接可用的维度列（没有的维度走子查询换算，见 ``_slice_clause``）
FACT_DIM_COLUMNS: dict[str, dict[str, str]] = {
    "ecom": {
        "channel": "channel_id",
        "category": "category_id",
        "region": "region_id",
        "segment": "segment_id",
    },
    "fmcg": {"channel": "channel_id", "region": "region_id", "sku": "sku_id"},
}

#: 单次交叉最多几个维度（需求说明书 §5.3：控制组合爆炸）
MAX_DRILLDOWN_DIMENSIONS = 3


class DecompositionError(ValueError):
    """用例层失败（切片无数据、场景不存在、指标字典缺 SQL 等）。"""


@dataclass(frozen=True)
class Period:
    """对比期间：``[start, end]`` 闭区间，日期格式 ``YYYY-MM-DD``。"""

    start: str
    end: str
    label: str = ""

    def __post_init__(self) -> None:
        start = date.fromisoformat(self.start)
        end = date.fromisoformat(self.end)
        if end < start:
            raise DecompositionError(f"期间起止顺序不对：{self.start} ~ {self.end}")

    @property
    def days(self) -> int:
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1

    def previous(self) -> Period:
        """紧挨着的上一个等长期间（环比对照）。"""

        start = date.fromisoformat(self.start) - timedelta(days=self.days)
        end = date.fromisoformat(self.start) - timedelta(days=1)
        return Period(start.isoformat(), end.isoformat(), label="上一期")

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "days": self.days, "label": self.label}


@dataclass(frozen=True)
class SliceFilter:
    """维度切片：``{"channel": ["paid_ads"]}`` 这样的编码过滤，对应真值清单里的 dimension_json。"""

    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: str | dict[str, Any] | None) -> SliceFilter:
        if not payload:
            return cls()
        raw = json.loads(payload) if isinstance(payload, str) else payload
        return cls({key: tuple(values) for key, values in raw.items()})

    @property
    def empty(self) -> bool:
        return not self.filters

    def as_json(self) -> str:
        return json.dumps(
            {key: list(values) for key, values in self.filters.items()},
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass
class NodeReport:
    """一层分解的结果：两期取值、贡献量、贡献率与残差。"""

    code: str
    name: str
    method: str
    base_total: float
    current_total: float
    delta: float
    contributions: dict[str, float]
    rates: dict[str, float | None]
    residual: float
    relative: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        ranked = sorted(self.contributions.items(), key=lambda item: abs(item[1]), reverse=True)
        return {
            "code": self.code,
            "name": self.name,
            "method": self.method,
            "base_total": self.base_total,
            "current_total": self.current_total,
            "delta": self.delta,
            "residual": self.residual,
            "relative_residual": self.relative,
            "contributions": [
                {
                    "factor": factor,
                    "contribution": value,
                    "rate": self.rates.get(factor),
                }
                for factor, value in ranked
            ],
            "detail": self.detail,
        }


@dataclass
class DecompositionReport:
    """一次完整分解的报告：口径、期间、切片、各层结果与最大残差。"""

    scenario: str
    scenario_name: str
    metric_code: str
    metric_name: str
    base: Period
    current: Period
    slice_filter: SliceFilter
    tolerance: float
    nodes: list[NodeReport]
    #: 因零值等前置条件不成立而**未下钻**的节点及原因（零值预案的可核对形式）
    skipped_targets: list[dict[str, str]] = field(default_factory=list)
    #: 每层都已通过 ``assert_conservation``（唯一的浮点比较入口）后才会置为 True
    conserved: bool = True

    @property
    def root(self) -> NodeReport:
        return self.nodes[0]

    @property
    def max_residual(self) -> float:
        """最大绝对残差（单位与指标口径一致，用于展示而非判定）。"""

        return max((abs(node.residual) for node in self.nodes), default=0.0)

    @property
    def max_relative_residual(self) -> float:
        """最大相对误差——判定口径就是它，分母固定 ``max(|Δ|, 1)``。"""

        return max((node.relative for node in self.nodes), default=0.0)

    @property
    def ok(self) -> bool:
        """是否守恒成立：由 ``assert_conservation``（唯一浮点比较入口）的结论决定。"""

        return self.conserved

    def top(self, limit: int = 3) -> list[dict[str, Any]]:
        """根节点贡献 Top-N（面试与报告都从这张表开始讲）。"""

        return self.root.as_dict()["contributions"][:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "scenario_name": self.scenario_name,
            "metric": {"code": self.metric_code, "name": self.metric_name},
            "base": self.base.as_dict(),
            "current": self.current.as_dict(),
            "slice": json.loads(self.slice_filter.as_json()),
            "tolerance": self.tolerance,
            "max_residual": self.max_residual,
            "max_relative_residual": self.max_relative_residual,
            "ok": self.ok,
            "nodes": [node.as_dict() for node in self.nodes],
            "skipped_targets": self.skipped_targets,
        }

    def render(self) -> str:
        """人类可读版（演示脚本与接口响应共用同一份数据结构）。"""

        lines = [
            f"【{self.scenario_name}】{self.metric_name}（{self.metric_code}）",
            f"基期 {self.base.start}~{self.base.end}（{self.base.days} 天）"
            f" → 现期 {self.current.start}~{self.current.end}（{self.current.days} 天）",
            f"切片 {self.slice_filter.as_json()}　容差 {self.tolerance:g}"
            f"　最大残差 {self.max_residual:.3e}",
            f"最大相对误差 {self.max_relative_residual:.3e}（判定口径：分母 max(|Δ|, 1)）",
        ]
        for node in self.nodes:
            direction = "↑" if node.delta > 0 else ("↓" if node.delta < 0 else "→")
            lines.append(
                f"\n── {node.name}（{node.code}，{node.method}）"
                f" {_format_value(node.base_total)} → {_format_value(node.current_total)}"
                f" {direction} {_format_value(node.delta, signed=True)}"
            )
            for item in node.as_dict()["contributions"]:
                rate = "—" if item["rate"] is None else f"{item['rate']:.1%}"
                contribution = _format_value(item["contribution"], signed=True)
                lines.append(
                    f"   {item['factor']:<20} {contribution:>18}　贡献率 {rate}"
                )
        if self.skipped_targets:
            lines.append("\n未下钻的节点（零值预案）：")
            for item in self.skipped_targets:
                lines.append(f"   {item['code']}：{item['reason']}")
        return "\n".join(lines)


@dataclass
class DimensionPivot:
    """一次维度下钻的透视表：锁定切片之下、按维度组合精确计算的贡献（技术规格 §5.5）。

    ``layer_delta`` 是**指标树在同一切片上**给出的总变动（独立来源），透视图的总变动必须与它
    一致——两个来源对不上就直接抛守恒错误，不允许"看起来差不多"就放过。
    """

    scenario: str
    dimensions: tuple[str, ...]
    slice_filter: SliceFilter
    base: Period
    current: Period
    table: ContributionTable
    layer_base: float
    layer_current: float
    layer_delta: float
    layer_residual: float

    @property
    def coverage(self) -> float:
        return self.table.coverage

    @property
    def conserved(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "dimensions": list(self.dimensions),
            "slice": json.loads(self.slice_filter.as_json()),
            "base": self.base.as_dict(),
            "current": self.current.as_dict(),
            "layer_delta": self.layer_delta,
            "layer_residual": self.layer_residual,
            "conserved": self.conserved,
            **self.table.as_dict(),
        }

    def render(self, *, limit: int | None = None) -> str:
        return self.table.render(limit=limit)


class MetricEngine:
    """指标分解引擎：指标字典是唯一来源，取数与分解都从这里过。"""

    def __init__(
        self, settings: Settings | None = None, metrics_path: Path | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.metrics_path = Path(metrics_path) if metrics_path else METRICS_PATH
        self.trees: dict[str, MetricTree] = load_metrics_file(self.metrics_path)

    # ---------------------------------------------------------------- 元信息

    def scenarios(self) -> list[str]:
        return sorted(self.trees)

    def tree(self, scenario: str) -> MetricTree:
        if scenario not in self.trees:
            raise DecompositionError(
                f"未知场景 {scenario}（可用：{', '.join(self.scenarios())}）"
            )
        return self.trees[scenario]

    def fact_table(self, scenario: str) -> str:
        tables = self.tree(scenario).raw.get("fact_tables") or []
        if not tables:
            raise DecompositionError(f"场景 {scenario} 未声明 fact_tables")
        return str(tables[0])

    def describe(self) -> dict[str, Any]:
        """给指标字典页用的结构清单（场景 → 指标节点）。"""

        payload: dict[str, Any] = {}
        for scenario, tree in self.trees.items():
            payload[scenario] = {
                "name": tree.scenario_name,
                "fact_table": self.fact_table(scenario),
                "dimensions": tree.dimensions,
                "root": tree.root.code,
                "metrics": [
                    {
                        "code": node.code,
                        "name": node.name,
                        "level": node.level,
                        "structure": node.structure,
                        "method": node.method,
                        "sign": node.sign,
                        "formula": node.formula,
                        "unit": node.unit,
                        "caliber": node.caliber,
                        "decomposable": node.decomposable,
                        "children": [child.code for child in node.children],
                        "has_sql": bool(node.sql),
                    }
                    for node in tree.nodes()
                ],
            }
        return payload

    # ------------------------------------------------------------------ 取数

    def period_values(
        self,
        scenario: str,
        period: Period,
        slice_filter: SliceFilter | None = None,
        *,
        connection=None,
    ) -> dict[str, float]:
        """按指标字典逐个节点取数（每个节点一条 SQL，返回 {指标编码: 值}）。"""

        tree = self.tree(scenario)
        own_connection = connection is None
        conn = connection or connect_warehouse_readonly(self.settings.warehouse_db)
        try:
            where = self._slice_clause(conn, scenario, slice_filter or SliceFilter())
            values: dict[str, float] = {}
            for node in tree.nodes():
                if not node.sql:
                    raise DecompositionError(f"指标 {node.code} 没有 SQL，无法取数")
                query = (
                    f"SELECT {node.sql} FROM {self.fact_table(scenario)} AS fact"
                    f" WHERE fact.day BETWEEN ? AND ?{where}"
                )
                row = conn.execute(query, (period.start, period.end)).fetchone()
                raw = None if row is None else row[0]
                if raw is None:
                    raise DecompositionError(
                        f"指标 {node.code} 在 {period.start}~{period.end}"
                        f"（切片 {slice_filter.as_json() if slice_filter else '{}'}）无数据"
                    )
                values[node.code] = float(raw)
            return values
        finally:
            if own_connection:
                conn.close()

    def _slice_clause(self, conn, scenario: str, slice_filter: SliceFilter) -> str:
        """把维度切片翻译成 SQL 条件（维度编码先换成真实 id，不做字符串拼 SQL）。"""

        clauses: list[str] = []
        fact_columns = FACT_DIM_COLUMNS.get(scenario, {})
        for dimension, codes in slice_filter.filters.items():
            if dimension not in DIM_LOOKUP:
                raise DecompositionError(f"未知维度：{dimension}")
            ids = self._dimension_ids(conn, dimension, codes)
            if dimension in fact_columns:
                clauses.append(f" AND fact.{fact_columns[dimension]} IN ({_int_list(ids)})")
                continue
            if scenario == "fmcg" and dimension == "category":
                # 快消事实表没有 category_id：经 dim_sku 折算一次
                clauses.append(
                    " AND fact.sku_id IN (SELECT sku_id FROM dw.dim_sku"
                    f" WHERE category_id IN ({_int_list(ids)}))"
                )
                continue
            raise DecompositionError(f"场景 {scenario} 的事实表不支持按 {dimension} 切片")
        return "".join(clauses)

    @staticmethod
    def _dimension_ids(conn, dimension: str, codes: tuple[str, ...]) -> list[int]:
        table, key = DIM_LOOKUP[dimension]
        if not codes:
            raise DecompositionError(f"维度 {dimension} 的切片为空")
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT {key} FROM {table} WHERE code IN ({placeholders})", tuple(codes)
        ).fetchall()
        ids = sorted({int(row[0]) for row in rows})
        if len(ids) != len(set(codes)):
            known = {str(row[0]) for row in rows}
            raise DecompositionError(
                f"维度 {dimension} 存在无效编码：{sorted(set(codes) - known)}"
            )
        return ids

    def validate_sql(self, scenario: str, period: Period) -> list[dict[str, Any]]:
        """逐节点执行一次 SQL：返回每个节点是否出数（指标字典校验接口用）。"""

        tree = self.tree(scenario)
        conn = connect_warehouse_readonly(self.settings.warehouse_db)
        checks: list[dict[str, Any]] = []
        try:
            for node in tree.nodes():
                entry: dict[str, Any] = {"code": node.code, "ok": False, "value": None}
                if not node.sql:
                    entry["problem"] = "缺少 SQL"
                    checks.append(entry)
                    continue
                try:
                    row = conn.execute(
                        f"SELECT {node.sql} FROM {self.fact_table(scenario)} AS fact"
                        " WHERE fact.day BETWEEN ? AND ?",
                        (period.start, period.end),
                    ).fetchone()
                except Exception as error:  # noqa: BLE001 - 校验接口要把错误原样报给使用者
                    entry["problem"] = str(error)
                    checks.append(entry)
                    continue
                value = None if row is None else row[0]
                entry["value"] = value
                entry["ok"] = value is not None
                if value is None:
                    entry["problem"] = "返回 NULL（该期间无数据）"
                checks.append(entry)
        finally:
            conn.close()
        return checks

    # ------------------------------------------------------------------ 分解

    def decompose(
        self,
        scenario: str,
        base: Period,
        current: Period,
        *,
        slice_filter: SliceFilter | None = None,
        targets: list[str] | None = None,
        auto_targets: list[str] | None = None,
        tolerance: float | None = None,
    ) -> DecompositionReport:
        """两期对比的逐层分解；任一层守恒失败即抛错（不返回半成品）。

        ``targets`` 是**严格**下钻：指定了就要求该节点两期的子因子都严格为正，
        否则按 LMDI 的前置条件报错（这是有意的——不静默改口径）。
        ``auto_targets`` 是**按预案筛选**下钻：取值非正的节点不拆，并在报告里写明原因，
        对应 `01-技术规格` §5.2 的零值预案（例如线下渠道佣金结构性为 0）。
        """

        active_slice = slice_filter or SliceFilter()
        active_tolerance = (
            self.settings.attr_conservation_tolerance if tolerance is None else tolerance
        )
        tree = self.tree(scenario)
        conn = connect_warehouse_readonly(self.settings.warehouse_db)
        try:
            base_values = self.period_values(scenario, base, active_slice, connection=conn)
            current_values = self.period_values(scenario, current, active_slice, connection=conn)
        finally:
            conn.close()
        accepted_targets, skipped_targets = self._screen_targets(
            scenario, base_values, current_values, auto_targets
        )
        result: TreeResult = decompose_tree(
            tree,
            base_values,
            current_values,
            targets=targets or accepted_targets,
            tolerance=active_tolerance,
        )
        nodes = [self._node_report(tree, result, code) for code in result.order]
        # 再显式过一遍全项目统一的守恒断言：这是"整次分解成立"的唯一判据
        for node in nodes:
            assert_conservation(list(node.contributions.values()), node.delta, active_tolerance)
        return DecompositionReport(
            scenario=scenario,
            scenario_name=tree.scenario_name,
            metric_code=tree.root.code,
            metric_name=tree.root.name,
            base=base,
            current=current,
            slice_filter=active_slice,
            tolerance=active_tolerance,
            nodes=nodes,
            skipped_targets=skipped_targets,
            conserved=True,
        )

    def dimension_table(
        self,
        scenario: str,
        base: Period,
        current: Period,
        *,
        dimensions: Sequence[str] = ("channel",),
        slice_filter: SliceFilter | None = None,
        top_n: int = 5,
        max_dimensions: int = MAX_DRILLDOWN_DIMENSIONS,
        tolerance: float | None = None,
        connection=None,
    ) -> DimensionPivot:
        """按维度组合做透视：每个组合的贡献**精确**来自数仓分组取数（不靠分摊凑）。

        口径（技术规格 §5.5 优先路径）：根指标对事实行可加，所以按组合分组直接算差值就能得到
        ``Σ组合贡献 = 层总变动``。换来的硬约束是：透视表的总变动必须等于指标树在同一切片上
        给出的总变动，两者不一致就抛守恒错误（``expected_delta`` 那条交叉校验）。

        只有某组合取值为 NULL（缺数据、因子在该组合内无法定义）时才降级为按占比分摊，
        并如实标记 ``heuristic=True``。
        """

        active_slice = slice_filter or SliceFilter()
        active_tolerance = (
            self.settings.attr_conservation_tolerance if tolerance is None else tolerance
        )
        names = tuple(str(item) for item in dimensions)
        tree = self.tree(scenario)
        if not names:
            raise DecompositionError("下钻至少要指定一个维度")
        if len(set(names)) != len(names):
            raise DecompositionError(f"下钻维度有重复项：{list(names)}")
        if len(names) > max_dimensions:
            raise DecompositionError(
                f"单次交叉最多 {max_dimensions} 个维度（需求说明书 §5.3），"
                f"收到 {len(names)} 个：{list(names)}"
            )
        # 允许下钻的维度跟"能不能切片"用同一个能力口径（``_slice_clause`` / ``_group_sources``）：
        # 快消的 category 虽然没写进场景 dimensions，但事实表经 dim_sku 折算后确实支持，
        # 把它挡在门外只是把"能算的"说成"不能算"，反而误导。
        supported = self.supported_group_dimensions(scenario)
        unsupported = [name for name in names if name not in supported]
        if unsupported:
            raise DecompositionError(
                f"场景 {scenario} 不支持这些维度下钻：{unsupported}"
                f"（可用：{list(supported)}）"
            )

        own_connection = connection is None
        conn = connection or connect_warehouse_readonly(self.settings.warehouse_db)
        try:
            selects, joins, groups = self._group_sources(scenario, names)
            where = self._slice_clause(conn, scenario, active_slice)
            query = (
                f"SELECT {', '.join(selects)}, {tree.root.sql} AS value"
                f" FROM {self.fact_table(scenario)} AS fact"
                f" {' '.join(joins)}"
                f" WHERE fact.day BETWEEN ? AND ?{where}"
                f" GROUP BY {', '.join(groups)}"
            )
            base_rows = self._group_rows(conn, query, base)
            current_rows = self._group_rows(conn, query, current)
            layer_base = self._root_value(conn, scenario, base, active_slice)
            layer_current = self._root_value(conn, scenario, current, active_slice)
        finally:
            if own_connection:
                conn.close()

        layer_delta = layer_current - layer_base
        missing = sorted(
            [list(key) for key, value in base_rows.items() if value is None]
            + [list(key) for key, value in current_rows.items() if value is None]
        )
        if missing:
            # 降级路径：按该组合在层内的占比分摊层总变动，并如实标成启发式
            shares = {
                key: (value or 0.0) for key, value in current_rows.items() if value is not None
            }
            table = allocate_by_share(
                layer_delta,
                shares,
                dimensions=names,
                top_n=top_n,
                tolerance=active_tolerance,
                max_dimensions=max_dimensions,
                note=(
                    f"这些组合取值为 NULL（缺数据）：{missing}；"
                    "按现期占比分摊层总变动，属启发式、非唯一解"
                ),
            )
            layer_residual = 0.0
        else:
            table = exact_contributions(
                {key: value for key, value in base_rows.items() if value is not None},
                {key: value for key, value in current_rows.items() if value is not None},
                dimensions=names,
                top_n=top_n,
                tolerance=active_tolerance,
                max_dimensions=max_dimensions,
                expected_delta=layer_delta,
            )
            layer_residual = table.residual
        return DimensionPivot(
            scenario=scenario,
            dimensions=names,
            slice_filter=active_slice,
            base=base,
            current=current,
            table=table,
            layer_base=layer_base,
            layer_current=layer_current,
            layer_delta=layer_delta,
            layer_residual=layer_residual,
        )

    @staticmethod
    def supported_group_dimensions(scenario: str) -> list[str]:
        """该场景事实表真正支持的分组维度（与 ``_slice_clause`` 的能力集一致）。"""

        fact_columns = FACT_DIM_COLUMNS.get(scenario, {})
        names = [name for name in DIM_LOOKUP if name in fact_columns]
        if scenario == "fmcg":
            names.append("category")  # 经 dw.dim_sku 折算，见 _slice_clause
        return sorted(names)

    def _group_sources(
        self, scenario: str, dimensions: tuple[str, ...]
    ) -> tuple[list[str], list[str], list[str]]:
        """把维度名翻译成"分组列 + 维表 join"：编码经维表取，SQL 里不拼用户字符串。"""

        fact_columns = FACT_DIM_COLUMNS.get(scenario, {})
        selects: list[str] = []
        joins: list[str] = []
        groups: list[str] = []
        joined: set[str] = set()
        for dimension in dimensions:
            if dimension not in DIM_LOOKUP:
                raise DecompositionError(f"未知维度：{dimension}")
            table, key = DIM_LOOKUP[dimension]
            alias = f"dim_{dimension}"
            if dimension in fact_columns:
                expression = f"fact.{fact_columns[dimension]}"
            elif scenario == "fmcg" and dimension == "category":
                # 快消事实表没有 category_id：经 dim_sku 折算一次（与 _slice_clause 同一口径）
                if "sku" not in joined:
                    joins.append(
                        "LEFT JOIN dw.dim_sku AS dim_sku ON fact.sku_id = dim_sku.sku_id"
                    )
                    joined.add("sku")
                expression = "dim_sku.category_id"
            else:
                raise DecompositionError(
                    f"场景 {scenario} 的事实表不支持按 {dimension} 分组下钻"
                )
            if dimension not in joined:
                joins.append(f"LEFT JOIN {table} AS {alias} ON {expression} = {alias}.{key}")
                joined.add(dimension)
            code = f"COALESCE({alias}.code, 'unknown')"
            selects.append(f"{code} AS {dimension}")
            groups.append(code)
        return selects, joins, groups

    @staticmethod
    def _group_rows(conn, query: str, period: Period) -> dict[tuple[str, ...], float | None]:
        """执行分组查询：``{(组合编码…): 取值}``；取值为 NULL 时保留 None（触发降级路径）。"""

        rows = conn.execute(query, (period.start, period.end)).fetchall()
        values: dict[tuple[str, ...], float | None] = {}
        for row in rows:
            key = tuple(str(item) for item in row[:-1])
            raw = row[-1]
            values[key] = None if raw is None else float(raw)
        return values

    def _root_value(
        self, conn, scenario: str, period: Period, slice_filter: SliceFilter
    ) -> float:
        """只取根指标一次：作为透视表的独立交叉校验值（不跑整棵树）。"""

        tree = self.tree(scenario)
        where = self._slice_clause(conn, scenario, slice_filter)
        row = conn.execute(
            f"SELECT {tree.root.sql} FROM {self.fact_table(scenario)} AS fact"
            f" WHERE fact.day BETWEEN ? AND ?{where}",
            (period.start, period.end),
        ).fetchone()
        if row is None or row[0] is None:
            raise DecompositionError(
                f"根指标 {tree.root.code} 在 {period.start}~{period.end}"
                f"（切片 {slice_filter.as_json()}）无数据"
            )
        return float(row[0])

    def _screen_targets(
        self,
        scenario: str,
        base_values: dict[str, float],
        current_values: dict[str, float],
        auto_targets: list[str] | None,
    ) -> tuple[list[str], list[dict[str, str]]]:
        """按零值预案筛选可下钻的节点：LMDI 要求因子严格为正，非正就"不拆并说明原因"。"""

        accepted: list[str] = []
        skipped: list[dict[str, str]] = []
        tree = self.tree(scenario)
        for code in auto_targets or []:
            node = tree.find(code)
            if node is None:
                skipped.append({"code": code, "reason": "指标字典里没有这个节点"})
                continue
            if not node.decomposable:
                skipped.append({"code": code, "reason": f"{node.structure} 结构不允许分解"})
                continue
            non_positive = [
                child.code
                for child in node.children
                if base_values.get(child.code, 0.0) <= 0 or current_values.get(child.code, 0.0) <= 0
            ]
            if non_positive:
                skipped.append(
                    {
                        "code": code,
                        "reason": (
                            f"子因子取值非正（{', '.join(non_positive)}）："
                            "LMDI 要求因子严格为正，按零值预案改为不分解"
                        ),
                    }
                )
                continue
            accepted.append(code)
        return accepted, skipped

    @staticmethod
    def _node_report(tree: MetricTree, result: TreeResult, code: str) -> NodeReport:
        node_result = result.nodes[code]
        contributions = dict(node_result.contributions)
        # 贡献率：ΔY = 0 时由 attribution 统一返回 None（禁止用 0 蒙混）
        rates = contribution_rates(contributions, node_result.delta)
        _, relative = conservation_residual(list(contributions.values()), node_result.delta)
        node: MetricNode | None = tree.find(code)
        detail = dict(node_result.meta)
        if node is not None:
            detail.update(
                {
                    "level": node.level,
                    "structure": node.structure,
                    "formula": node.formula,
                    "caliber": node.caliber,
                    "sign": node.sign,
                }
            )
        return NodeReport(
            code=node_result.code,
            name=node_result.name,
            method=node_result.method,
            base_total=node_result.base_total,
            current_total=node_result.current_total,
            delta=node_result.delta,
            contributions=contributions,
            rates=rates,
            residual=node_result.residual,
            relative=relative,
            detail=detail,
        )


def _int_list(ids: list[int]) -> str:
    """把已解析成整数的维度 id 列表拼进 SQL（id 来自数据库，不接受外部字符串）。"""

    return ",".join(str(value) for value in ids)


def _format_value(value: float, *, signed: bool = False) -> str:
    """按数量级选格式：比率型（|值| < 1）保留 6 位，金额/数量按整数千分位。"""

    if value == 0:
        return "0"
    if abs(value) < 1:
        return f"{value:+.6f}" if signed else f"{value:.6f}"
    return f"{value:+,.0f}" if signed else f"{value:,.0f}"


__all__ = [
    "DecompositionError",
    "DecompositionReport",
    "DimensionPivot",
    "MetricEngine",
    "NodeReport",
    "Period",
    "SliceFilter",
    "AttributionError",
]
