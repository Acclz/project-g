"""报告与导出接口测试（技术规格 §4.6）+ 大盘接口（§4.3）。"""

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
        "EXPORT_DIR": str(sandbox_settings.warehouse_db_path.parent / "exports"),
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


def _session(client: TestClient) -> int:
    response = client.post(
        "/api/sessions",
        json={
            "scenario": "ecom",
            "base_start": "2026-06-01",
            "base_end": "2026-06-04",
            "current_start": "2026-06-05",
            "current_end": "2026-06-08",
            "channel": "paid_ads",
            "title": "pytest 报告接口",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_report_create_get_list_annotate(client: TestClient) -> None:
    session_id = _session(client)
    created = client.post(f"/api/sessions/{session_id}/reports", json={"actor": "pytest"})
    assert created.status_code == 200, created.text
    report = created.json()
    report_id = report["meta"]["report_id"]
    assert [segment["index"] for segment in report["segments"]] == list(range(1, 8))
    detail = client.get(f"/api/reports/{report_id}")
    assert detail.status_code == 200
    assert detail.json()["segments"]
    listed = client.get("/api/reports", params={"domain": "ecom"})
    assert listed.status_code == 200
    assert any(item["id"] == report_id for item in listed.json()["items"])
    annotated = client.post(
        f"/api/reports/{report_id}/annotations",
        json={"anchor": "段2", "text": "残差这一行要留痕", "author": "pytest"},
    )
    assert annotated.status_code == 200
    assert client.get(f"/api/reports/{report_id}").json()["annotations"]


def test_export_three_formats_and_download(client: TestClient) -> None:
    session_id = _session(client)
    report_id = client.post(f"/api/sessions/{session_id}/reports", json={}).json()["meta"][
        "report_id"
    ]
    checksums = set()
    for fmt in ("md", "pdf", "xlsx"):
        response = client.post(f"/api/reports/{report_id}/export", json={"format": fmt})
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["format"] == fmt and job["status"] == "completed"
        checksums.add(job["content_checksum"])
        status = client.get(f"/api/exports/{job['id']}")
        assert status.status_code == 200
        assert status.json()["file_sha256"]
        download = client.get(f"/api/exports/{job['id']}/download")
        assert download.status_code == 200
        assert len(download.content) > 0
    assert len(checksums) == 1, "三种格式必须共享同一个内容校验和"


def test_unknown_format_returns_400(client: TestClient) -> None:
    session_id = _session(client)
    report_id = client.post(f"/api/sessions/{session_id}/reports", json={}).json()["meta"][
        "report_id"
    ]
    response = client.post(f"/api/reports/{report_id}/export", json={"format": "docx"})
    assert response.status_code == 400
    assert "docx" in response.json()["detail"]


def test_dashboard_endpoints(client: TestClient) -> None:
    anomalies = client.get(
        "/api/dashboard/anomalies",
        params={"domain": "ecom", "period": "2026-06-05~2026-06-08"},
    )
    assert anomalies.status_code == 200, anomalies.text
    body = anomalies.json()
    assert body["items"] and "thresholds" in body
    assert all("in_normal_band" in item for item in body["items"])
    series = client.get("/api/dashboard/series", params={"domain": "ecom", "metric": "gmv"})
    assert series.status_code == 200
    payload = series.json()
    assert payload["points"] and payload["code"] == "gmv"
    # 小样本数仓只有 8 天：基线窗口（过去 4 周）落在数据之外，band 为 None 是正确行为；
    # 但字段必须齐全，调用方据此显示"没有历史窗口，算不出基线带"。
    assert set(payload["baseline"]) >= {"value", "low", "high", "sigma", "covered_days"}
    missing = client.get("/api/dashboard/series", params={"domain": "ecom", "metric": "nope"})
    assert missing.status_code == 404
