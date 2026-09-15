"""FastAPI 应用入口。

路由挂载在 ``/api`` 前缀下（技术规格 §4）。P2 只交付骨架 + 健康探针，
业务路由按 ``docs/00-需求说明书.md`` §14 的分期，在 P3 之后逐个落地。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    dashboard,
    evaluation,
    events_api,
    health,
    metrics,
    reports_api,
    sandbox_api,
    sessions_api,
)
from app.config import get_settings


def create_app() -> FastAPI:
    """组装 FastAPI 应用：配置在导入时即校验，配置有问题则启动失败。"""

    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, debug=settings.debug)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router, prefix="/api")
    app.include_router(metrics.router, prefix="/api")
    app.include_router(sandbox_api.router, prefix="/api")
    app.include_router(evaluation.router, prefix="/api")
    app.include_router(events_api.router, prefix="/api")
    # 报告路由要先于会话路由注册：会话的 `/{session_id}/{action}`（任务控制）是通配路由，
    # 否则 `POST /api/sessions/{id}/reports` 会被当成"控制动作 reports"。
    app.include_router(reports_api.router, prefix="/api")
    app.include_router(sessions_api.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    return app


app = create_app()
