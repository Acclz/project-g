"""评测路由（技术规格 §4.7）：P3 先落地"沙箱对抗"数据集，归因评测集在 P6 接入。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.db import connect_app
from app.sandbox.adversarial import DATASET_NAME, run_suite
from app.sandbox.runner import SandboxRunner

router = APIRouter(prefix="/eval", tags=["eval"])

DATASETS = (DATASET_NAME,)


class EvalRequest(BaseModel):
    """触发评测：数据集 + 标签（标签用于在 eval_runs 里区分批次）。"""

    dataset: Literal["sandbox_adversarial"] = DATASET_NAME
    label: str = "manual"


@router.post("/run")
def run_eval(payload: EvalRequest) -> dict[str, Any]:
    """跑沙箱对抗集并落档：拦截率 100% 才算通过。"""

    started = datetime.now()
    report = run_suite(SandboxRunner(), actor=f"eval:{payload.label}")
    payload_dict = report.as_dict()
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
    return {"eval_run_id": run_id, "duration_ms": duration_ms, **payload_dict}


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
        }
        items.append(record)
    return {"items": items, "datasets": list(DATASETS)}
