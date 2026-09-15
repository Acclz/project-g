"""证明只读数仓连接"写不了任何东西"。

技术规格 §6.2 的第一道约束是"数据只读"。PostgreSQL 靠只读角色实现，SQLite 靠打开方式实现：
``file:<dw.db>?mode=ro&immutable=1`` + ``PRAGMA query_only=ON``。本脚本逐条尝试写操作，
要求**全部**被拒绝，任何一条写成功即为缺陷。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.config import get_settings  # noqa: E402
from app.db import connect_warehouse_readonly  # noqa: E402

WRITE_ATTEMPTS: tuple[tuple[str, str], ...] = (
    ("INSERT", "INSERT INTO dim_region (region_id, code, name) VALUES (99, 'x', 'x')"),
    ("UPDATE", "UPDATE dim_region SET name = 'x' WHERE region_id = 1"),
    ("DELETE", "DELETE FROM dim_region WHERE region_id = 1"),
    ("CREATE", "CREATE TABLE probe_table (id INTEGER)"),
    ("DROP", "DROP TABLE dim_region"),
)


def main() -> int:
    settings = get_settings()
    warehouse = settings.warehouse_db
    if not warehouse.exists():
        print(f"[SKIP] {warehouse} 不存在，请先跑 generate_warehouse.py")
        return 0

    conn = connect_warehouse_readonly(warehouse)
    failures: list[str] = []
    try:
        query_only = conn.execute("PRAGMA query_only").fetchone()[0]
        print(f"[{'PASS' if query_only == 1 else 'FAIL'}] PRAGMA query_only = {query_only}")
        if query_only != 1:
            failures.append("query_only 未生效")

        app_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN"
            " ('users', 'audit_logs', 'ground_truth')"
        ).fetchall()
        print(f"[{'PASS' if not app_tables else 'FAIL'}] 业务表在数仓连接里不可见：{app_tables}")
        if app_tables:
            failures.append("数仓连接能看到业务表")

        for label, statement in WRITE_ATTEMPTS:
            try:
                conn.execute(statement)
            except sqlite3.Error as error:
                print(f"[PASS] {label} 被拒绝：{error}")
            else:
                print(f"[FAIL] {label} 竟然执行成功")
                failures.append(f"{label} 未被拦截")
    finally:
        conn.close()

    if failures:
        print("结论：只读约束存在漏洞 → " + "；".join(failures))
        return 1
    print("结论：只读约束全部生效（写操作 5/5 被拒绝）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
