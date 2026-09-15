"""沙箱策略单测：不启动子进程，直接验证 AST 判定（快，且失败时能立刻定位规则）。"""

from __future__ import annotations

import pytest

from app.sandbox import policy
from app.sandbox.dw_tables import allowed_table_names


def _sql(sql: str) -> policy.Decision:
    return policy.check_sql(sql, allowed_table_names())


def _python(code: str) -> policy.Decision:
    return policy.check_python(code)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM dw.fact_ecom_daily",
        "SELECT channel_id, SUM(gmv_cents) FROM fact_ecom_daily GROUP BY channel_id",
        "WITH t AS (SELECT day FROM dim_date) SELECT COUNT(*) FROM t",
        "SELECT s.sku_id, SUM(f.units) FROM fact_fmcg_daily AS f"
        " JOIN dim_sku AS s ON s.sku_id = f.sku_id GROUP BY s.sku_id",
    ],
)
def test_normal_select_is_allowed(sql: str) -> None:
    assert _sql(sql).ok, _sql(sql).reason


@pytest.mark.parametrize(
    ("sql", "reason_keyword"),
    [
        ("SELECT * FROM users", "白名单"),
        ("SELECT * FROM app.audit_logs", "白名单"),
        ("SELECT 1; DROP TABLE dim_region", "单条"),
        ("SELECT 1;\n-- 注释\nDROP TABLE dim_region", "单条"),
        ("ATTACH DATABASE 'evil.db' AS evil", "SELECT"),
        ("DETACH DATABASE dw", "SELECT"),
        ("DROP TABLE dw.dim_region", "SELECT"),
        ("CREATE TABLE probe (id INTEGER)", "SELECT"),
        ("ALTER TABLE dw.dim_region ADD COLUMN x INTEGER", "SELECT"),
        ("UPDATE dw.fact_ecom_daily SET gmv_cents = 0", "SELECT"),
        ("DELETE FROM dw.fact_order", "SELECT"),
        ("INSERT INTO audit_logs (actor) VALUES ('x')", "SELECT"),
        ("PRAGMA writable_schema = ON", "SELECT"),
        ("SELECT readfile('C:/Windows/win.ini')", "函数"),
        ("SELECT writefile('evil.txt', 'x')", "函数"),
        ("SELECT load_extension('evil.dll')", "函数"),
        ("SELECT * FROM sqlite_master", "白名单"),
        ("", "为空"),
    ],
)
def test_dangerous_sql_is_blocked(sql: str, reason_keyword: str) -> None:
    decision = _sql(sql)
    assert not decision.ok
    assert reason_keyword in decision.reason


def test_sql_longer_than_limit_is_blocked() -> None:
    assert not _sql("SELECT 1 -- " + "x" * policy.MAX_CODE_LENGTH).ok


@pytest.mark.parametrize(
    "code",
    [
        "import numpy as np\nresult = float(np.mean([1, 2, 3]))",
        "import pandas as pd\nresult = pd.DataFrame({'a': [1]}).shape",
        "from statistics import mean\nresult = mean([1, 2, 3])",
        "result = sum(range(10))",
    ],
)
def test_safe_python_is_allowed(code: str) -> None:
    assert _python(code).ok, _python(code).reason


@pytest.mark.parametrize(
    ("code", "reason_keyword"),
    [
        ("import os", "禁止导入"),
        ("import subprocess", "禁止导入"),
        ("import socket", "禁止导入"),
        ("import urllib.request", "禁止导入"),
        ("from os import path", "禁止导入"),
        ("import sqlite3", "禁止导入"),
        ("result = eval('1+1')", "内建"),
        ("exec('x = 1')", "内建"),
        ("result = __import__('os')", "内建"),
        ("result = ().__class__.__bases__[0].__subclasses__()", "属性"),
        ("result = (1).__class__", "属性"),
        ("", "为空"),
    ],
)
def test_dangerous_python_is_blocked(code: str, reason_keyword: str) -> None:
    decision = _python(code)
    assert not decision.ok
    assert reason_keyword in decision.reason


def test_open_passes_static_check_but_is_guarded_at_runtime() -> None:
    """``open`` 不写进语法黑名单：它由运行时守卫限定在沙箱目录内（见 child.py）。

    这是有意的分层——静态判定负责"能不能做"，运行时守卫负责"能碰哪些文件"；
    越界写文件在 ``test_sandbox_runner`` 里被实际验证为拦截。
    """

    assert "open" not in policy.FORBIDDEN_PYTHON_BUILTINS
    assert policy.check_python("with open('x.txt', 'w') as h:\n    h.write('x')").ok


def test_unknown_kind_is_rejected() -> None:
    assert not policy.check("shell", "echo hi", allowed_table_names()).ok
