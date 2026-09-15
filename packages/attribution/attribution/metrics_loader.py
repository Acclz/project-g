"""指标字典加载与口径校验（对应 docs/01-技术规格.md §4.2 的 /metrics/{id}/validate）。

核心规则（docs/00-需求说明书.md §5.1、§5.2）：
* structure = additive | multiplicative | unresolvable；
* 只有可加或可乘结构允许作为分解对象，**比率型（unresolvable）禁止分解**；
* method 必须与 structure 匹配：additive → diff，multiplicative → lmdi。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ADDITIVE = "additive"
MULTIPLICATIVE = "multiplicative"
UNRESOLVABLE = "unresolvable"
ATOMIC = "atomic"
VALID_STRUCTURES = {ADDITIVE, MULTIPLICATIVE, UNRESOLVABLE, ATOMIC}
VALID_METHODS = {"diff", "lmdi"}
EXPECTED_METHOD = {ADDITIVE: "diff", MULTIPLICATIVE: "lmdi"}


class MetricsValidationError(ValueError):
    """指标字典不合法（结构不可分解、方法不匹配等）。"""


@dataclass
class MetricNode:
    code: str
    name: str
    level: str
    structure: str
    method: str | None = None
    formula: str | None = None
    sql: str | None = None
    unit: str | None = None
    precision: int | None = None
    caliber: str | None = None
    sign: float = 1.0
    children: list[MetricNode] = field(default_factory=list)

    @property
    def decomposable(self) -> bool:
        """是否允许作为分解对象。"""
        return self.structure in (ADDITIVE, MULTIPLICATIVE) and bool(self.children)

    def find(self, code: str) -> MetricNode | None:
        if self.code == code:
            return self
        for child in self.children:
            hit = child.find(code)
            if hit is not None:
                return hit
        return None

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class MetricTree:
    scenario_code: str
    scenario_name: str
    dimensions: list[str]
    root: MetricNode
    calibers: dict[str, dict[str, Any]]
    raw: dict[str, Any]
    #: What-If 的可干预因子白名单（顶层 ``intervenable_factors`` 段按场景取）
    intervenable_factors: list[str] = field(default_factory=list)

    def find(self, code: str) -> MetricNode | None:
        return self.root.find(code)

    def nodes(self) -> list[MetricNode]:
        return list(self.root.walk())


def _build_node(payload: dict[str, Any], path: str) -> MetricNode:
    for required in ("code", "name", "level"):
        if required not in payload:
            raise MetricsValidationError(f"{path}: 缺少必填字段 {required}")
    structure = payload.get("structure")
    if structure is None:
        # 叶子允许省略 structure（默认 atomic）；带子节点的节点必须显式声明
        if payload.get("children"):
            raise MetricsValidationError(f"{path}: 带子节点的指标必须显式声明 structure")
        structure = ATOMIC
    if structure not in VALID_STRUCTURES:
        raise MetricsValidationError(f"{path}: structure 非法：{structure}")
    method = payload.get("method")
    if method is not None and method not in VALID_METHODS:
        raise MetricsValidationError(f"{path}: method 非法：{method}")
    children = [
        _build_node(child, f"{path}/{child.get('code', '?')}")
        for child in payload.get("children", [])
    ]
    node = MetricNode(
        code=payload["code"],
        name=payload["name"],
        level=payload["level"],
        structure=structure,
        method=method,
        formula=payload.get("formula"),
        sql=payload.get("sql"),
        unit=payload.get("unit"),
        precision=payload.get("precision"),
        caliber=payload.get("caliber"),
        sign=float(payload.get("sign", 1.0)),
        children=children,
    )
    if node.sign not in (1.0, -1.0):
        raise MetricsValidationError(f"{path}: sign 只允许 1 或 -1，实际为 {node.sign}")
    _validate_node(node, path)
    return node


def _validate_node(node: MetricNode, path: str) -> None:
    if not node.children:
        # 叶子：structure 只描述它在父节点算式中的角色（可加项 / 可乘项 / 基础量），
        # 它本身不是分解对象，因此不再强制 method 与 structure 对齐。
        return
    if node.structure not in (ADDITIVE, MULTIPLICATIVE):
        raise MetricsValidationError(
            f"{path}: 只有可加（additive）或可乘（multiplicative）结构允许挂子节点，"
            f"实际为 {node.structure}"
        )
    expected = EXPECTED_METHOD[node.structure]
    if node.method != expected:
        raise MetricsValidationError(
            f"{path}: {node.structure} 结构的 method 必须为 {expected}，实际为 {node.method}"
        )


def load_metrics_text(text: str) -> dict[str, MetricTree]:
    """解析指标字典文本，返回 {场景编码: MetricTree}。"""
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict) or "scenarios" not in payload:
        raise MetricsValidationError("指标字典缺少 scenarios 段")
    calibers = payload.get("calibers", {})
    trees: dict[str, MetricTree] = {}
    for scenario in payload["scenarios"]:
        code = scenario.get("code")
        if not code:
            raise MetricsValidationError("scenario 缺少 code")
        if code in trees:
            raise MetricsValidationError(f"场景编码重复：{code}")
        root = _build_node(scenario["root"], f"scenario[{code}]")
        _validate_calibers(root, calibers, f"scenario[{code}]")
        trees[code] = MetricTree(
            scenario_code=code,
            scenario_name=scenario.get("name", code),
            dimensions=list(scenario.get("dimensions", [])),
            root=root,
            calibers=calibers,
            raw=scenario,
            intervenable_factors=[
                str(item)
                for item in payload.get("intervenable_factors", {}).get(code, [])
            ],
        )
    return trees


def _validate_calibers(node: MetricNode, calibers: dict[str, Any], path: str) -> None:
    if node.caliber and node.caliber not in calibers:
        raise MetricsValidationError(f"{path}/{node.code}: 引用了未定义的口径 {node.caliber}")
    for child in node.children:
        _validate_calibers(child, calibers, path)


def load_metrics_file(path: str | Path) -> dict[str, MetricTree]:
    return load_metrics_text(Path(path).read_text(encoding="utf-8"))
