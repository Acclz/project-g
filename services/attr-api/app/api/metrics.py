"""指标与指标树路由（技术规格 §4.2）。

路由层只做参数校验与响应组装：结构来自指标字典，取值来自数仓，分解在用例层。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import connect_warehouse_readonly
from app.services.decomposition import DecompositionError, MetricEngine, Period

router = APIRouter(prefix="/metrics", tags=["metrics"])


class ValidateRequest(BaseModel):
    """口径校验请求：可指定校验用的期间，默认取数仓里最后 7 天。"""

    scenario: str = Field(description="场景编码：ecom / fmcg")
    period_start: str | None = None
    period_end: str | None = None


@router.get("")
def list_metrics(domain: str | None = None) -> dict[str, Any]:
    """指标字典清单（按场景分组），供指标字典页与校验脚本共用。"""

    engine = MetricEngine()
    payload = engine.describe()
    if domain:
        if domain not in payload:
            raise HTTPException(status_code=404, detail=f"未知场景：{domain}")
        payload = {domain: payload[domain]}
    return {"items": payload, "scenarios": engine.scenarios()}


@router.get("/{code}/tree")
def metric_tree(
    code: str, scenario: str = Query(description="场景编码：ecom / fmcg")
) -> dict[str, Any]:
    """某个指标的拆解树（默认只返回结构，不含数值）。"""

    engine = MetricEngine()
    try:
        tree = engine.tree(scenario)
    except DecompositionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    node = tree.find(code)
    if node is None:
        raise HTTPException(status_code=404, detail=f"场景 {scenario} 里没有指标 {code}")
    return {
        "scenario": scenario,
        "scenario_name": tree.scenario_name,
        "fact_table": engine.fact_table(scenario),
        "dimensions": tree.dimensions,
        "tree": _serialize_node(node),
    }


@router.post("/{code}/validate")
def validate_metric(code: str, payload: ValidateRequest) -> dict[str, Any]:
    """口径校验：结构可分解性 + 每段 SQL 能否在数仓上出数。"""

    engine = MetricEngine()
    try:
        tree = engine.tree(payload.scenario)
    except DecompositionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    node = tree.find(code)
    if node is None:
        raise HTTPException(status_code=404, detail=f"场景 {payload.scenario} 里没有指标 {code}")

    period = _resolve_period(engine, payload)
    checks = engine.validate_sql(payload.scenario, period)
    problems = [
        {"code": check["code"], "problem": check["problem"]}
        for check in checks
        if not check["ok"]
    ]
    return {
        "scenario": payload.scenario,
        "metric": code,
        "period": period.as_dict(),
        "structure": node.structure,
        "decomposable": node.decomposable,
        "nodes_checked": len(checks),
        "ok": not problems,
        "problems": problems,
        "checks": checks,
    }


def _resolve_period(engine: MetricEngine, payload: ValidateRequest) -> Period:
    if payload.period_start and payload.period_end:
        return Period(payload.period_start, payload.period_end, label="指定期间")
    conn = connect_warehouse_readonly(engine.settings.warehouse_db)
    try:
        end = conn.execute("SELECT MAX(day) FROM dw.dim_date").fetchone()[0]
        start = conn.execute(
            "SELECT MIN(day) FROM (SELECT day FROM dw.dim_date ORDER BY day DESC LIMIT 7)"
        ).fetchone()[0]
    finally:
        conn.close()
    if not end or not start:
        raise HTTPException(status_code=409, detail="数仓维度表为空，请先生成数仓")
    return Period(str(start), str(end), label="默认期间（数仓末尾 7 天）")


def _serialize_node(node) -> dict[str, Any]:
    return {
        "code": node.code,
        "name": node.name,
        "level": node.level,
        "structure": node.structure,
        "method": node.method,
        "sign": node.sign,
        "formula": node.formula,
        "unit": node.unit,
        "caliber": node.caliber,
        "decomposable": node.decomposable,
        "children": [_serialize_node(child) for child in node.children],
    }
