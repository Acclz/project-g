"""事件日历测试：匹配口径、时间硬门禁、软删除与入库。"""

from __future__ import annotations

from datetime import date

from app.config import Settings
from app.db import connect_app
from app.services.events import (
    CAUSAL_THRESHOLD,
    BusinessEvent,
    causal_chain,
    load_events_from_db,
    load_seed_events,
    match_events,
    sync_seed_events,
)

SCENARIO_DIMENSIONS = {"channel", "category", "region", "segment"}


def test_seed_events_are_well_formed() -> None:
    events = load_seed_events()
    assert len(events) >= 5
    for event in events:
        assert event.name and event.type
        assert event.end_day >= event.start_day
        assert event.dimensions, "每条事件都要声明影响维度，否则无法匹配"


def test_overlapping_event_enters_causal_chain() -> None:
    events = [
        BusinessEvent(
            event_id=None,
            name="竞品降价",
            type="竞品动作",
            start_day=date(2026, 6, 4),
            end_day=date(2026, 6, 12),
            dimensions={"channel": ["paid_ads"]},
        )
    ]
    matches = match_events(
        events,
        window_start=date(2026, 6, 5),
        window_end=date(2026, 6, 11),
        slice_dimensions={"channel"},
        scenario_dimensions=SCENARIO_DIMENSIONS,
    )
    assert len(matches) == 1
    match = matches[0]
    assert match.relevance >= CAUSAL_THRESHOLD
    assert match.tier == "因果链" and match.is_exogenous is True
    assert causal_chain(matches)[0]["is_exogenous"] is True


def test_far_away_event_is_not_linked() -> None:
    """时间不符不得关联（需求说明书 §5.7「禁止强行关联」）。"""

    events = load_seed_events()  # 含 1 月年货节、6 月大促等
    matches = match_events(
        events,
        window_start=date(2026, 6, 5),
        window_end=date(2026, 6, 11),
        slice_dimensions={"channel"},
        scenario_dimensions=SCENARIO_DIMENSIONS,
    )
    names = [match.event.name for match in matches]
    assert "年货节大促" not in names, "4 个月前的事件不允许进因果链"
    assert all(match.time_proximity > 0 for match in matches)


def test_dimension_overlap_uses_jaccard() -> None:
    """沾的维度越多，重合度越低——防止"顺手沾一个维度"就被判成相关。"""

    narrow = BusinessEvent(
        None, "窄事件", "类型", date(2026, 6, 5), date(2026, 6, 5), {"channel": ["paid_ads"]}
    )
    broad = BusinessEvent(
        None,
        "宽事件",
        "类型",
        date(2026, 6, 5),
        date(2026, 6, 5),
        {"channel": ["paid_ads"], "category": ["grain_oil"]},
    )
    slice_filter = {"channel"}
    narrow_match = match_events(
        [narrow],
        window_start=date(2026, 6, 5),
        window_end=date(2026, 6, 5),
        slice_dimensions=slice_filter,
        scenario_dimensions=SCENARIO_DIMENSIONS,
    )[0]
    broad_match = match_events(
        [broad],
        window_start=date(2026, 6, 5),
        window_end=date(2026, 6, 5),
        slice_dimensions=slice_filter,
        scenario_dimensions=SCENARIO_DIMENSIONS,
    )[0]
    assert narrow_match.dimension_overlap == 1.0
    assert broad_match.dimension_overlap == 0.5
    assert narrow_match.relevance > broad_match.relevance


def test_event_without_dimension_intersection_is_ignored() -> None:
    events = [
        BusinessEvent(
            None,
            "只影响区域",
            "外部环境",
            date(2026, 6, 5),
            date(2026, 6, 6),
            {"region": ["east"]},
        )
    ]
    matches = match_events(
        events,
        window_start=date(2026, 6, 5),
        window_end=date(2026, 6, 6),
        slice_dimensions={"channel"},
        scenario_dimensions=SCENARIO_DIMENSIONS,
    )
    assert matches == [], "维度无交集时不得关联"


def test_seed_events_are_persisted_once(sandbox_settings: Settings) -> None:
    connection = connect_app(sandbox_settings.app_db)
    try:
        sync_seed_events(connection)  # 首次可能已有数据（同一会话里别的用例先播种过）
        second = sync_seed_events(connection)
        events = load_events_from_db(connection)
    finally:
        connection.close()
    assert second == 0, "播种必须幂等，不能覆盖人工维护的数据"
    assert len(events) >= len(load_seed_events())
