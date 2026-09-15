"""会话层测试：状态机、上下文锁定、并发隔离、步骤流与"结果过期"。"""

from __future__ import annotations

import os
import time

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.sandbox.runner import SandboxRunner
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.sessions import (
    STATUS_AWAITING,
    STATUS_CREATED,
    ContextLocked,
    SessionBusy,
    SessionError,
    SessionService,
)


@pytest.fixture(scope="module")
def service(sandbox_settings: Settings, ecom_injection_warehouse: SmallWarehouse) -> SessionService:
    return SessionService(
        sandbox_settings,
        engine=MetricEngine(sandbox_settings),
        sandbox=SandboxRunner(sandbox_settings),
    )


def _create(service: SessionService) -> int:
    state = service.create(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        title="pytest 会话",
    )
    assert state.status == STATUS_CREATED
    assert state.current.start == "2026-06-05" and state.current.end == "2026-06-08"
    return state.id


def test_create_locks_context_and_lists(service: SessionService) -> None:
    session_id = _create(service)
    state = service.get(session_id)
    assert state.caliber_version == 1
    assert state.slice_filter.filters == {"channel": ("paid_ads",)}
    assert state.data_digest and state.data_digest != "missing"
    listed = service.list(limit=5)
    assert any(item["id"] == session_id for item in listed)


def test_unknown_session_raises(service: SessionService) -> None:
    with pytest.raises(SessionError):
        service.get(999999)


def test_drilldown_can_only_narrow(service: SessionService) -> None:
    session_id = _create(service)
    narrowed = service.drilldown(
        session_id,
        extra_slice=SliceFilter({"channel": ("paid_ads",), "region": ("east",)}),
    )
    assert narrowed.slice_filter.filters == {"channel": ("paid_ads",), "region": ("east",)}
    with pytest.raises(ContextLocked):
        service.drilldown(session_id, extra_slice=SliceFilter({"channel": ("catering",)}))


def test_run_transitions_and_persists_results(service: SessionService) -> None:
    session_id = _create(service)
    service.start(session_id)
    # 并发隔离：同一会话第二个任务必须被拒
    with pytest.raises(SessionBusy):
        service.start(session_id)
    done = service.wait(session_id, timeout=600)
    assert done.status in (STATUS_AWAITING, "failed"), done.status
    assert done.steps, "每次状态迁移都要落步骤"
    if done.status == STATUS_AWAITING:
        assert service.hypotheses(session_id), "分析完成后应落库假设"
        assert service.evidence(session_id), "每条假设都要有证据"
        events = service.events(session_id)
        assert len(events) >= 2
        assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)


def test_result_expires_after_data_refresh(
    service: SessionService, sandbox_settings: Settings
) -> None:
    session_id = _create(service)
    assert service.is_stale(session_id) is False
    # 模拟"数据刷新"：改一下数仓文件的时间戳，指纹随之变化
    time.sleep(0.01)
    os.utime(sandbox_settings.warehouse_db, None)
    assert service.is_stale(session_id) is True, "数据刷新后旧结论必须判为过期"


def test_control_actions(service: SessionService) -> None:
    session_id = _create(service)
    paused = service.control(session_id, "pause")
    assert paused.status == STATUS_AWAITING
    cancelled = service.control(session_id, "cancel")
    assert cancelled.status == "failed"
    with pytest.raises(SessionError):
        service.control(session_id, "not-a-real-action")


def test_steps_are_persisted_for_reload(service: SessionService) -> None:
    """L1 的步骤要落 ``session_steps``：只放内存里，前端刷新一次就没了。"""

    session_id = _create(service)
    service.start(session_id)
    done = service.wait(session_id, timeout=600)
    assert done.status in (STATUS_AWAITING, "failed"), done.status
    kinds = [step["kind"] for step in done.steps]
    if done.status == STATUS_AWAITING:
        assert "query" in kinds and "hypothesis" in kinds and "verify" in kinds
        assert [step["seq"] for step in done.steps] == list(range(1, len(done.steps) + 1))


def test_drilldown_runs_l2_and_persists_pivots(service: SessionService) -> None:
    """L2：收紧切片 → 透视表与逐层守恒落库（报告第 3 段的证据源）。"""

    session_id = _create(service)
    accepted = service.drilldown(
        session_id,
        extra_slice=SliceFilter({"region": ("east",)}),
        dimensions=(("category",),),
        top_n=3,
    )
    assert accepted.slice_filter.filters == {"channel": ("paid_ads",), "region": ("east",)}
    assert accepted.status in ("analysing", STATUS_AWAITING)
    done = service.wait(session_id, timeout=600)
    records = service.drilldown_records(session_id)
    assert records, "下钻记录必须落库"
    summary = [item for item in records if item["payload"].get("summary")]
    assert summary, "下钻小结（守恒 + 覆盖率 + 关键变化特征）必须落库"
    payload = summary[-1]["payload"]
    assert payload["slice"] == {"channel": ["paid_ads"], "region": ["east"]}
    assert payload["narrowed"] is True
    if done.status == STATUS_AWAITING:
        assert payload["conserved"] is True
        assert payload["conservation"] and payload["highlights"]
        assert payload["coverage"][0]["top_n"] == 3
        assert done.steps[-1]["kind"] in ("drilldown", "plan")


def test_drilldown_rejects_empty_request(service: SessionService) -> None:
    session_id = _create(service)
    with pytest.raises(SessionError):
        service.drilldown(session_id, dimensions=())


def test_whatif_runs_and_persists_curve(service: SessionService) -> None:
    """What-If：同步返回区间曲线，并把弹性、前提条件与把握度落库（报告第 6 段的证据源）。"""

    session_id = _create(service)
    result = service.whatif(session_id, factor="price_index", adjustments=(0.0, 0.1))
    assert result["session_id"] == session_id
    assert result["status"] == STATUS_AWAITING
    curve = result["whatif"]["curve"]
    assert curve["points"] and curve["elasticity"]["sample_size"] >= 4
    assert curve["base_value"] > 0
    assert result["whatif"]["assumptions"]
    records = service.whatif_records(session_id)
    summary = [item for item in records if item["payload"].get("summary")]
    assert summary, "推演小结必须落库"
    assert summary[-1]["payload"]["factor"]["code"] == "price_index"
    assert summary[-1]["payload"]["curve"]["points"]
    # 推演结束后会话回到"等待用户"，执行位必须释放（可以接着再推一次）
    again = service.whatif(session_id, factor="budget_share", adjustments=(0.0, -0.1))
    assert again["whatif"]["factor"]["code"] == "budget_share"


def test_whatif_rejects_non_intervenable_factor(service: SessionService) -> None:
    session_id = _create(service)
    with pytest.raises(ValueError) as excinfo:
        service.whatif(session_id, factor="cvr")
    assert "可干预" in str(excinfo.value)
    # 被拒的请求不占执行位：会话状态没被改成 analysing/failed，推演可以照常进行
    assert service.get(session_id).status == STATUS_CREATED
    assert service.whatif_records(session_id) == []
