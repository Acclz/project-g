"""业务事件日历路由（技术规格 §4.5）。

约定：

* 列表按时间与类型筛选；
* 新建/修改/删除都要落审计（删除是**软删除**，保留痕迹供追溯）；
* `/api/events/match` 按会话窗口做匹配，返回"命中"与"待观察"两档，并把命中的事件标为外生变量。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import connect_app
from app.services.decomposition import MetricEngine, SliceFilter
from app.services.events import load_events_from_db, match_events, sync_seed_events

router = APIRouter(prefix="/events", tags=["events"])


class EventRequest(BaseModel):
    """新建/修改事件的请求体（维度用编码，不用内部 id）。"""

    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    start_day: str
    end_day: str | None = None
    dimensions: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""


def _connection():
    return connect_app(get_settings().app_db)


def _audit(connection, action: str, target_id: str, detail: dict[str, Any]) -> None:
    import json

    connection.execute(
        "INSERT INTO audit_logs (actor, action, target_type, target_id, detail_json)"
        " VALUES (?,?,?,?,?)",
        ("api", action, "event", target_id, json.dumps(detail, ensure_ascii=False)),
    )


@router.get("")
def list_events(
    event_type: str | None = Query(default=None, alias="type"),
    start_day: str | None = None,
    end_day: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """事件列表（按类型与时间筛选，软删除的不返回）。"""

    connection = _connection()
    try:
        sync_seed_events(connection)
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if event_type:
            conditions.append("type = ?")
            params.append(event_type)
        if start_day:
            conditions.append("start_day >= ?")
            params.append(start_day)
        if end_day:
            conditions.append("(end_day IS NULL OR end_day <= ?)")
            params.append(end_day)
        rows = connection.execute(
            "SELECT id, name, type, start_day, end_day, dimensions_json, note FROM events"
            f" WHERE {' AND '.join(conditions)} ORDER BY start_day DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        connection.close()
    import json

    return {
        "items": [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "start_day": row["start_day"],
                "end_day": row["end_day"],
                "dimensions": json.loads(row["dimensions_json"] or "{}"),
                "note": row["note"],
            }
            for row in rows
        ]
    }


@router.post("")
def create_event(payload: EventRequest) -> dict[str, Any]:
    """新建事件（写审计）。"""

    connection = _connection()
    try:
        import json

        cursor = connection.execute(
            "INSERT INTO events (name, type, start_day, end_day, dimensions_json, note)"
            " VALUES (?,?,?,?,?,?)",
            (
                payload.name,
                payload.type,
                payload.start_day,
                payload.end_day,
                json.dumps(payload.dimensions, ensure_ascii=False),
                payload.note,
            ),
        )
        event_id = int(cursor.lastrowid or 0)
        _audit(connection, "create", str(event_id), payload.model_dump())
        connection.commit()
    finally:
        connection.close()
    return {"id": event_id}


@router.put("/{event_id}")
def update_event(event_id: int, payload: EventRequest) -> dict[str, Any]:
    """修改事件（写审计）。"""

    connection = _connection()
    try:
        import json

        cursor = connection.execute(
            "UPDATE events SET name = ?, type = ?, start_day = ?, end_day = ?,"
            " dimensions_json = ?, note = ? WHERE id = ? AND deleted_at IS NULL",
            (
                payload.name,
                payload.type,
                payload.start_day,
                payload.end_day,
                json.dumps(payload.dimensions, ensure_ascii=False),
                payload.note,
                event_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"事件 {event_id} 不存在")
        _audit(connection, "update", str(event_id), payload.model_dump())
        connection.commit()
    finally:
        connection.close()
    return {"id": event_id, "updated": True}


@router.delete("/{event_id}")
def delete_event(event_id: int) -> dict[str, Any]:
    """删除事件：**软删除**（打时间戳），保留审计痕迹。"""

    connection = _connection()
    try:
        cursor = connection.execute(
            "UPDATE events SET deleted_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
            (event_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"事件 {event_id} 不存在或已删除")
        _audit(connection, "delete", str(event_id), {"soft": True})
        connection.commit()
    finally:
        connection.close()
    return {"id": event_id, "deleted": True, "soft": True}


@router.get("/match")
def match(
    scenario: str,
    window_start: str,
    window_end: str,
    channel: str | None = None,
    category: str | None = None,
    region: str | None = None,
    segment: str | None = None,
    sku: str | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    """按窗口与切片匹配事件：返回命中（入因果链、外生变量）与待观察两档。"""

    engine = MetricEngine()
    tree = engine.tree(scenario)
    slice_filter = SliceFilter(
        {
            key: (value,)
            for key, value in {
                "channel": channel,
                "category": category,
                "region": region,
                "segment": segment,
                "sku": sku,
            }.items()
            if value
        }
    )
    connection = _connection()
    try:
        sync_seed_events(connection)
        events = load_events_from_db(connection)
    finally:
        connection.close()
    matches = match_events(
        events,
        window_start=date.fromisoformat(window_start),
        window_end=date.fromisoformat(window_end),
        slice_dimensions=set(slice_filter.filters),
        scenario_dimensions=set(tree.dimensions),
        window_days=window_days or get_settings().attr_event_window_days,
        slice_values={key: list(values) for key, values in slice_filter.filters.items()},
    )
    return {
        "scenario": scenario,
        "slice": {key: list(values) for key, values in slice_filter.filters.items()},
        "matched": [item.as_dict() for item in matches if item.tier == "因果链"],
        "watching": [item.as_dict() for item in matches if item.tier == "待观察"],
    }
