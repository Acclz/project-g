"""P3 接口层测试：指标树、口径校验、沙箱预览与记录、评测触发。"""

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
    """把应用指向临时数仓：接口测试绝不允许读写仓库里的 ``.data/``。"""

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


def test_list_metrics(client: TestClient) -> None:
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["scenarios"] == ["ecom", "fmcg"]
    assert body["items"]["ecom"]["root"] == "gmv"
    assert body["items"]["fmcg"]["root"] == "gross_profit"


def test_metric_tree_returns_structure(client: TestClient) -> None:
    response = client.get("/api/metrics/gmv/tree", params={"scenario": "ecom"})
    assert response.status_code == 200
    tree = response.json()["tree"]
    assert tree["code"] == "gmv"
    assert tree["structure"] == "multiplicative" and tree["method"] == "lmdi"
    children = {child["code"]: child for child in tree["children"]}
    assert set(children) == {"uv", "cvr", "aov"}
    ctr = next(child for child in children["uv"]["children"] if child["code"] == "ctr")
    assert ctr["decomposable"] is False, "比率型指标只展示、不分解"


def test_metric_tree_unknown_code_returns_404(client: TestClient) -> None:
    response = client.get("/api/metrics/not_exist/tree", params={"scenario": "ecom"})
    assert response.status_code == 404


def test_validate_metric_runs_every_node_sql(client: TestClient) -> None:
    response = client.post(
        "/api/metrics/gmv/validate",
        json={
            "scenario": "ecom",
            "period_start": "2026-06-01",
            "period_end": "2026-06-04",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True, body["problems"]
    assert body["nodes_checked"] == 11
    assert body["decomposable"] is True


def test_sandbox_preview_allows_normal_query(client: TestClient) -> None:
    response = client.post(
        "/api/sandbox/preview",
        json={"kind": "sql", "code": "SELECT COUNT(*) AS n FROM dw.fact_ecom_daily"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rows"][0][0] > 0
    assert body["audit_id"]


def test_sandbox_preview_blocks_unauthorized_table(client: TestClient) -> None:
    response = client.post(
        "/api/sandbox/preview",
        json={"kind": "sql", "code": "SELECT * FROM users"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "白名单" in body["blocked_reason"]


def test_sandbox_runs_endpoint_reports_audit(client: TestClient) -> None:
    response = client.get("/api/sandbox/runs", params={"limit": 20})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert body["blocked"] >= 1, "被拦截的执行也必须留痕"
    assert body["items"][0]["statement_digest"]


def test_eval_run_records_adversarial_suite(client: TestClient) -> None:
    response = client.post(
        "/api/eval/run", json={"dataset": "sandbox_adversarial", "label": "pytest"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eval_run_id"] > 0
    assert body["interception_rate"] == 1.0
    assert body["escaped"] == [] and body["false_blocks"] == []

    history = client.get("/api/eval/runs").json()
    assert history["items"][0]["summary"]["interception_rate"] == 1.0
    assert history["items"][0]["label"] == "pytest"
