"""归因算法包（纯算法，无 IO、无框架依赖）。

设计约束（见 docs/01-技术规格.md 第 1 节）：
* 禁止 import FastAPI / SQLAlchemy / 网络库；
* 所有分解结果必须通过守恒断言；
* 唯一允许的浮点比较入口是 conservation.assert_conservation。
"""

from .allocation import (
    AllocationError,
    Contribution,
    ContributionTable,
    allocate_by_share,
    exact_contributions,
)
from .conservation import ConservationError, assert_conservation, conservation_residual
from .diff import additive_contributions, contribution_rates
from .inference import (
    ConfidenceBreakdown,
    InferenceError,
    PermutationResult,
    absolute_risk_difference,
    composite_confidence,
    coverage_score,
    direction_consistency,
    effect_score,
    permutation_test,
    significance_score,
    spearman_correlation,
    standardized_difference,
)
from .lmdi import LmdiError, lmdi_contributions
from .metrics_loader import MetricNode, MetricTree, load_metrics_file, load_metrics_text
from .tree import DecompositionError, NodeResult, TreeResult, decompose_tree

__all__ = [
    "AllocationError",
    "ConfidenceBreakdown",
    "ConservationError",
    "Contribution",
    "ContributionTable",
    "DecompositionError",
    "InferenceError",
    "LmdiError",
    "MetricNode",
    "MetricTree",
    "NodeResult",
    "PermutationResult",
    "TreeResult",
    "absolute_risk_difference",
    "additive_contributions",
    "allocate_by_share",
    "assert_conservation",
    "composite_confidence",
    "conservation_residual",
    "contribution_rates",
    "coverage_score",
    "decompose_tree",
    "direction_consistency",
    "effect_score",
    "exact_contributions",
    "lmdi_contributions",
    "load_metrics_file",
    "load_metrics_text",
    "permutation_test",
    "significance_score",
    "spearman_correlation",
    "standardized_difference",
]
