"""E2 补测：混合树（加法外层 + 乘法内层）随机 120 组，守恒必须 100% 通过。

需求依据：`docs/00-需求说明书.md` §11.2 E2——加法、乘法、混合树三种结构各 ≥100 组随机数据，
相对残差 < 1e-9，通过率 100%。加法与乘法的随机组已在 `test_diff_lmdi.py`（各 800 组）里覆盖，
本文件补上"两套方法混在同一棵树里"的情形：这正是场景 B（毛利 = 收入 − 成本，收入再拆乘法）的形状。
"""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from attribution import (
    DecompositionError,
    assert_conservation,
    conservation_residual,
    decompose_tree,
    load_metrics_text,
)

GROUPS = 120
TOLERANCE = 1e-9


def _payload() -> dict:
    """一棵两层的混合树：加法根 + 两个乘法子节点 + 两个原子成本项。"""

    return {
        "scenarios": [
            {
                "code": "mixed",
                "name": "混合树（毛利 = 收入 − 原材料 − 物流 − 其他）",
                "dimensions": ["channel"],
                "root": {
                    "code": "gross_profit",
                    "name": "毛利额",
                    "level": "composite",
                    "structure": "additive",
                    "method": "diff",
                    "formula": "revenue - material_cost - logistics_cost - other_cost",
                    "children": [
                        {
                            "code": "revenue",
                            "name": "营收",
                            "level": "derived",
                            "structure": "multiplicative",
                            "method": "lmdi",
                            "formula": "revenue_units * revenue_price",
                            "children": [
                                {"code": "revenue_units", "name": "销量", "level": "atomic"},
                                {"code": "revenue_price", "name": "单价", "level": "atomic"},
                            ],
                        },
                        {
                            "code": "material_cost",
                            "name": "原材料成本",
                            "level": "derived",
                            "structure": "multiplicative",
                            "method": "lmdi",
                            "sign": -1,
                            "formula": "material_units * material_unit_cost",
                            "children": [
                                {"code": "material_units", "name": "耗用数量", "level": "atomic"},
                                {"code": "material_unit_cost", "name": "单位成本", "level": "atomic"},
                            ],
                        },
                        {"code": "logistics_cost", "name": "物流成本", "level": "atomic", "sign": -1},
                        {"code": "other_cost", "name": "其他成本", "level": "atomic", "sign": -1},
                    ],
                },
            }
        ]
    }


def _draw(rng: np.random.Generator) -> dict[str, float]:
    """随机两期取值：数量级贴近快消场景（成本项为正，符号由 sign 处理）。

    中间节点（收入 / 原材料成本 / 毛利额）的取值在这里**按定义算出来**再一起传入：
    加法的父节点要求子节点的声明值，乘法的父节点会自己校验"声明值 = 子节点之积"，
    所以两边的口径必须一致——这正是"层与层之间不得互相抵消"的可测形式。
    """

    values = {
        "revenue_units": float(rng.uniform(800, 6000)),
        "revenue_price": float(rng.uniform(12, 90)),
        "material_units": float(rng.uniform(800, 6000)),
        "material_unit_cost": float(rng.uniform(4, 55)),
        "logistics_cost": float(rng.uniform(1000, 12000)),
        "other_cost": float(rng.uniform(200, 1500)),
    }
    values["revenue"] = values["revenue_units"] * values["revenue_price"]
    values["material_cost"] = values["material_units"] * values["material_unit_cost"]
    values["gross_profit"] = (
        values["revenue"]
        - values["material_cost"]
        - values["logistics_cost"]
        - values["other_cost"]
    )
    return values


def test_mixed_tree_conservation_over_120_random_groups() -> None:
    tree = load_metrics_text(yaml.safe_dump(_payload(), allow_unicode=True))["mixed"]
    rng = np.random.default_rng(20260915)
    methods: set[str] = set()
    for index in range(GROUPS):
        base = _draw(rng)
        current = _draw(rng)
        result = decompose_tree(
            tree,
            base,
            current,
            targets=["revenue", "material_cost"],
            tolerance=TOLERANCE,
        )
        # 根节点 + 两个乘法子节点都要被分解到（否则"混合树"没被真正覆盖）
        assert set(result.nodes) == {"gross_profit", "revenue", "material_cost"}, index
        for code, node in result.nodes.items():
            residual, relative = conservation_residual(
                list(node.contributions.values()), node.delta
            )
            assert relative < TOLERANCE, (
                f"第 {index} 组 {code} 相对误差过大：{relative:.3e}（绝对残差 {residual:.3e}）"
            )
            # 再走一次全项目统一的守恒断言：贡献之和 == Δ目标
            assert_conservation(list(node.contributions.values()), node.delta, TOLERANCE)
            methods.add(node.method)
    assert methods == {"diff", "lmdi"}, "混合树必须同时用到差额分析与 LMDI"


def test_mixed_tree_rejects_broken_identity() -> None:
    """声明值与子节点推导值不一致时必须整次失败（不允许局部通过）。"""

    tree = load_metrics_text(yaml.safe_dump(_payload(), allow_unicode=True))["mixed"]
    base = _draw(np.random.default_rng(1))
    current = _draw(np.random.default_rng(2))
    broken = dict(current)
    broken["gross_profit"] = 1234567.0  # 与子节点之和明显不符
    with pytest.raises(DecompositionError):
        decompose_tree(tree, base, broken, tolerance=TOLERANCE)
