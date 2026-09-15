"""真值的独立核算（严禁用被测系统的分解结果反推）。

需求说明书 §5.12 要求"期望值由独立路径产生"：生成器自己知道每个切片注入了多少，
因此真值在这里由**解析式**算出，再从数仓里用独立 SQL 复核一遍（见 ``verify.py``）。
本模块用的是本文件自带的 LMDI 求解，不调用 ``packages/attribution``：
两条独立实现互相校验（测试里断言两者在 1e-9 内一致），既证明公式对，也避免"用自己的答案验自己"。

真值口径：

* **乘法结构（GMV）**：注入只改动单一因子 x（曝光 → UV，或支付成功率 → CVR），其余因子按观测值保持，
  因此该因子贡献 = ΔY = Y₁ − Y₀，其中 Y₀ = Y₁ × (x₀ / x₁)。
* **加法结构（毛利额）**：注入只改动单一成本项 x，贡献 = −(x₁ − x₀)（成本项 sign = -1）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.warehouse.calibration import REPO_ROOT, CalibrationError

DEFAULT_INJECTION_PATH = REPO_ROOT / "corpus" / "warehouse" / "truth_injections.yaml"


@dataclass(frozen=True)
class InjectionSpec:
    """一条预埋真因：什么时候、在哪个切片的哪个因子上、动了多少。"""

    id: str
    scenario: str
    factor: str
    leaf_factor: str
    multiplier: float
    start_day: date
    end_day: date
    filters: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""

    @property
    def injection_pct(self) -> float:
        """注入幅度（相对变化，如 -0.22 表示下滑 22%）。"""

        return self.multiplier - 1.0

    def filter_json(self) -> str:
        """维度过滤条件写进真值表，便于报告里原样引用。"""

        return json.dumps(self.filters, ensure_ascii=False, sort_keys=True)


def load_injections(path: Path | None = None) -> list[InjectionSpec]:
    """读取真值注入清单（``corpus/warehouse/truth_injections.yaml``）。"""

    target = Path(path) if path is not None else DEFAULT_INJECTION_PATH
    if not target.exists():
        raise CalibrationError(f"真值注入清单不存在：{target}")
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    specs: list[InjectionSpec] = []
    for item in payload.get("injections", []):
        window = item.get("window") or []
        if len(window) != 2:
            raise CalibrationError(f"注入 {item.get('id')} 的 window 必须是 [起, 止] 两天")
        filters = {
            key: ([value] if isinstance(value, str) else [str(entry) for entry in value])
            for key, value in (item.get("filter") or {}).items()
        }
        specs.append(
            InjectionSpec(
                id=str(item["id"]),
                scenario=str(item["scenario"]),
                factor=str(item["factor"]),
                leaf_factor=str(item["leaf_factor"]),
                multiplier=float(item["multiplier"]),
                start_day=date.fromisoformat(str(window[0])),
                end_day=date.fromisoformat(str(window[1])),
                filters=filters,
                note=str(item.get("note") or ""),
            )
        )
    if not specs:
        raise CalibrationError(f"真值注入清单为空：{target}")
    return specs


def lmdi_factor_contribution(y0: float, y1: float, x0: float, x1: float) -> float:
    """LMDI 单因子贡献：``L(Y₁,Y₀) · ln(x₁/x₀)``，``L(a,b)=(a−b)/(ln a−ln b)``。

    当只有该因子变化时，结果应等于 ΔY（守恒）；调用方必须断言这一点。
    需要所有取值严格大于 0——真值核算里若出现 0，说明切片选得太小，应当换切片而不是硬算。
    """

    if min(y0, y1, x0, x1) <= 0:
        raise CalibrationError(
            f"LMDI 真值核算要求四个量严格为正，实际 y0={y0} y1={y1} x0={x0} x1={x1}"
        )
    if math.isclose(y0, y1, rel_tol=1e-15):
        return 0.0
    log_mean = (y1 - y0) / (math.log(y1) - math.log(y0))
    return log_mean * math.log(x1 / x0)


def additive_factor_contribution(x0: float, x1: float, sign: int) -> float:
    """差额分析单因子贡献：``sign × (x₁ − x₀)``（成本项 sign = -1）。"""

    if sign not in (1, -1):
        raise CalibrationError(f"sign 只能是 1 或 -1，实际 {sign}")
    return sign * (x1 - x0)


def counterfactual_metric(
    observed_metric: float, factor_base: float, factor_injected: float
) -> int:
    """乘法场景的反事实目标值：``Y₀ = Y₁ × x₀ / x₁``（四舍五入到分）。

    这条式子把"注入前应当是多少"写死成解析式，避免用被测系统的分解结果当期望值。
    """

    if factor_injected <= 0:
        raise CalibrationError(f"注入后的因子值必须为正，实际 {factor_injected}")
    return int(round(observed_metric * factor_base / factor_injected))
