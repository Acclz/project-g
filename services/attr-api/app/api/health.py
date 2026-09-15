"""健康检查路由。

为什么单独存在：``scripts/smoke_test.py``（P7）与本地启动脚本需要一条不依赖登录的存活探针，
它只报告进程状态与数仓文件是否就绪，不返回任何业务数据。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(tags=["infra"])


class HealthResponse(BaseModel):
    """存活探针响应：只含状态、环境与数仓是否就绪。"""

    status: str
    app_env: str
    warehouse_ready: bool
    app_db_ready: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """返回进程存活状态与两个数据库文件的存在性（不读取数据，不泄漏配置）。"""

    active = get_settings()
    return HealthResponse(
        status="ok",
        app_env=active.app_env,
        warehouse_ready=active.warehouse_db.exists(),
        app_db_ready=active.app_db.exists(),
    )
