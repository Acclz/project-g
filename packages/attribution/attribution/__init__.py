"""归因算法包（纯算法，无 IO、无框架依赖）。

设计约束（见 docs/01-技术规格.md 第 1 节）：
* 禁止 import FastAPI / SQLAlchemy / 网络库；
* 所有分解结果必须通过守恒断言；
* 唯一允许的浮点比较入口是 conservation.assert_conservation。
"""

from .conservation import ConservationError, assert_conservation, conservation_residual
from .diff import additive_contributions, contribution_rates
from .lmdi import LmdiError, lmdi_contributions
from .metrics_loader import MetricNode, MetricTree, load_metrics_file, load_metrics_text

__all__ = [
    "ConservationError",
    "LmdiError",
    "MetricNode",
    "MetricTree",
    "additive_contributions",
    "assert_conservation",
    "conservation_residual",
    "contribution_rates",
    "lmdi_contributions",
    "load_metrics_file",
    "load_metrics_text",
]
