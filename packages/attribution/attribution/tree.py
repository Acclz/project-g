"""指标树逐层分解调度：每层独立守恒，任一层失败即整次失败。

需求依据：docs/00-需求说明书.md §5.2、docs/01-技术规格.md §5.3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .conservation import DEFAULT_TOLERANCE, assert_conservation, conservation_residual
from .diff import additive_contributions
from .lmdi import lmdi_contributions
from .metrics_loader import ADDITIVE, MULTIPLICATIVE, MetricNode, MetricTree, MetricsValidationError


class DecompositionError(ValueError):
    """分解失败（缺值、口径不一致、层内不守恒等）。"""


@dataclass
class NodeResult:
    code: str
    name: str
    method: str
    base_total: float
    current_total: float
    delta: float
    contributions: dict[str, float]
    residual: float
    meta: dict[str, object] = field(default_factory=dict)


@dataclass
class TreeResult:
    root_code: str
    nodes: dict[str, NodeResult]
    order: list[str]

    def contributions(self, code: str) -> dict[str, float]:
        if code not in self.nodes:
            raise DecompositionError(f"未分解节点：{code}")
        return self.nodes[code].contributions


def decompose_tree(
    tree: MetricTree,
    base_values: Mapping[str, float],
    current_values: Mapping[str, float],
    *,
    targets: Iterable[str] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> TreeResult:
    """自顶向下分解整棵树。

    base_values / current_values：以指标编码为键的取值（叶子必须有值；
    中间节点的取值若提供则用于一致性校验，未提供则由子节点推导）。
    加法节点：节点值必须等于子节点之和；乘法节点：节点值必须等于子节点之积。

    targets：需要**额外下钻**的节点编码集合。默认只分解根节点（一级拆解），
    需要下钻时显式声明，避免"想拆一层却被迫提供整棵树的所有叶子值"。
    只有可分解结构（可加 / 可乘）的节点才能出现在 targets 中。
    """
    targets = set(targets or ())
    results: dict[str, NodeResult] = {}
    order: list[str] = []

    def visit(node: MetricNode) -> tuple[float, float]:
        """返回该节点在两期的取值。"""
        if node.structure == ADDITIVE:
            # 加法节点用带符号项：成本项的 sign = -1，因此"成本上升"自动表现为负贡献
            base_children = {
                c.code: c.sign * _require(base_values, c.code, node.code) for c in node.children
            }
            curr_children = {
                c.code: c.sign * _require(current_values, c.code, node.code) for c in node.children
            }
            base_total = _node_value(node.code, base_values, sum(base_children.values()))
            curr_total = _node_value(node.code, current_values, sum(curr_children.values()))
            _assert_identity(node, base_total, sum(base_children.values()), tolerance, "base")
            _assert_identity(node, curr_total, sum(curr_children.values()), tolerance, "current")
            contributions = additive_contributions(base_children, curr_children, tolerance)
            _record(node, base_total, curr_total, contributions, "diff", {}, results, order)

        elif node.structure == MULTIPLICATIVE:
            base_children = {c.code: _require(base_values, c.code, node.code) for c in node.children}
            curr_children = {
                c.code: _require(current_values, c.code, node.code) for c in node.children
            }
            base_total = _node_value(node.code, base_values, _product(base_children.values()))
            curr_total = _node_value(node.code, current_values, _product(curr_children.values()))
            _assert_identity(node, base_total, _product(base_children.values()), tolerance, "base")
            _assert_identity(node, curr_total, _product(curr_children.values()), tolerance, "current")
            contributions, meta = lmdi_contributions(
                base_children, curr_children, base_total, curr_total, tolerance=tolerance
            )
            _record(node, base_total, curr_total, contributions, "lmdi", meta, results, order)

        else:
            raise DecompositionError(
                f"节点 {node.code} 的结构为 {node.structure}，不允许作为分解对象"
            )

        for child in node.children:
            if child.decomposable and child.code in targets:
                visit(child)
        return base_total, curr_total

    visit(tree.root)
    return TreeResult(root_code=tree.root.code, nodes=results, order=order)


def _record(
    node: MetricNode,
    base_total: float,
    curr_total: float,
    contributions: dict[str, float],
    method: str,
    meta: dict[str, object],
    results: dict[str, NodeResult],
    order: list[str],
) -> float:
    delta = curr_total - base_total
    residual = assert_conservation(list(contributions.values()), delta)
    results[node.code] = NodeResult(
        code=node.code,
        name=node.name,
        method=method,
        base_total=base_total,
        current_total=curr_total,
        delta=delta,
        contributions=dict(contributions),
        residual=residual,
        meta={**meta, "decomposable": True},
    )
    order.append(node.code)
    return residual


def _require(values: Mapping[str, float], code: str, parent: str) -> float:
    if code not in values:
        raise DecompositionError(f"节点 {parent} 的子指标 {code} 缺少取值")
    return float(values[code])


def _node_value(code: str, values: Mapping[str, float], derived: float) -> float:
    return float(values[code]) if code in values else derived


def _assert_identity(
    node: MetricNode, declared: float, derived: float, tolerance: float, period: str
) -> None:
    if abs(declared - derived) / max(abs(derived), 1.0) >= tolerance:
        raise DecompositionError(
            f"节点 {node.code}（{period}）的声明值 {declared} 与子节点推导值 {derived} 不一致"
        )


def _product(values) -> float:
    result = 1.0
    for value in values:
        result *= value
    return result


__all__ = ["DecompositionError", "NodeResult", "TreeResult", "decompose_tree", "MetricsValidationError"]
