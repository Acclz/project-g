"""业务大事件日历：种子数据、入库与窗口匹配（技术规格 §5.10、需求说明书 §5.7）。

匹配口径**必须可复算**，所以相关度公式写死在代码里并解释清楚：

```
相关度 = 0.6 × 维度重合度 + 0.4 × 时间接近度
维度重合度：切片非空时 = |事件维度键 ∩ 切片维度键| / |切片维度键|
           切片为空时 = |事件维度键| / |场景维度数|
时间接近度：事件窗口与分析窗口重叠记 1.0；否则按间隔天数线性衰减（默认 3 天窗口）
```

档位（需求说明书 §5.7）：≥0.5 入因果链并标记为**外生变量**；0.3~0.5 只列"待观察"；<0.3 不展示。
低相关度的事件宁可不说，也不许"时间差不多就硬关联"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT

SEED_EVENTS_PATH = REPO_ROOT / "corpus" / "events" / "business_events.yaml"
DEFAULT_WINDOW_DAYS = 3
CAUSAL_THRESHOLD = 0.5
WATCH_THRESHOLD = 0.3
DIMENSION_WEIGHT = 0.6
TIME_WEIGHT = 0.4
#: 时间硬门禁：间隔超过窗口天数的排期事件**不得关联**（需求说明书 §5.7「禁止强行关联」）
HARD_TIME_GATE = True


@dataclass(frozen=True)
class BusinessEvent:
    """一条业务大事件：时间窗 + 影响维度 + 备注。"""

    event_id: int | None
    name: str
    type: str
    start_day: date
    end_day: date
    dimensions: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""

    def dimension_keys(self) -> set[str]:
        return set(self.dimensions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "name": self.name,
            "type": self.type,
            "window": [self.start_day.isoformat(), self.end_day.isoformat()],
            "dimensions": self.dimensions,
            "note": self.note,
        }


@dataclass(frozen=True)
class EventMatch:
    """一次匹配结果：相关度、两个分量、档位与外生变量标记。"""

    event: BusinessEvent
    dimension_overlap: float
    time_proximity: float
    relevance: float
    tier: str
    is_exogenous: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.as_dict(),
            "dimension_overlap": round(self.dimension_overlap, 4),
            "time_proximity": round(self.time_proximity, 4),
            "relevance": round(self.relevance, 4),
            "tier": self.tier,
            "is_exogenous": self.is_exogenous,
            "reason": self.reason,
        }


def load_seed_events(path: Path | None = None) -> list[BusinessEvent]:
    """读取种子事件（``corpus/events/business_events.yaml``）。"""

    target = Path(path) if path is not None else SEED_EVENTS_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    events: list[BusinessEvent] = []
    for item in payload.get("events", []):
        window = item.get("window") or []
        if len(window) != 2:
            raise ValueError(f"事件 {item.get('name')} 的 window 必须是 [起, 止]")
        events.append(
            BusinessEvent(
                event_id=None,
                name=str(item["name"]),
                type=str(item.get("type", "未分类")),
                start_day=date.fromisoformat(str(window[0])),
                end_day=date.fromisoformat(str(window[1])),
                dimensions={
                    key: [str(code) for code in codes]
                    for key, codes in (item.get("dimensions") or {}).items()
                },
                note=str(item.get("note") or ""),
            )
        )
    return events


def load_events_from_db(connection) -> list[BusinessEvent]:
    """从 ``app.events`` 读取事件（软删除的不返回）。"""

    rows = connection.execute(
        "SELECT id, name, type, start_day, end_day, dimensions_json, note FROM events"
        " WHERE deleted_at IS NULL ORDER BY start_day"
    ).fetchall()
    return [
        BusinessEvent(
            event_id=int(row["id"]),
            name=row["name"],
            type=row["type"],
            start_day=date.fromisoformat(row["start_day"]),
            end_day=date.fromisoformat(row["end_day"]) if row["end_day"] else None,
            dimensions=json.loads(row["dimensions_json"] or "{}"),
            note=row["note"] or "",
        )
        for row in rows
    ]


def sync_seed_events(connection) -> int:
    """把种子事件写入 ``app.events``（只在表为空时写入，避免覆盖人工维护的数据）。"""

    existing = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if existing:
        return 0
    events = load_seed_events()
    connection.executemany(
        "INSERT INTO events (name, type, start_day, end_day, dimensions_json, note)"
        " VALUES (?,?,?,?,?,?)",
        [
            (
                event.name,
                event.type,
                event.start_day.isoformat(),
                event.end_day.isoformat() if event.end_day else None,
                json.dumps(event.dimensions, ensure_ascii=False),
                event.note,
            )
            for event in events
        ],
    )
    connection.commit()
    return len(events)


def match_events(
    events: list[BusinessEvent],
    *,
    window_start: date,
    window_end: date,
    slice_dimensions: set[str],
    scenario_dimensions: set[str],
    window_days: int = DEFAULT_WINDOW_DAYS,
    slice_values: dict[str, list[str]] | None = None,
) -> list[EventMatch]:
    """按"维度重合 + 时间接近"给事件打分，并给出档位（结果按相关度降序）。"""

    matches: list[EventMatch] = []
    for event in events:
        overlap = _dimension_overlap(event, slice_dimensions, scenario_dimensions, slice_values)
        proximity, gap = _time_proximity(event, window_start, window_end, window_days)
        if HARD_TIME_GATE and (gap > window_days or overlap == 0.0):
            continue  # 时间不符或维度无交集：直接不展示，不用加权分"捞"回来
        relevance = DIMENSION_WEIGHT * overlap + TIME_WEIGHT * proximity
        if relevance >= CAUSAL_THRESHOLD:
            tier, exogenous = "因果链", True
        elif relevance >= WATCH_THRESHOLD:
            tier, exogenous = "待观察", False
        else:
            continue  # 低相关度不展示，避免"时间差不多就硬关联"
        if gap == 0:
            timing = "时间窗重叠"
        else:
            timing = f"间隔 {gap} 天"
        matches.append(
            EventMatch(
                event=event,
                dimension_overlap=overlap,
                time_proximity=proximity,
                relevance=relevance,
                tier=tier,
                is_exogenous=exogenous,
                reason=(
                    f"维度重合度 {overlap:.2f}"
                    "（交集 "
                    f"{sorted(event.dimension_keys() & slice_dimensions) or '按场景维度计'}）"
                    f"、时间接近度 {proximity:.2f}（{timing}）"
                ),
            )
        )
    matches.sort(key=lambda item: item.relevance, reverse=True)
    return matches


def _dimension_overlap(
    event: BusinessEvent,
    slice_dimensions: set[str],
    scenario_dimensions: set[str],
    slice_values: dict[str, list[str]] | None = None,
) -> float:
    """维度重合度用 Jaccard：交集 ÷ 并集。

    为什么不用"交集 ÷ 切片维度数"：只要事件沾了 channel 就给满分 1.0，会把"顺手沾一个维度"
    的事件顶到最前（P4 首轮实测出现过把 4 个月前的大促判成因果链）。Jaccard 会惩罚"顺手多沾维度"。
    """

    reference = slice_dimensions or scenario_dimensions
    if not reference:
        return 0.0
    union = event.dimension_keys() | reference
    if not union:
        return 0.0
    matched = 0.0
    for dimension in event.dimension_keys() & reference:
        event_values = {str(code) for code in event.dimensions.get(dimension, [])}
        slice_codes = {str(code) for code in (slice_values or {}).get(dimension, [])}
        if slice_codes and event_values and not (event_values & slice_codes):
            continue  # 维度相同但取值不相交（事件影响付费渠道、分析切片是餐饮渠道）
        matched += 1
    return matched / len(union)


def _time_proximity(
    event: BusinessEvent, window_start: date, window_end: date, window_days: int
) -> tuple[float, int]:
    """返回（时间接近度, 间隔天数）；窗口重叠时接近度为 1。"""

    if event.end_day < window_start:
        gap = (window_start - event.end_day).days
    elif event.start_day > window_end:
        gap = (event.start_day - window_end).days
    else:
        return 1.0, 0
    proximity = max(0.0, 1.0 - gap / max(1, window_days))
    return proximity, gap


def causal_chain(matches: list[EventMatch]) -> list[dict[str, Any]]:
    """因果链：只放"入链"的事件，并统一标注为外生变量。"""

    return [
        {
            "event": match.event.as_dict(),
            "relevance": round(match.relevance, 4),
            "tier": match.tier,
            "is_exogenous": match.is_exogenous,
            "reason": match.reason,
        }
        for match in matches
        if match.tier == "因果链"
    ]


__all__ = [
    "CAUSAL_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "WATCH_THRESHOLD",
    "BusinessEvent",
    "EventMatch",
    "causal_chain",
    "load_events_from_db",
    "load_seed_events",
    "match_events",
    "sync_seed_events",
]
