"""数据沙箱路由（技术规格 §4.7）：管理员预览单条 SQL / 脚本 + 执行记录查询。

即使调用方是管理员，代码也一样过沙箱——不存在"管理员通道"绕过执行器。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.db import connect_app
from app.sandbox.runner import SandboxRunner

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class PreviewRequest(BaseModel):
    """沙箱预览请求：``kind`` 决定走 SQL 还是 Python 通道。"""

    kind: Literal["sql", "python"] = "sql"
    code: str = Field(min_length=1, description="单条 SELECT 或一段数值计算代码")
    actor: str = "admin"
    session_id: int | None = None


@router.post("/preview")
def preview(payload: PreviewRequest) -> dict[str, Any]:
    """在沙箱里试跑一段代码：结果、耗时、内存峰值与拦截原因一并返回，并写审计。"""

    runner = SandboxRunner()
    if payload.kind == "sql":
        result = runner.run_sql(payload.code, actor=payload.actor, session_id=payload.session_id)
    else:
        result = runner.run_python(
            payload.code, actor=payload.actor, session_id=payload.session_id
        )
    return result.as_dict()


@router.get("/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """沙箱执行记录（**含被拦截的记录**，这是对抗测试的证据来源）。"""

    connection = connect_app(SandboxRunner().settings.app_db)
    try:
        total = connection.execute("SELECT COUNT(*) FROM sandbox_runs").fetchone()[0]
        blocked = connection.execute(
            "SELECT COUNT(*) FROM sandbox_runs WHERE blocked_reason IS NOT NULL"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id, session_id, step_id, language, statement_digest, rows, duration_ms,"
            " exit_code, blocked_reason, actor, created_at FROM sandbox_runs"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return {
        "total": int(total),
        "blocked": int(blocked),
        "items": [dict(row) for row in rows],
    }
