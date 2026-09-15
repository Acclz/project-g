"""会话路由（技术规格 §4.4）：状态机、追问、SSE 步骤流、下钻与任务控制。

路由层只做参数校验、错误码映射与响应组装；状态与执行在 ``app/services/sessions.py``。

SSE 与 ``session_steps.kind`` 一一对应（技术规格 §4.4）：事件里的 ``kind`` 就是
``plan / query / drilldown / hypothesis / verify / event / whatif / report``。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.decomposition import Period, SliceFilter
from app.services.drilldown import parse_dimensions
from app.services.sessions import (
    STATUS_AWAITING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    ContextLocked,
    SessionBusy,
    SessionError,
    SessionService,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])

STREAM_POLL_SECONDS = 0.3
STREAM_MAX_SECONDS = 180.0
TERMINAL_STATUSES = (STATUS_AWAITING, STATUS_COMPLETED, STATUS_FAILED)

_SERVICES: dict[str, SessionService] = {}


def service() -> SessionService:
    """按数据库路径缓存服务实例：事件流在内存里，必须跨请求共享同一个实例。"""

    settings = get_settings()
    key = f"{settings.app_db}|{settings.warehouse_db}"
    if key not in _SERVICES:
        _SERVICES[key] = SessionService(settings)
    return _SERVICES[key]


class CreateSessionRequest(BaseModel):
    """创建会话：锁定指标场景、两期与初始切片。"""

    scenario: str = Field(description="场景编码：ecom / fmcg")
    base_start: str
    base_end: str
    current_start: str
    current_end: str
    channel: str | None = None
    category: str | None = None
    region: str | None = None
    segment: str | None = None
    sku: str | None = None
    title: str = ""
    actor: str = "analyst"


class MessageRequest(BaseModel):
    """追问：默认继承锁定上下文，只带自然语言与可选的切片收紧。"""

    message: str = ""
    actor: str = "analyst"


class DrilldownRequest(MessageRequest):
    """下钻：切片只能收紧（放宽会被 409 拒绝）。"""

    channel: str | None = None
    category: str | None = None
    region: str | None = None
    segment: str | None = None
    sku: str | None = None
    #: 要透视的维度组合，例如 ``[["channel"], ["channel", "category"]]``；
    #: 省略时按场景声明的维度自动选（每个组合最多 3 维，见需求说明书 §5.3）
    dimensions: list[list[str] | str] | None = None
    top_n: int = Field(default=5, ge=1, le=50)


def _slice_from(payload: Any) -> SliceFilter:
    values = {
        key: (getattr(payload, key),)
        for key in ("channel", "category", "region", "segment", "sku")
        if getattr(payload, key, None)
    }
    return SliceFilter(values)


def _translate(error: Exception) -> HTTPException:
    """会话层异常 → HTTP 码：并发冲突与上下文锁定都是 409，其余是 4xx。"""

    if isinstance(error, (SessionBusy, ContextLocked)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SessionError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=f"{type(error).__name__}: {error}")


@router.post("")
def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
    """新建会话（锁定指标 + 口径版本 + 两期 + 切片）。"""

    try:
        state = service().create(
            scenario=payload.scenario,
            base=Period(payload.base_start, payload.base_end),
            current=Period(payload.current_start, payload.current_end),
            slice_filter=_slice_from(payload),
            title=payload.title,
            actor=payload.actor,
        )
    except Exception as error:  # noqa: BLE001 - 统一映射成 HTTP 错误
        raise _translate(error) from error
    return state.as_dict()


@router.get("")
def list_sessions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """会话列表（按创建时间倒序）。"""

    return {"items": service().list(limit=limit)}


@router.get("/{session_id}")
def get_session(session_id: int) -> dict[str, Any]:
    """会话详情：状态、锁定上下文、步骤流与数据版本。"""

    try:
        return service().get(session_id).as_dict()
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.post("/{session_id}/messages")
def send_message(session_id: int, payload: MessageRequest) -> dict[str, Any]:
    """发送追问并触发执行（同一会话同时只允许一个任务）。"""

    try:
        return service().start(session_id, message=payload.message, actor=payload.actor).as_dict()
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.get("/{session_id}/stream")
def stream(session_id: int, since: int = Query(default=-1)) -> StreamingResponse:
    """SSE 步骤流：增量推送事件，任务结束且无新事件后自动收尾。"""

    def _events() -> Iterator[str]:
        cursor = since
        started = time.perf_counter()
        while time.perf_counter() - started < STREAM_MAX_SECONDS:
            for event in service().events(session_id, since=cursor):
                cursor = event["seq"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            try:
                status = service().get(session_id).status
            except SessionError:
                yield 'data: {"kind": "error", "status": "not_found"}\n\n'
                return
            if status in TERMINAL_STATUSES and not service().events(session_id, since=cursor):
                yield 'data: {"kind": "report", "status": "stream_closed"}\n\n'
                return
            time.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.post("/{session_id}/drilldown")
def drilldown(session_id: int, payload: DrilldownRequest) -> dict[str, Any]:
    """维度下钻（L2）：在锁定切片之上只允许收紧，随后跑透视与逐层守恒。"""

    extra = _slice_from(payload)
    try:
        dimensions = parse_dimensions(payload.dimensions)
    except Exception as error:  # noqa: BLE001 - 维度组合形状不对属于参数错误
        raise HTTPException(status_code=400, detail=str(error)) from error
    if extra.empty and not dimensions:
        raise HTTPException(
            status_code=400, detail="下钻必须至少指定一个维度取值，或指定要透视的维度"
        )
    try:
        return service().drilldown(
            session_id,
            extra_slice=extra,
            dimensions=dimensions,
            top_n=payload.top_n,
            actor=payload.actor,
            message=payload.message,
        ).as_dict()
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.post("/{session_id}/{action}")
def control(session_id: int, action: str, payload: MessageRequest) -> dict[str, Any]:
    """任务控制：``pause`` / ``cancel`` / ``resume``。"""

    if action not in ("pause", "cancel", "resume"):
        raise HTTPException(status_code=404, detail=f"不支持的控制动作：{action}")
    try:
        return service().control(session_id, action, actor=payload.actor).as_dict()
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.get("/{session_id}/hypotheses")
def hypotheses(session_id: int) -> dict[str, Any]:
    """假设列表（含置信度、状态与排除理由）。"""

    return {"items": service().hypotheses(session_id)}


@router.get("/{session_id}/evidence")
def evidence(session_id: int) -> dict[str, Any]:
    """证据链（SQL 摘要、样本量、p 值、效应量）。"""

    return {"items": service().evidence(session_id)}


@router.get("/{session_id}/drilldowns")
def drilldowns(session_id: int) -> dict[str, Any]:
    """下钻记录：透视表、逐层守恒、覆盖率与关键变化特征（报告第 3 段的证据源）。"""

    return {"items": service().drilldown_records(session_id)}
