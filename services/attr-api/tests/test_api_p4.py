"""P4 接口测试：事件日历 CRUD/匹配 + 评测数据集扩展。"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from conftest import SmallWarehouse
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(scope="module")
def client(
    sandbox_settings: Settings, ecom_injection_warehouse: SmallWarehouse
) -> Iterator[TestClient]:
    overrides = {
        "WAREHOUSE_DB_PATH": str(sandbox_settings.warehouse_db_path),
        "APP_DB_PATH": str(sandbox_settings.app_db_path),
        "SANDBOX_TMP_DIR": str(sandbox_settings.sandbox_tmp_dir),
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def test_event_crud_and_soft_delete(client: TestClient) -> None:
    created = client.post(
        "/api/events",
        json={
            "name": "测试事件",
            "type": "内部动作",
            "start_day": "2026-06-05",
            "end_day": "2026-06-06",
            "dimensions": {"channel": ["paid_ads"]},
            "note": "pytest",
        },
    )
    assert created.status_code == 200
    event_id = created.json()["id"]

    listed = client.get("/api/events", params={"type": "内部动作"}).json()["items"]
    assert any(item["id"] == event_id for item in listed)

    updated = client.put(
        f"/api/events/{event_id}",
        json={
            "name": "测试事件（改）",
            "type": "内部动作",
            "start_day": "2026-06-05",
            "end_day": "2026-06-07",
            "dimensions": {"channel": ["paid_ads"]},
            "note": "pytest",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["updated"] is True

    deleted = client.delete(f"/api/events/{event_id}")
    assert deleted.json()["soft"] is True
    remaining = client.get("/api/events").json()["items"]
    assert all(item["id"] != event_id for item in remaining), "软删除后不应出现在列表里"


def test_event_match_returns_two_tiers(client: TestClient) -> None:
    response = client.get(
        "/api/events/match",
        params={"scenario": "ecom", "window_start": "2026-06-05", "window_end": "2026-06-11",
                "channel": "paid_ads"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"matched", "watching"}
    assert all(item["is_exogenous"] for item in body["matched"]), "入链事件必须标为外生变量"
    assert all(item["relevance"] >= 0.5 for item in body["matched"])


def test_event_match_rejects_far_window(client: TestClient) -> None:
    response = client.get(
        "/api/events/match",
        params={"scenario": "ecom", "window_start": "2026-06-05", "window_end": "2026-06-11",
                "channel": "catering"},
    )
    body = response.json()
    assert body["matched"] == [], "没有同窗事件时不得强行关联"


def test_unknown_eval_dataset_is_rejected(client: TestClient) -> None:
    response = client.post("/api/eval/run", json={"dataset": "not_exist", "label": "pytest"})
    assert response.status_code == 422


def test_attribution_eval_dataset_runs_and_persists(client: TestClient) -> None:
    response = client.post(
        "/api/eval/run", json={"dataset": "attribution_eval", "label": "pytest"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dataset"] == "attribution_eval"
    assert body["eval_run_id"] > 0
    assert "top1_rate" in body["metrics"]
    history = client.get("/api/eval/runs").json()
    assert any(item["dataset"] == "attribution_eval" for item in history["items"])
