"""异动判定与大盘数据（需求说明书 §4.1、§5.4；技术规格 §4.3）。

口径写死在代码里，避免"每次解释一遍"：

* **对比方式**：环比——现期与紧挨着的等长上一期比；
* **基线带**：过去 4 周**同时段**（同星期几）的中位数，上下界 = 中位数 ± 2σ
  （σ 取这 4 周同日取值的标准差）；
* **判定阈值（配置可调）**：|变动幅度| > 5% **且** Z 分数 > 2 才算异动；
* **去噪**：每个指标都必须输出"是否落在正常波动区间"，而不是只给一个变动率。

一处如实声明：§5.4 写的是"基线 = 过去 4 周同时段中位数 × 季节性因子"。本项目的合成数仓把季节性
**已经写进了每天的观测值**（`dim_date.season_factor` 参与生成），再乘一次就是重复计数，
所以这里默认不再乘（`apply_season_factor=False`），但把季节因子原样带在输出里便于核对；
开关留在函数参数上，换真实数据时可以打开。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.db import connect_app, connect_warehouse_readonly
from app.services.decomposition import MetricEngine, Period, SliceFilter

#: 基线窗口：过去 4 周（按同星期几对齐）
BASELINE_WEEKS = 4
#: 默认判定阈值（可在配置里改，改动要留痕）
DEFAULT_CHANGE_THRESHOLD = 0.05
DEFAULT_Z_THRESHOLD = 2.0


class AnomalyError(ValueError):
    """异动判定失败（指标不存在、期间无数据、期间格式不对等）。"""


@dataclass(frozen=True)
class AnomalyVerdict:
    """单个指标在给定期间的异动判定结果。"""

    code: str
    name: str
    level: str
    method: str | None
    period: Period
    base_period: Period
    base_value: float
    current_value: float
    delta: float
    change_rate: float | None
    baseline_value: float | None
    baseline_low: float | None
    baseline_high: float | None
    z_score: float | None
    in_normal_band: bool
    is_anomaly: bool
    severity: float
    season_factor: float | None
    covered_days: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "method": self.method,
            "period": self.period.as_dict(),
            "base_period": self.base_period.as_dict(),
            "base_value": self.base_value,
            "current_value": self.current_value,
            "delta": self.delta,
            "change_rate": self.change_rate,
            "baseline_value": self.baseline_value,
            "baseline_low": self.baseline_low,
            "baseline_high": self.baseline_high,
            "z_score": self.z_score,
            "in_normal_band": self.in_normal_band,
            "is_anomaly": self.is_anomaly,
            "severity": self.severity,
            "season_factor": self.season_factor,
            "covered_days": self.covered_days,
            "note": self.note,
        }


def resolve_period(engine: MetricEngine, period: str | None, *, default_days: int = 7) -> Period:
    """解析大盘期间：``YYYY-MM-DD~YYYY-MM-DD``；省略时取数仓末尾 ``default_days`` 天。"""

    if period:
        if "~" not in period:
            raise AnomalyError(f"期间格式应为 起~止（YYYY-MM-DD~YYYY-MM-DD），收到：{period}")
        start, end = (item.strip() for item in period.split("~", 1))
        return Period(start, end, label="指定期间")
    connection = connect_warehouse_readonly(engine.settings.warehouse_db)
    try:
        end = connection.execute("SELECT MAX(day) FROM dw.dim_date").fetchone()[0]
        days = connection.execute(
            "SELECT day FROM dw.dim_date ORDER BY day DESC LIMIT ?", (default_days,)
        ).fetchall()
    finally:
        connection.close()
    if not end or not days:
        raise AnomalyError("数仓维度表为空，请先生成数仓")
    start = min(str(row[0]) for row in days)
    return Period(start, str(end), label=f"数仓末尾 {default_days} 天")


def _season_factors(engine: MetricEngine, days: list[str]) -> dict[str, float]:
    if not days:
        return {}
    connection = connect_warehouse_readonly(engine.settings.warehouse_db)
    try:
        placeholders = ",".join("?" * len(days))
        rows = connection.execute(
            f"SELECT day, season_factor FROM dw.dim_date WHERE day IN ({placeholders})", days
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): float(row[1]) for row in rows}


def baseline_for(
    engine: MetricEngine,
    scenario: str,
    period: Period,
    *,
    expression: str | None = None,
    slice_filter: SliceFilter | None = None,
    weeks: int = BASELINE_WEEKS,
    apply_season_factor: bool = False,
) -> dict[str, Any]:
    """算基线带：现期每一天都取"过去 N 周同一天"的值，中位数作基线、σ 定带宽。"""

    active_slice = slice_filter or SliceFilter()
    end = date.fromisoformat(period.end)
    start = date.fromisoformat(period.start)
    span = (end - start).days + 1
    history_start = start - timedelta(days=7 * weeks)
    history_end = start - timedelta(days=1)
    series = engine.daily_series(
        scenario,
        Period(history_start.isoformat(), history_end.isoformat(), label="基线窗口"),
        expression=expression,
        slice_filter=active_slice,
    ).get((), [])
    lookup = {day: value for day, value in series}
    days = [(start + timedelta(days=offset)).isoformat() for offset in range(span)]
    medians: list[float] = []
    spreads: list[float] = []
    for day in days:
        anchor = date.fromisoformat(day)
        lags = [
            lookup.get((anchor - timedelta(days=7 * step)).isoformat())
            for step in range(1, weeks + 1)
        ]
        values = [value for value in lags if value is not None]
        if not values:
            continue
        medians.append(statistics.median(values))
        if len(values) > 1:
            spreads.append(statistics.pstdev(values))
    if not medians:
        return {
            "value": None,
            "low": None,
            "high": None,
            "sigma": None,
            "covered_days": 0,
            "season_factor": None,
        }
    baseline = statistics.fmean(medians)
    sigma = statistics.fmean(spreads) if spreads else 0.0
    factor = None
    if apply_season_factor:
        factors = _season_factors(engine, days)
        if factors:
            factor = statistics.fmean(factors.values())
            baseline = baseline * factor
    return {
        "value": baseline,
        "low": baseline - 2.0 * sigma,
        "high": baseline + 2.0 * sigma,
        "sigma": sigma,
        "covered_days": len(medians),
        "season_factor": factor,
    }


def judge(
    engine: MetricEngine,
    scenario: str,
    period: Period,
    code: str,
    *,
    slice_filter: SliceFilter | None = None,
    change_threshold: float | None = None,
    z_threshold: float | None = None,
    apply_season_factor: bool = False,
) -> AnomalyVerdict:
    """按 §5.4 的口径判定单个指标是否异动（幅度与 Z 分数两条都要过）。"""

    active_change_threshold = (
        engine.settings.attr_change_threshold
        if change_threshold is None
        else change_threshold
    )
    active_z_threshold = (
        engine.settings.attr_z_threshold if z_threshold is None else z_threshold
    )
    tree = engine.tree(scenario)
    node = tree.find(code)
    if node is None:
        raise AnomalyError(f"场景 {scenario} 里没有指标 {code}")
    if not node.sql:
        raise AnomalyError(f"指标 {code} 没有 SQL，无法取数")
    active_slice = slice_filter or SliceFilter()
    base_period = period.previous()
    base_values = engine.metric_totals(
        scenario, base_period, slice_filter=active_slice, expression=node.sql
    )
    current_values = engine.metric_totals(
        scenario, period, slice_filter=active_slice, expression=node.sql
    )
    base_value = float(base_values.get(()) or 0.0)
    current_value = float(current_values.get(()) or 0.0)
    delta = current_value - base_value
    change_rate = None if base_value == 0 else delta / base_value
    band = baseline_for(
        engine,
        scenario,
        period,
        expression=node.sql,
        slice_filter=active_slice,
        apply_season_factor=apply_season_factor,
    )
    sigma = band["sigma"]
    z_score = None
    note = ""
    if band["value"] is None:
        note = "历史窗口不足，算不出基线带：这类指标只报变动幅度，不判异动"
    elif not sigma:
        note = "4 周同日取值的波动为 0，Z 分数不定义，只按变动幅度判"
    else:
        z_score = (current_value - float(band["value"])) / sigma
    in_normal_band = (
        True
        if band["low"] is None or band["high"] is None
        else bool(band["low"] <= current_value <= band["high"])
    )
    amplitude_ok = change_rate is not None and abs(change_rate) > active_change_threshold
    z_ok = z_score is not None and abs(z_score) > active_z_threshold
    # 波动为 0（合成数据里同一天取值恒定）时 Z 分数没有定义：这时只按幅度判，并写进 note
    is_anomaly = amplitude_ok and (z_ok or (z_score is None and not sigma))
    return AnomalyVerdict(
        code=node.code,
        name=node.name,
        level=node.level,
        method=node.method,
        period=period,
        base_period=base_period,
        base_value=base_value,
        current_value=current_value,
        delta=delta,
        change_rate=change_rate,
        baseline_value=band["value"],
        baseline_low=band["low"],
        baseline_high=band["high"],
        z_score=z_score,
        in_normal_band=in_normal_band,
        is_anomaly=is_anomaly,
        severity=abs(z_score) if z_score is not None else abs(change_rate or 0.0) * 10.0,
        season_factor=band["season_factor"],
        covered_days=band["covered_days"],
        note=note,
    )


def dashboard_anomalies(
    engine: MetricEngine,
    scenario: str,
    *,
    period: Period | None = None,
    slice_filter: SliceFilter | None = None,
    change_threshold: float | None = None,
    z_threshold: float | None = None,
    max_level: int = 3,
) -> dict[str, Any]:
    """异动清单（按严重度排序）：根指标 + 前几层因子，附"是否已发起归因"。"""

    active_period = period or resolve_period(engine, None)
    active_change_threshold = (
        engine.settings.attr_change_threshold
        if change_threshold is None
        else change_threshold
    )
    active_z_threshold = (
        engine.settings.attr_z_threshold if z_threshold is None else z_threshold
    )
    tree = engine.tree(scenario)
    depth = {"composite": 1, "derived": 2, "atomic": 3}
    items: list[dict[str, Any]] = []
    for node in tree.nodes():
        if depth.get(node.level, 3) > max_level or not node.sql:
            continue
        verdict = judge(
            engine,
            scenario,
            active_period,
            node.code,
            slice_filter=slice_filter,
            change_threshold=change_threshold,
            z_threshold=z_threshold,
        )
        payload = verdict.as_dict()
        payload["attribution"] = attribution_status(engine, scenario, node.code, active_period)
        items.append(payload)
    items.sort(key=lambda item: (-abs(item["severity"]), item["code"]))
    return {
        "scenario": scenario,
        "scenario_name": tree.scenario_name,
        "period": active_period.as_dict(),
        "thresholds": {
            "change_rate": active_change_threshold,
            "z_score": active_z_threshold,
        },
        "items": items,
        "anomalies": [item for item in items if item["is_anomaly"]],
    }


def attribution_status(
    engine: MetricEngine, scenario: str, code: str, period: Period
) -> dict[str, Any]:
    """是否已对同一指标 + 同一期间发起过归因（前端据此决定按钮是"发起"还是"继续"）。"""

    connection = connect_app(engine.settings.app_db)
    try:
        rows = connection.execute(
            "SELECT s.id, s.status FROM sessions AS s"
            " JOIN metric_definitions AS m ON m.id = s.metric_id"
            " WHERE s.domain = ? AND m.code = ? AND s.current_period = ?"
            " ORDER BY s.id DESC",
            (scenario, f"{scenario}.{code}", period.start),
        ).fetchall()
    except Exception:  # noqa: BLE001 - 业务库还没建表时按"未发起"处理
        return {"started": False, "session_ids": [], "latest_status": None}
    finally:
        connection.close()
    return {
        "started": bool(rows),
        "session_ids": [int(row["id"]) for row in rows],
        "latest_status": rows[0]["status"] if rows else None,
    }


def dashboard_series(
    engine: MetricEngine,
    scenario: str,
    code: str,
    *,
    days: int = 60,
    slice_filter: SliceFilter | None = None,
) -> dict[str, Any]:
    """指标日序列 + 基线带（大盘卡片与趋势图用同一份数据）。"""

    tree = engine.tree(scenario)
    node = tree.find(code)
    if node is None or not node.sql:
        raise AnomalyError(f"场景 {scenario} 里没有可出数的指标 {code}")
    period = resolve_period(engine, None, default_days=days)
    active_slice = slice_filter or SliceFilter()
    series = engine.daily_series(
        scenario, period, expression=node.sql, slice_filter=active_slice
    ).get((), [])
    band = baseline_for(engine, scenario, period, expression=node.sql, slice_filter=active_slice)
    return {
        "scenario": scenario,
        "code": node.code,
        "name": node.name,
        "unit": node.unit,
        "period": period.as_dict(),
        "points": [{"day": day, "value": value} for day, value in series],
        "baseline": band,
    }


__all__ = [
    "BASELINE_WEEKS",
    "DEFAULT_CHANGE_THRESHOLD",
    "DEFAULT_Z_THRESHOLD",
    "AnomalyError",
    "AnomalyVerdict",
    "attribution_status",
    "baseline_for",
    "dashboard_anomalies",
    "dashboard_series",
    "judge",
    "resolve_period",
]
