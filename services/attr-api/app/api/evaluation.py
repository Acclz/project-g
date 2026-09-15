"""评测路由（技术规格 §4.7）：沙箱对抗（P3）+ 归因准确率 / 伪相关陷阱（P4）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.db import connect_app
from app.sandbox.adversarial import DATASET_NAME, run_suite
from app.sandbox.runner import SandboxRunner
from app.services.eval_suite import DATASETS as SUITE_DATASETS
from app.services.eval_suite import run_dataset

router = APIRouter(prefix="/eval", tags=["eval"])

DATASETS = (DATASET_NAME, *SUITE_DATASETS)


class EvalRequest(BaseModel):
    """触发评测：数据集 + 标签（标签用于在 eval_runs 里区分批次）。"""

    dataset: Literal["sandbox_adversarial", "attribution_eval", "correlation_traps"] = (
        DATASET_NAME
    )
    label: str = "manual"


@router.post("/run")
def run_eval(payload: EvalRequest) -> dict[str, Any]:
    """跑指定数据集并落档：数据集的判定（拦截率 100% / 误纳率 ≤10% / 守恒）决定 passed。"""

    started = datetime.now()
    if payload.dataset == DATASET_NAME:
        report = run_suite(SandboxRunner(), actor=f"eval:{payload.label}")
        payload_dict: dict[str, Any] = report.as_dict()
        passed = report.ok
    else:
        suite = run_dataset(payload.dataset, label=payload.label)
        payload_dict = suite.as_dict()
        passed = suite.passed
    payload_dict["passed"] = passed
    finished = datetime.now()
    connection = connect_app(SandboxRunner().settings.app_db)
    try:
        cursor = connection.execute(
            "INSERT INTO eval_runs (label, dataset, metrics_json, started_at, finished_at)"
            " VALUES (?,?,?,?,?)",
            (
                payload.label,
                payload.dataset,
                json.dumps(payload_dict, ensure_ascii=False),
                started.isoformat(timespec="seconds"),
                finished.isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
        run_id = int(cursor.lastrowid or 0)
    finally:
        connection.close()
    duration_ms = int((finished - started).total_seconds() * 1000)
    return {
        "eval_run_id": run_id,
        "duration_ms": duration_ms,
        "passed": passed,
        **payload_dict,
    }


@router.get("/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """历史评测记录（只做只读查询）。"""

    connection = connect_app(SandboxRunner().settings.app_db)
    try:
        rows = connection.execute(
            "SELECT id, label, dataset, metrics_json, started_at, finished_at FROM eval_runs"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    items = []
    for row in rows:
        record = dict(row)
        metrics = json.loads(record.pop("metrics_json") or "{}")
        record["summary"] = {
            "total": metrics.get("total"),
            "interception_rate": metrics.get("interception_rate"),
            "escaped": metrics.get("escaped"),
            "false_blocks": metrics.get("false_blocks"),
            "passed": metrics.get("passed"),
            "top1_rate": (metrics.get("metrics") or {}).get("top1_rate"),
            "accept_rate": (metrics.get("metrics") or {}).get("accept_rate"),
        }
        items.append(record)
    return {"items": items, "datasets": list(DATASETS)}
