"""服务骨架冒烟：应用能起、健康探针能应答、配置校验在导入时生效。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


def test_health_endpoint_reports_process_state() -> None:
    """/api/health 只报进程与库文件状态，不泄漏配置，也不依赖登录。"""

    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "app_env", "warehouse_ready", "app_db_ready"}
    assert isinstance(body["warehouse_ready"], bool)


def test_non_local_env_requires_real_secret() -> None:
    """非 local 环境仍用占位密钥时必须启动失败（技术规格 §9 的"缺关键项即失败"）。"""

    with pytest.raises(ValueError):
        Settings(app_env="prod", secret_key="change-me-in-real-env")
