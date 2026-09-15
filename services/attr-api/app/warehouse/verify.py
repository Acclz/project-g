"""数仓校验：对账断言、指标 SQL 可执行性、指标树恒等式、真值期间复核。

这是 P2 的"能不能交"判据，四类检查各自回答一个会被追问的问题：

1. **对账断言**：聚合表是不是明细表汇总出来的？（虚增一处，对账立刻不平。）
2. **指标 SQL**：指标字典里写的每一段 SQL 能不能在真实数据上跑出数？（"指标树可配置"的落地证明。）
3. **指标树恒等式**：`GMV = UV × CVR × AOV` 与 `毛利 = 收入 − 成本` 在两期数据上是否严格成立？
   浮点比较统一走 `packages/attribution` 的守恒断言，不在本文件另写一套。
4. **真值复核**：ground_truth 里的期望值能不能用一条独立 SQL 从数仓里复算出来？
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, get_settings
from app.db import connect_app, connect_warehouse_readonly
from app.warehouse import dw_schema
from attribution import ConservationError, MetricTree, assert_conservation, load_metrics_file

METRICS_PATH = REPO_ROOT / "corpus" / "warehouse" / "metrics.yaml"
PERIOD_DAYS = 7


@dataclass
class CheckResult:
    """一条检查的结论：名称、是否通过、可读的细节（失败时写清差在哪）。"""

    name: str
    ok: bool
    detail: str


@dataclass
class VerifyReport:
    """整份校验报告。``ok`` 为真才允许上报"数仓可用"。"""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(CheckResult(name=name, ok=ok, detail=detail))

    def render(self) -> str:
        lines = []
        for check in self.checks:
            lines.append(f"[{'PASS' if check.ok else 'FAIL'}] {check.name} — {check.detail}")
        lines.append(f"结论：{'全部通过' if self.ok else '存在失败项，不允许交付'}")
        return "\n".join(lines)


def load_metric_trees(path: Path | None = None) -> dict[str, MetricTree]:
    """读取指标字典（唯一来源 ``corpus/warehouse/metrics.yaml``）。"""

    return load_metrics_file(Path(path) if path is not None else METRICS_PATH)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _window(conn: sqlite3.Connection, *, last_days: int | None = None) -> tuple[str, str]:
    """给校验用的时间窗：默认全历史，``last_days`` 时取末尾若干天。"""

    end = str(_scalar(conn, "SELECT MAX(day) FROM dw.dim_date"))
    if last_days is None:
        return str(_scalar(conn, "SELECT MIN(day) FROM dw.dim_date")), end
    start = _scalar(
        conn,
        "SELECT MIN(day) FROM (SELECT day FROM dw.dim_date ORDER BY day DESC LIMIT ?)",
        (last_days,),
    )
    return str(start), str(end)


def warehouse_digest(conn: sqlite3.Connection) -> str:
    """整库指纹：按天汇总的稳定摘要，用于"固定种子重跑是否完全一致"的判定。

    只取计数与金额的按天合计，既能覆盖全部事实数值，又不受写入顺序影响。
    """

    digest = hashlib.sha256()
    for statement in (
        "SELECT day, SUM(impressions), SUM(clicks), SUM(orders_paid), SUM(units),"
        " SUM(gmv_cents), SUM(refund_cents) FROM dw.fact_ecom_daily GROUP BY day ORDER BY day",
        "SELECT day, COUNT(*), SUM(amount_cents), SUM(units) FROM dw.fact_order"
        " GROUP BY day ORDER BY day",
        "SELECT day, SUM(units), SUM(revenue_cents), SUM(raw_material_cents),"
        " SUM(logistics_cents), SUM(channel_commission_cents), SUM(other_cost_cents)"
        " FROM dw.fact_fmcg_daily GROUP BY day ORDER BY day",
    ):
        for row in conn.execute(statement):
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


def _check_tables(report: VerifyReport, conn: sqlite3.Connection) -> None:
    expected = dw_schema.table_names()
    found = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [name for name in expected if name not in found]
    counts = {
        name: _scalar(conn, f"SELECT COUNT(*) FROM dw.{name}")
        for name in expected
    }
    empty = [name for name, count in counts.items() if not count]
    detail = "行数 " + ", ".join(f"{name}={counts[name]}" for name in expected)
    report.add(
        "表结构完整",
        not missing and not empty,
        detail if not missing else f"缺表 {missing}；{detail}",
    )


def _check_reconciliation(report: VerifyReport, conn: sqlite3.Connection) -> None:
    """聚合表 vs 明细表：按天逐项比对，任何一天不平即失败。"""

    daily = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in conn.execute(
            "SELECT day, SUM(orders_paid), SUM(units), SUM(gmv_cents), SUM(refund_cents)"
            " FROM dw.fact_ecom_daily GROUP BY day"
        )
    }
    detail_rows = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in conn.execute(
            "SELECT day,"
            " SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status = 'paid' THEN units ELSE 0 END),"
            " SUM(CASE WHEN status = 'paid' THEN amount_cents ELSE 0 END),"
            " SUM(CASE WHEN status = 'refunded' THEN amount_cents ELSE 0 END)"
            " FROM dw.fact_order GROUP BY day"
        )
    }
    mismatches = [
        (day, daily[day], detail_rows.get(day))
        for day in sorted(daily)
        if daily[day] != detail_rows.get(day)
    ]
    report.add(
        "聚合表与明细表对账",
        not mismatches,
        (
            f"{len(daily)} 天逐日比对（支付单数 / 销量 / GMV / 退款）全部一致"
            if not mismatches
            else f"{len(mismatches)} 天不一致，示例：{mismatches[0]}"
        ),
    )


def _check_metric_sql(report: VerifyReport, conn: sqlite3.Connection) -> None:
    """指标字典里每段 SQL 都要能在数仓上跑出非空结果。"""

    trees = load_metric_trees()
    start, end = _window(conn, last_days=PERIOD_DAYS)
    failures: list[str] = []
    checked = 0
    for scenario, tree in trees.items():
        fact_tables = tree.raw.get("fact_tables") or []
        if not fact_tables:
            failures.append(f"{scenario}: 未声明 fact_tables")
            continue
        for node in tree.nodes():
            if not node.sql:
                continue
            query = (
                f"SELECT {node.sql} FROM {fact_tables[0]} AS fact"
                " WHERE fact.day BETWEEN ? AND ?"
            )
            try:
                value = _scalar(conn, query, (start, end))
            except sqlite3.Error as error:  # 语法或表名错误都必须暴露
                failures.append(f"{scenario}/{node.code}: {error}")
                continue
            checked += 1
            if value is None:
                failures.append(f"{scenario}/{node.code}: 返回 NULL")
    report.add(
        "指标字典 SQL 可执行",
        not failures,
        (
            f"{len(trees)} 个场景共 {checked} 个指标节点在 {start}~{end} 上全部出数"
            if not failures
            else f"{len(failures)} 处失败：" + "；".join(failures[:3])
        ),
    )


def _metric_values(
    conn: sqlite3.Connection, tree: MetricTree, start: str, end: str
) -> dict[str, float]:
    fact_tables = tree.raw["fact_tables"]
    values: dict[str, float] = {}
    for node in tree.nodes():
        if not node.sql:
            continue
        values[node.code] = float(
            _scalar(
                conn,
                f"SELECT {node.sql} FROM {fact_tables[0]} AS fact"
                " WHERE fact.day BETWEEN ? AND ?",
                (start, end),
            )
        )
    return values


def _check_identities(report: VerifyReport, conn: sqlite3.Connection) -> None:
    """指标树的每条恒等式都要在整段历史上严格成立（残差 < 1e-9）。"""

    trees = load_metric_trees()
    start, end = _window(conn)
    failures: list[str] = []
    checked = 0
    for scenario, tree in trees.items():
        values = _metric_values(conn, tree, start, end)
        for label, contributions, delta in _identity_cases(scenario, values):
            checked += 1
            try:
                assert_conservation(contributions, delta)
            except ConservationError as error:
                failures.append(f"{scenario}/{label}: {error}")
    report.add(
        "指标树恒等式成立",
        not failures,
        (
            f"{checked} 条恒等式在 {start}~{end} 上相对残差 < 1e-9"
            if not failures
            else f"{len(failures)} 条不成立：" + "；".join(failures[:3])
        ),
    )


def _identity_cases(
    scenario: str, values: dict[str, float]
) -> list[tuple[str, list[float], float]]:
    """把指标树的恒等式翻译成 (说明, 贡献项, 目标值)。"""

    if scenario == "ecom":
        return [
            ("GMV = UV × CVR × AOV", [values["uv"] * values["cvr"] * values["aov"]], values["gmv"]),
            ("UV = 曝光 × CTR", [values["impressions"] * values["ctr"]], values["uv"]),
            (
                "CVR = 加购率 × 下单率 × 支付成功率",
                [values["cart_rate"] * values["order_rate"] * values["pay_success_rate"]],
                values["cvr"],
            ),
            (
                "AOV = 件单价 × 连带率",
                [values["unit_price"] * values["attach_rate"]],
                values["aov"],
            ),
        ]
    return [
        (
            "毛利额 = 营收 − 原材料 − 物流 − 佣金 − 其他",
            [
                values["revenue"],
                -values["raw_material_cost"],
                -values["logistics_cost"],
                -values["channel_commission"],
                -values["other_cost"],
            ],
            values["gross_profit"],
        ),
        ("营收 = 销量 × 平均单价", [values["units"] * values["avg_price"]], values["revenue"]),
        (
            "原材料成本 = 耗用数量 × 单位原料成本",
            [values["raw_units"] * values["unit_cost"]],
            values["raw_material_cost"],
        ),
        (
            "渠道佣金 = 计佣基数 × 佣金率",
            [values["commission_base"] * values["commission_rate"]],
            values["channel_commission"],
        ),
    ]


def _check_ground_truth(
    report: VerifyReport, dw: sqlite3.Connection, app: sqlite3.Connection
) -> None:
    """真值复核：期望值必须能被独立 SQL 从数仓复算出来，因子取值也必须对得上。"""

    rows = app.execute(
        "SELECT id, scenario, factor, leaf_factor, factor_injected_value,"
        " observed_slice_metric_cents, verify_sql, metric_observed_cents"
        " FROM ground_truth ORDER BY id"
    ).fetchall()
    if not rows:
        report.add("真值清单复核", False, "ground_truth 为空，说明没有预埋真因")
        return
    metric_mismatch: list[str] = []
    factor_mismatch: list[str] = []
    for row in rows:
        recomputed = _scalar(dw, row["verify_sql"])
        if recomputed is None or int(recomputed) != int(row["observed_slice_metric_cents"]):
            metric_mismatch.append(f"#{row['id']} {row['verify_sql']} → {recomputed}")
            continue
        if int(row["metric_observed_cents"]) != int(row["observed_slice_metric_cents"]):
            metric_mismatch.append(f"#{row['id']} 记录内部不一致")
        factor_sql = _factor_sql(row)
        observed = _scalar(dw, factor_sql)
        if observed is None or abs(float(observed) - float(row["factor_injected_value"])) > 1e-6:
            factor_mismatch.append(
                f"#{row['id']} {row['factor']} 记录 {row['factor_injected_value']:.6f}"
                f" vs 复算 {observed}"
            )
    report.add(
        "真值期间可复算",
        not metric_mismatch,
        (
            f"{len(rows)} 条真值全部与独立 SQL 复算一致"
            if not metric_mismatch
            else f"{len(metric_mismatch)} 条不一致：{metric_mismatch[:2]}"
        ),
    )
    report.add(
        "真值因子取值可复算",
        not factor_mismatch,
        (
            f"{len(rows)} 条真值的注入后因子取值可由数仓复算"
            if not factor_mismatch
            else f"{len(factor_mismatch)} 条不一致：{factor_mismatch[:2]}"
        ),
    )


def _factor_sql(row: sqlite3.Row) -> str:
    """按真值记录还原"注入后因子取值"的独立 SQL。"""

    base = row["verify_sql"]
    # 真值 SQL 里已经带好了切片条件，这里只把聚合表达式换成"因子口径"
    if row["scenario"] == "ecom":
        if row["leaf_factor"] == "impressions":
            metric = "SUM(visitors)"
        else:
            metric = "1.0 * SUM(orders_paid) / NULLIF(SUM(visitors), 0)"
    else:
        metric = (
            "1.0 * SUM(raw_material_cents) / NULLIF(SUM(units), 0)"
            if row["leaf_factor"] == "unit_cost"
            else "1.0 * SUM(logistics_cents) / NULLIF(SUM(units), 0)"
        )
    tail = base.split(" WHERE ", 1)[1]
    table = base.split(" FROM dw.", 1)[1].split(" WHERE ", 1)[0]
    return f"SELECT {metric} FROM dw.{table} WHERE {tail}"


def verify_warehouse(
    dw_path: Path | None = None,
    app_path: Path | None = None,
    *,
    digest: bool = True,
) -> tuple[VerifyReport, str | None]:
    """跑完整套校验，返回（报告, 数仓指纹）。"""

    settings = get_settings()
    dw_target = Path(dw_path) if dw_path is not None else settings.warehouse_db
    app_target = Path(app_path) if app_path is not None else settings.app_db
    report = VerifyReport()
    if not dw_target.exists():
        report.add("数仓文件存在", False, f"{dw_target} 不存在，请先跑 generate_warehouse.py")
        return report, None
    dw = connect_warehouse_readonly(dw_target)
    app = connect_app(app_target)
    try:
        _check_tables(report, dw)
        _check_reconciliation(report, dw)
        _check_metric_sql(report, dw)
        _check_identities(report, dw)
        _check_ground_truth(report, dw, app)
        fingerprint = warehouse_digest(dw) if digest else None
    finally:
        dw.close()
        app.close()
    return report, fingerprint
