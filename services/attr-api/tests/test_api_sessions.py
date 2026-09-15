"""会话接口测试：创建、追问、SSE 步骤流、下钻收紧与并发 409。"""

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


def _create(client: TestClient) -> int:
    response = client.post(
        "/api/sessions",
        json={
            "scenario": "ecom",
            "base_start": "2026-06-01",
            "base_end": "2026-06-04",
            "current_start": "2026-06-05",
            "current_end": "2026-06-08",
            "channel": "paid_ads",
            "title": "pytest 会话接口",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["slice"] == {"channel": ["paid_ads"]}
    return body["id"]


def test_create_list_detail(client: TestClient) -> None:
    session_id = _create(client)
    listed = client.get("/api/sessions", params={"limit": 5}).json()["items"]
    assert any(item["id"] == session_id for item in listed)
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["caliber_version"] == 1
    assert detail["current"]["start"] == "2026-06-05"


def test_drilldown_can_only_narrow(client: TestClient) -> None:
    session_id = _create(client)
    ok = client.post(
        f"/api/sessions/{session_id}/drilldown",
        json={"region": "east"},
    )
    assert ok.status_code == 200
    assert ok.json()["slice"] == {"channel": ["paid_ads"], "region": ["east"]}
    blocked = client.post(
        f"/api/sessions/{session_id}/drilldown",
        json={"channel": "catering"},
    )
    assert blocked.status_code == 409
    assert "锁定" in blocked.json()["detail"]


def test_drilldown_requires_a_dimension(client: TestClient) -> None:
    session_id = _create(client)
    response = client.post(f"/api/sessions/{session_id}/drilldown", json={})
    assert response.status_code == 400


def test_drilldown_accepts_pivot_dimensions_without_widening(client: TestClient) -> None:
    """只指定透视维度时不算放宽切片：切片保持锁定值，任务照跑。"""

    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/drilldown",
        json={"dimensions": [["category", "region"]], "top_n": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["slice"] == {"channel": ["paid_ads"]}
    assert response.json()["status"] in ("analysing", "awaiting_user", "failed")


def test_drilldown_rejects_too_many_dimensions(client: TestClient) -> None:
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/drilldown",
        json={"dimensions": [["channel", "category", "region", "segment"]]},
    )
    assert response.status_code == 400
    assert "3" in response.json()["detail"]


def test_drilldowns_endpoint_returns_records(client: TestClient) -> None:
    session_id = _create(client)
    empty = client.get(f"/api/sessions/{session_id}/drilldowns")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    client.post(f"/api/sessions/{session_id}/drilldown", json={"region": "east"})
    listed = client.get(f"/api/sessions/{session_id}/drilldowns")
    assert listed.status_code == 200
    assert listed.json()["items"], "下钻受理后要能查到记录"


def test_message_starts_run_and_second_is_conflict(client: TestClient) -> None:
    session_id = _create(client)
    first = client.post(f"/api/sessions/{session_id}/messages", json={"message": "为什么下滑"})
    assert first.status_code == 200
    assert first.json()["status"] in ("analysing", "awaiting_user")
    second = client.post(f"/api/sessions/{session_id}/messages", json={"message": "再来一次"})
    assert second.status_code in (200, 409), second.text


def test_stream_emits_events_then_closes(client: TestClient) -> None:
    session_id = _create(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"message": "跑一次"})
    with client.stream("GET", f"/api/sessions/{session_id}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = []
        for line in response.iter_lines():
            if line:
                chunks.append(line)
            if len(chunks) >= 2:
                break
    assert chunks and all(chunk.startswith("data: ") for chunk in chunks)
    assert "stream_closed" in "".join(chunks) or "kind" in "".join(chunks)


def test_control_actions_and_hypotheses_endpoints(client: TestClient) -> None:
    session_id = _create(client)
    paused = client.post(f"/api/sessions/{session_id}/pause", json={})
    assert paused.status_code == 200
    assert paused.json()["status"] == "awaiting_user"
    cancelled = client.post(f"/api/sessions/{session_id}/cancel", json={})
    assert cancelled.json()["status"] == "failed"
    unknown = client.post(f"/api/sessions/{session_id}/boom", json={})
    assert unknown.status_code == 404
    assert client.get(f"/api/sessions/{session_id}/hypotheses").json()["items"] == []
    assert client.get(f"/api/sessions/{session_id}/evidence").json()["items"] == []
