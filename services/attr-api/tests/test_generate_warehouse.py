"""生成器测试：规模、对账、可复现、真值解析式。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from conftest import SmallWarehouse

from app.db import connect_app, connect_warehouse_readonly
from app.warehouse.generator import WarehouseGenerator
from app.warehouse.groundtruth import lmdi_factor_contribution
from app.warehouse.verify import warehouse_digest


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    return conn.execute(sql).fetchone()[0]


def test_row_shape_matches_design(ecom_injection_warehouse: SmallWarehouse) -> None:
    """维度表按定义展开，事实表按"活跃组合"稀疏，快消表是全组合。"""

    dw = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_date") == 8
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_channel") == 8
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_category") == 12
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_region") == 7
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_segment") == 5
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.dim_sku") == 40
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.fact_fmcg_daily") == 8 * 8 * 40 * 7
        ecom_rows = int(_scalar(dw, "SELECT COUNT(*) FROM dw.fact_ecom_daily"))
        assert 0 < ecom_rows < 8 * 8 * 12 * 7 * 5, "电商日表应按活跃组合稀疏"
        assert int(_scalar(dw, "SELECT COUNT(*) FROM dw.fact_order")) > 0
    finally:
        dw.close()


def test_identity_columns_hold_by_construction(ecom_injection_warehouse: SmallWarehouse) -> None:
    """UV ≡ 曝光 × CTR 且漏斗单调：数据本身就满足这些定义式。"""

    dw = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        assert _scalar(dw, "SELECT COUNT(*) FROM dw.fact_ecom_daily WHERE visitors <> clicks") == 0
        assert (
            _scalar(
                dw,
                "SELECT COUNT(*) FROM dw.fact_ecom_daily"
                " WHERE add_to_cart > visitors OR orders_created > add_to_cart"
                " OR orders_paid > orders_created OR units < orders_paid",
            )
            == 0
        )
        # 营收 = 销量 × 标准售价（按构造成立，所以这里必须是 0 行不满足）
        assert (
            _scalar(
                dw,
                "SELECT COUNT(*) FROM dw.fact_fmcg_daily WHERE revenue_cents <> units *"
                " (SELECT standard_price_cents FROM dw.dim_sku s"
                " WHERE s.sku_id = fact_fmcg_daily.sku_id)",
            )
            == 0
        )
    finally:
        dw.close()


def test_daily_table_reconciles_with_order_detail(ecom_injection_warehouse: SmallWarehouse) -> None:
    """聚合表 == 明细表：按天比对支付单数、销量、GMV、退款额。"""

    dw = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        mismatch = _scalar(
            dw,
            """
            WITH daily AS (
                SELECT day, SUM(orders_paid) AS orders_paid, SUM(units) AS units,
                       SUM(gmv_cents) AS gmv, SUM(refund_cents) AS refund
                FROM dw.fact_ecom_daily GROUP BY day
            ), detail AS (
                SELECT day,
                       SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS orders_paid,
                       SUM(CASE WHEN status = 'paid' THEN units ELSE 0 END) AS units,
                       SUM(CASE WHEN status = 'paid' THEN amount_cents ELSE 0 END) AS gmv,
                       SUM(CASE WHEN status = 'refunded' THEN amount_cents ELSE 0 END) AS refund
                FROM dw.fact_order GROUP BY day
            )
            SELECT COUNT(*) FROM daily JOIN detail USING (day)
            WHERE daily.orders_paid <> detail.orders_paid OR daily.units <> detail.units
               OR daily.gmv <> detail.gmv OR daily.refund <> detail.refund
            """,
        )
        assert mismatch == 0
    finally:
        dw.close()


def test_same_seed_reproduces_identical_warehouse(tmp_path: Path) -> None:
    """固定种子重跑必须完全一致（需求说明书 §5.12 可复现性）。"""

    first = WarehouseGenerator(seed=4242, days=5, start_day=date(2026, 6, 1), profile="tiny")
    second = WarehouseGenerator(seed=4242, days=5, start_day=date(2026, 6, 1), profile="tiny")
    first.generate(tmp_path / "a" / "dw.db", tmp_path / "a" / "app.db")
    second.generate(tmp_path / "b" / "dw.db", tmp_path / "b" / "app.db")
    digests = []
    for name in ("a", "b"):
        conn = connect_warehouse_readonly(tmp_path / name / "dw.db")
        try:
            digests.append(warehouse_digest(conn))
        finally:
            conn.close()
    assert digests[0] == digests[1]


def test_different_seed_changes_data(tmp_path: Path) -> None:
    digests = []
    for seed, name in ((11, "c"), (12, "d")):
        generator = WarehouseGenerator(seed=seed, days=5, start_day=date(2026, 6, 1), profile="tiny")
        generator.generate(tmp_path / name / "dw.db", tmp_path / name / "app.db")
        conn = connect_warehouse_readonly(tmp_path / name / "dw.db")
        try:
            digests.append(warehouse_digest(conn))
        finally:
            conn.close()
    assert digests[0] != digests[1]


def test_ground_truth_is_analytic_and_recomputable(ecom_injection_warehouse: SmallWarehouse) -> None:
    """真值三件事：注入幅度对得上、贡献额等于解析式、能被独立 SQL 复算。"""

    app = connect_app(ecom_injection_warehouse.app_path)
    dw = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        rows = app.execute("SELECT * FROM ground_truth").fetchall()
        assert len(rows) == 1, "8 天小样本只应落一条电商注入真值"
        row = rows[0]
        assert row["scenario"] == "ecom"
        assert row["factor"] == "cvr"
        assert row["injection_pct"] == pytest.approx(-0.22)
        observed = int(_scalar(dw, row["verify_sql"]))
        assert observed == row["observed_slice_metric_cents"]
        assert row["injected_contribution_cents"] == (
            row["metric_observed_cents"] - row["metric_counterfactual_cents"]
        )
        # 独立 LMDI 复核：单因子变动时贡献额必须等于 ΔY
        contribution = lmdi_factor_contribution(
            float(row["metric_counterfactual_cents"]),
            float(row["metric_observed_cents"]),
            row["factor_base_value"],
            row["factor_injected_value"],
        )
        assert contribution == pytest.approx(row["injected_contribution_cents"], abs=1.0)
        assert row["metric_observed_cents"] < row["metric_counterfactual_cents"], "注入是下滑"
    finally:
        app.close()
        dw.close()


def test_fmcg_cost_injection_hurts_gross_profit(fmcg_injection_warehouse: SmallWarehouse) -> None:
    """快消成本注入：毛利额贡献为负，且等于 −Δ成本。"""

    app = connect_app(fmcg_injection_warehouse.app_path)
    try:
        row = app.execute(
            "SELECT * FROM ground_truth WHERE scenario = 'fmcg' ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["factor"] == "raw_material_cost"
        assert row["injection_pct"] == pytest.approx(0.08)
        assert row["injected_contribution_cents"] < 0
        assert row["factor_injected_value"] > row["factor_base_value"]
    finally:
        app.close()


def test_skipped_injection_outside_window_is_reported(tmp_path: Path) -> None:
    """窗口外的注入必须显式记录为跳过，而不是静默消失。"""

    generator = WarehouseGenerator(seed=7, days=5, start_day=date(2025, 3, 1), profile="tiny")
    stats = generator.generate(tmp_path / "dw.db", tmp_path / "app.db")
    assert stats.ground_truth_rows == 0
    assert len(stats.skipped_injections) == 4
