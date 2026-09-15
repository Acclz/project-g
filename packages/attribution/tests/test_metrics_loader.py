"""指标字典加载与口径校验（需求 §5.1/§5.2：比率型禁止分解）。"""

from pathlib import Path

import pytest

from attribution.metrics_loader import (
    MetricsValidationError,
    load_metrics_file,
    load_metrics_text,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
METRICS_PATH = REPO_ROOT / "corpus" / "warehouse" / "metrics.yaml"


def test_loads_real_metrics_dictionary():
    trees = load_metrics_file(METRICS_PATH)
    assert set(trees) == {"ecom", "fmcg"}
    ecom = trees["ecom"]
    assert ecom.root.code == "gmv"
    assert ecom.root.structure == "multiplicative"
    assert ecom.root.method == "lmdi"
    assert [c.code for c in ecom.root.children] == ["uv", "cvr", "aov"]
    assert ecom.dimensions == ["channel", "category", "region", "segment"]


def test_intervenable_factors_are_loaded_per_scenario():
    """What-If 的可干预白名单来自指标字典（顶层 intervenable_factors 段）。"""

    trees = load_metrics_file(METRICS_PATH)
    assert trees["ecom"].intervenable_factors == [
        "budget_share",
        "price_index",
        "commission_rate",
    ]
    assert trees["fmcg"].intervenable_factors == [
        "price_index",
        "commission_rate",
        "logistics_mode",
    ]


def test_ratio_metric_is_not_decomposable():
    trees = load_metrics_file(METRICS_PATH)
    ctr = trees["ecom"].find("ctr")
    assert ctr is not None
    assert ctr.structure == "unresolvable"
    assert ctr.decomposable is False


def test_fmcg_mixed_tree_methods():
    trees = load_metrics_file(METRICS_PATH)
    fmcg = trees["fmcg"]
    assert fmcg.root.structure == "additive"
    assert fmcg.root.method == "diff"
    revenue = fmcg.find("revenue")
    assert revenue is not None and revenue.structure == "multiplicative"
    assert revenue.method == "lmdi"


def test_rejects_ratio_metric_with_children():
    yaml_text = """
scenarios:
  - code: bad
    name: bad
    dimensions: [channel]
    root:
      code: root
      name: 根
      level: composite
      structure: multiplicative
      method: lmdi
      children:
        - code: ratio
          name: 比率
          level: derived
          structure: unresolvable
          children:
            - {code: x, name: X, level: atomic, sql: "SUM(x)"}
"""
    with pytest.raises(MetricsValidationError):
        load_metrics_text(yaml_text)


def test_rejects_method_structure_mismatch():
    yaml_text = """
scenarios:
  - code: bad
    name: bad
    dimensions: [channel]
    root:
      code: root
      name: 根
      level: composite
      structure: additive
      method: lmdi
      children:
        - {code: x, name: X, level: atomic, structure: additive, method: diff, sql: "SUM(x)"}
"""
    with pytest.raises(MetricsValidationError):
        load_metrics_text(yaml_text)


def test_rejects_undefined_caliber():
    yaml_text = """
calibers: {}
scenarios:
  - code: bad
    name: bad
    dimensions: [channel]
    root:
      code: root
      name: 根
      level: composite
      structure: additive
      method: diff
      caliber: not_defined
      children:
        - {code: x, name: X, level: atomic, structure: additive, method: diff, sql: "SUM(x)"}
"""
    with pytest.raises(MetricsValidationError):
        load_metrics_text(yaml_text)
