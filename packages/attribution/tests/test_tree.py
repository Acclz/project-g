"""指标树逐层分解：每层独立守恒 + 节点声明值与子节点推导值一致。"""

from pathlib import Path

import pytest

from attribution.metrics_loader import load_metrics_file
from attribution.tree import DecompositionError, decompose_tree

REPO_ROOT = Path(__file__).resolve().parents[3]
METRICS_PATH = REPO_ROOT / "corpus" / "warehouse" / "metrics.yaml"


def test_ecom_multiplicative_layer_is_conserved():
    tree = load_metrics_file(METRICS_PATH)["ecom"]
    base = {"uv": 1000.0, "cvr": 0.030, "aov": 200.0}
    current = {"uv": 1350.0, "cvr": 0.018, "aov": 195.0}
    result = decompose_tree(tree, base, current)
    node = result.nodes["gmv"]
    assert node.method == "lmdi"
    assert node.delta == pytest.approx(1350 * 0.018 * 195 - 1000 * 0.030 * 200)
    assert abs(sum(node.contributions.values()) - node.delta) < 1e-9
    # 渠道结构变化（uv 升、cvr 降）应体现为方向相反的两项贡献
    assert node.contributions["uv"] > 0
    assert node.contributions["cvr"] < 0


def test_ecom_child_layer_uv_drills_into_ratio_dimensions():
    tree = load_metrics_file(METRICS_PATH)["ecom"]
    base = {"gmv": 6000.0, "uv": 1000.0, "cvr": 0.03, "aov": 200.0, "impressions": 5000.0, "ctr": 0.2}
    current = {
        "gmv": 4738.5,
        "uv": 1350.0,
        "cvr": 0.018,
        "aov": 195.0,
        "impressions": 4500.0,
        "ctr": 0.3,
    }
    # 默认只拆根；显式下钻 uv 才会继续拆一层
    shallow = decompose_tree(tree, base, current)
    assert set(shallow.nodes) == {"gmv"}
    result = decompose_tree(tree, base, current, targets={"uv"})
    assert set(result.nodes) == {"gmv", "uv"}
    # uv = impressions × ctr 是乘法结构，走 LMDI；ctr 本身是比率型，不再往下拆
    assert result.nodes["uv"].method == "lmdi"
    assert "ctr" not in result.nodes
    ctr = tree.find("ctr")
    assert ctr is not None and ctr.decomposable is False


def test_node_declared_value_must_match_children_sum():
    tree = load_metrics_file(METRICS_PATH)["fmcg"]
    # 故意把 root 声明成与子节点不一致的值
    base = {
        "gross_profit": 999.0,
        "revenue": 1000.0,
        "raw_material_cost": 200.0,
        "logistics_cost": 50.0,
        "channel_commission": 100.0,
        "other_cost": 50.0,
    }
    current = dict(base)
    with pytest.raises(DecompositionError):
        decompose_tree(tree, base, current)


def test_fmcg_mixed_tree_conserves_each_layer():
    tree = load_metrics_file(METRICS_PATH)["fmcg"]
    base = {
        "revenue": 1000.0,
        "raw_material_cost": 200.0,
        "logistics_cost": 50.0,
        "channel_commission": 100.0,
        "other_cost": 50.0,
        "units": 100.0,
        "avg_price": 10.0,
        "raw_units": 100.0,
        "unit_cost": 2.0,
        "commission_base": 1000.0,
        "commission_rate": 0.1,
    }
    current = {
        "revenue": 1100.0,
        "raw_material_cost": 260.0,
        "logistics_cost": 70.0,
        "channel_commission": 140.0,
        "other_cost": 50.0,
        "units": 110.0,
        "avg_price": 10.0,
        "raw_units": 110.0,
        "unit_cost": 260.0 / 110.0,
        "commission_base": 1100.0,
        "commission_rate": 140.0 / 1100.0,
    }
    base["gross_profit"] = 1000.0 - 200.0 - 50.0 - 100.0 - 50.0
    current["gross_profit"] = 1100.0 - 260.0 - 70.0 - 140.0 - 50.0
    result = decompose_tree(tree, base, current, targets={"revenue", "raw_material_cost", "channel_commission"})
    root = result.nodes["gross_profit"]
    assert root.method == "diff"
    assert abs(sum(root.contributions.values()) - root.delta) < 1e-9
    # 成本项的符号：成本上升 → 贡献为负
    assert root.contributions["raw_material_cost"] < 0
    assert root.contributions["revenue"] > 0
    revenue_node = result.nodes["revenue"]
    assert revenue_node.method == "lmdi"
    assert abs(sum(revenue_node.contributions.values()) - revenue_node.delta) < 1e-6


def test_missing_child_value_raises():
    tree = load_metrics_file(METRICS_PATH)["ecom"]
    with pytest.raises(DecompositionError):
        decompose_tree(tree, {"uv": 1.0, "cvr": 0.1}, {"uv": 1.0, "cvr": 0.1, "aov": 10.0})
