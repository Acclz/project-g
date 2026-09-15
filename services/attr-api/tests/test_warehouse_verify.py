"""数仓校验流程测试：指标 SQL 可执行、恒等式成立、只读约束生效、审计不可改。"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import SmallWarehouse

from app.db import connect_app, connect_warehouse_readonly
from app.warehouse.verify import load_metric_trees, verify_warehouse


def test_metrics_dictionary_is_loadable_and_complete() -> None:
    """指标字典必须能被校验器读懂：两个场景、比率型不参与分解。"""

    trees = load_metric_trees()
    assert set(trees) == {"ecom", "fmcg"}
    ecom = trees["ecom"]
    assert ecom.root.code == "gmv"
    assert ecom.find("ctr") is not None
    assert ecom.find("ctr").decomposable is False, "比率型指标禁止作为分解对象"
    assert trees["fmcg"].root.code == "gross_profit"


def test_verify_report_passes_on_generated_warehouse(ecom_injection_warehouse: SmallWarehouse) -> None:
    report, fingerprint = verify_warehouse(
        ecom_injection_warehouse.dw_path, ecom_injection_warehouse.app_path
    )
    assert report.ok, report.render()
    assert fingerprint and len(fingerprint) == 64
    names = {check.name for check in report.checks}
    assert {
        "表结构完整",
        "聚合表与明细表对账",
        "指标字典 SQL 可执行",
        "指标树恒等式成立",
        "真值期间可复算",
        "真值因子取值可复算",
    } <= names


def test_readonly_connection_rejects_writes(ecom_injection_warehouse: SmallWarehouse) -> None:
    """沙箱数据只读：靠打开方式实现，写操作必须被 SQLite 直接拒绝。"""

    conn = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO dim_region (region_id, code, name) VALUES (99, 'x', 'x')")
        with pytest.raises(sqlite3.Error):
            conn.execute("DROP TABLE dim_region")
    finally:
        conn.close()


def test_readonly_connection_cannot_see_app_tables(ecom_injection_warehouse: SmallWarehouse) -> None:
    """dw.db 与 app.db 分成两个文件，只读打开数仓就连业务表都看不到。"""

    conn = connect_warehouse_readonly(ecom_injection_warehouse.dw_path)
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('users', 'audit_logs')"
        ).fetchall()
        assert found == []
    finally:
        conn.close()


def test_audit_logs_are_immutable(ecom_injection_warehouse: SmallWarehouse) -> None:
    """审计表不可改、不可删：PostgreSQL 靠权限，SQLite 靠触发器（技术规格 §3.2）。"""

    conn = connect_app(ecom_injection_warehouse.app_path)
    try:
        conn.execute(
            "INSERT INTO audit_logs (actor, action, target_type, target_id, detail_json)"
            " VALUES ('tester', 'login', 'user', '1', '{}')"
        )
        conn.commit()
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE audit_logs SET action = 'x' WHERE id = 1")
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM audit_logs WHERE id = 1")
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM audit_logs")
    finally:
        conn.close()
