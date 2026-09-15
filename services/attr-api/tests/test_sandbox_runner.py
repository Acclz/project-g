"""沙箱执行器集成测试：子进程、配额、审计、环境变量白名单。"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.db import connect_app
from app.sandbox.runner import Quotas, SandboxRunner, digest_of


@pytest.fixture(scope="module")
def runner(sandbox_settings: Settings) -> SandboxRunner:
    return SandboxRunner(sandbox_settings)


def _audit_rows(settings: Settings) -> list[sqlite3.Row]:
    connection = connect_app(settings.app_db)
    try:
        return connection.execute("SELECT * FROM sandbox_runs ORDER BY id").fetchall()
    finally:
        connection.close()


def test_allowed_sql_returns_rows(runner: SandboxRunner) -> None:
    result = runner.run_sql(
        "SELECT COUNT(*) AS orders_paid, SUM(gmv_cents) AS gmv FROM dw.fact_ecom_daily"
        " WHERE day BETWEEN '2026-06-01' AND '2026-06-03'"
    )
    assert result.ok, result.error
    assert result.blocked_reason is None
    assert result.rows and len(result.rows) == 1
    assert result.columns == ["orders_paid", "gmv"]
    assert result.rows[0][0] > 0
    assert result.audit_id is not None


def test_blocked_sql_is_audited(sandbox_settings: Settings, runner: SandboxRunner) -> None:
    before = len(_audit_rows(sandbox_settings))
    result = runner.run_sql("SELECT * FROM users")
    assert not result.ok
    assert "白名单" in (result.blocked_reason or "")
    assert result.audit_id is not None
    rows = _audit_rows(sandbox_settings)
    assert len(rows) == before + 1
    last = rows[-1]
    assert last["blocked_reason"] and "白名单" in last["blocked_reason"]
    assert last["exit_code"] == 0  # 策略拦截不启动子进程，统一记 0


def test_audit_stores_digest_not_source(
    runner: SandboxRunner, sandbox_settings: Settings
) -> None:
    sql = "SELECT 1 AS probe"
    runner.run_sql(sql)
    rows = _audit_rows(sandbox_settings)
    assert rows[-1]["statement_digest"] == digest_of(sql)
    assert sql not in (rows[-1]["statement_digest"] or "")


def test_python_numpy_runs(runner: SandboxRunner) -> None:
    result = runner.run_python(
        "import numpy as np\n"
        "sample = np.array([1.0, 2.0, 3.0, 4.0])\n"
        "print('mean', float(sample.mean()))\n"
        "result = {'mean': float(sample.mean()), 'n': int(sample.size)}"
    )
    assert result.ok, result.error
    assert "mean" in result.result
    assert "mean 2.5" in result.stdout


def test_python_import_os_is_blocked(runner: SandboxRunner) -> None:
    result = runner.run_python("import os\nresult = os.getcwd()")
    assert not result.ok
    assert "禁止导入" in (result.blocked_reason or "")


def test_python_file_write_outside_work_dir_is_blocked(runner: SandboxRunner) -> None:
    result = runner.run_python(
        "with open('C:/Windows/Temp/evil.txt', 'w') as handle:\n    handle.write('x')"
    )
    assert not result.ok
    assert "文件访问" in (result.blocked_reason or "")


def test_timeout_kills_child(runner: SandboxRunner) -> None:
    result = runner.run_python("while True:\n    pass", quotas=Quotas(timeout_seconds=2))
    assert not result.ok
    assert "超时" in (result.blocked_reason or "")
    assert result.duration_ms >= 1500


def test_memory_bomb_is_killed(runner: SandboxRunner) -> None:
    result = runner.run_python(
        "blocks = []\n"
        "for _ in range(80):\n"
        "    blocks.append([0] * 1000000)\n"
        "result = len(blocks)",
        quotas=Quotas(memory_mb=256, timeout_seconds=30),
    )
    assert not result.ok
    assert "内存超限" in (result.blocked_reason or "")


def test_row_limit_is_enforced(runner: SandboxRunner) -> None:
    result = runner.run_sql(
        "SELECT * FROM dw.fact_order LIMIT 5000", quotas=Quotas(max_rows=1000)
    )
    assert not result.ok
    assert "结果集超限" in (result.blocked_reason or "")


def test_scan_limit_is_enforced(runner: SandboxRunner) -> None:
    result = runner.run_sql(
        "SELECT COUNT(*) FROM dw.fact_order AS a, dw.fact_order AS b",
        quotas=Quotas(scan_steps=200_000, timeout_seconds=20),
    )
    assert not result.ok
    assert "扫描超限" in (result.blocked_reason or "")


def test_child_env_only_carries_allowlisted_values(runner: SandboxRunner) -> None:
    """§6.1：子进程不继承服务进程的环境变量，只注入 SANDBOX_* 与只读 DSN。"""

    env = runner._child_env()  # noqa: SLF001 - 直接验证安全属性
    assert env["SANDBOX_DW_DSN"].startswith("file:")
    assert "mode=ro" in env["SANDBOX_DW_DSN"] and "immutable=1" in env["SANDBOX_DW_DSN"]
    for secret in ("LLM_API_KEY", "SECRET_KEY", "DATABASE_URL"):
        assert secret not in env


def test_bad_child_response_is_reported(runner: SandboxRunner) -> None:
    """子进程若返回非 JSON，父进程要给出结构化错误而不是抛异常。"""

    from app.sandbox import runner as runner_module

    assert runner_module._parse_response("not json") is None
    assert runner_module._parse_response("[]") is None
    assert runner_module._parse_response('{"ok": true}') == {"ok": True}
