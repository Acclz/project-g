"""七段式报告：单一中间态、必备字段、结论绑定贡献额与行动项三要素（需求说明书 §9/§5.11）。"""

from __future__ import annotations

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.report import SEGMENTS, ReportError, ReportService, load_playbook
from app.services.sessions import SessionService


@pytest.fixture(scope="module")
def service(sandbox_settings: Settings, ecom_injection_warehouse: SmallWarehouse) -> ReportService:
    engine = MetricEngine(sandbox_settings)
    sessions = SessionService(sandbox_settings, engine=engine)
    return ReportService(sandbox_settings, engine=engine, sessions=sessions)


@pytest.fixture(scope="module")
def session_id(service: ReportService) -> int:
    """一个跑过 L1 + L2 + What-If 的会话：报告七段都能有真实数据。"""

    state = service.sessions.create(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        title="pytest 报告会话",
    )
    service.sessions.start(state.id)
    service.sessions.wait(state.id, timeout=900)
    service.sessions.drilldown(state.id, dimensions=(("category",),), top_n=3)
    service.sessions.wait(state.id, timeout=900)
    service.sessions.whatif(state.id, factor="price_index", adjustments=(0.0, 0.1))
    return state.id


def test_structure_has_seven_segments_in_order(service: ReportService, session_id: int) -> None:
    structure = service.build(session_id)
    assert [segment["code"] for segment in structure["segments"]] == [
        code for code, _ in SEGMENTS
    ]
    assert [segment["index"] for segment in structure["segments"]] == list(range(1, 8))
    assert [segment["title"] for segment in structure["segments"]] == [
        title for _, title in SEGMENTS
    ]
    for segment in structure["segments"]:
        assert segment["source"], f"第 {segment['index']} 段必须标注数据来源"
        assert segment["period"], f"第 {segment['index']} 段必须标注统计期间"
        assert segment["fields"], f"第 {segment['index']} 段不能没有必备字段"


def test_required_fields_per_segment(service: ReportService, session_id: int) -> None:
    structure = service.build(session_id)
    labels = {
        segment["index"]: [item["label"] for item in segment["fields"]]
        for segment in structure["segments"]
    }
    # 段 1：口径版本 + 对比方式（§9 必备字段）
    assert "口径版本" in labels[1] and "对比方式" in labels[1]
    # 段 2：残差必须显式给出
    assert any("残差" in item for item in labels[2])
    # 段 3：TOP N 与覆盖率
    assert any("覆盖率" in item for item in labels[3])
    # 段 4：每条结论绑定贡献额（display 里带金额）
    assert any("首要原因" in item for item in labels[4])
    # 段 5：检验方法与样本量
    assert any("检验方法与样本量" in item for item in labels[5])
    # 段 6：推演结论的区间
    assert any("区间" in item or "弹性" in item for item in labels[6])
    # 段 7：行动项三要素
    for key in ("驱动因子", "业务动作", "责任岗位", "预估周期", "预期收益区间"):
        assert any(key in item for item in labels[7]), key


def test_conclusions_bind_contribution_amounts(service: ReportService, session_id: int) -> None:
    structure = service.build(session_id)
    segment = structure["segments"][3]
    primary = next(item for item in segment["fields"] if item["label"] == "首要原因")
    assert primary["value"] is not None
    assert "分" in primary["display"] and "贡献率" in primary["display"]
    assert segment["data"]["ranked"], "结论必须带排序后的贡献清单"


def test_actions_have_owner_cycle_and_range(service: ReportService, session_id: int) -> None:
    structure = service.build(session_id)
    segment = structure["segments"][6]
    ranges = [
        item for item in segment["fields"] if item["label"].endswith("预期收益区间")
    ]
    assert ranges, "行动项必须给出预期收益区间或说明为什么不给"
    assert all(item["display"] for item in ranges)


def test_report_is_persisted_and_annotated(service: ReportService, session_id: int) -> None:
    report = service.create(session_id, actor="pytest")
    assert report["meta"]["report_id"] > 0
    assert report["meta"]["report_status"] == "draft"
    assert service.get(report["meta"]["report_id"])["segments"]
    annotation = service.add_annotation(
        report["meta"]["report_id"], anchor="段3", text="这里的覆盖率需要再核一遍", author="pytest"
    )
    assert annotation["id"] > 0
    fetched = service.get(report["meta"]["report_id"])
    assert any(item["anchor"] == "段3" for item in fetched["annotations"])
    listed = service.list_reports(domain="ecom")
    assert any(item["id"] == report["meta"]["report_id"] for item in listed)


def test_stale_session_cannot_enter_report(
    sandbox_settings: Settings, service: ReportService, session_id: int
) -> None:
    """数据刷新后旧结论标"结果过期"：必须先重算才能进报告（§5.9）。"""

    import os
    import time

    state = service.sessions.create(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        title="pytest 过期会话",
    )
    original = sandbox_settings.warehouse_db.stat()
    time.sleep(0.01)
    os.utime(sandbox_settings.warehouse_db, None)
    try:
        with pytest.raises(ReportError) as excinfo:
            service.build(state.id)
        assert "过期" in str(excinfo.value)
    finally:
        # 复位数仓的原始时间戳：其他用例的数据版本快照不能被这一次模拟刷新带偏
        os.utime(
            sandbox_settings.warehouse_db,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )


def test_playbook_declares_itself_as_project_defined() -> None:
    playbook = load_playbook()
    assert "项目自定参数" in playbook["source_note"]
    assert playbook["factors"]["ecom"]["cvr"]["knob"] is None
    assert "不给预期收益区间" in playbook["factors"]["ecom"]["cvr"]["note"]


def test_segment_six_without_whatif_says_so(service: ReportService) -> None:
    """没跑推演的会话：段 6 必须老实说没有数据，而不是编一个区间。"""

    state = service.sessions.create(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        title="pytest 无推演",
    )
    structure = service.build(state.id)
    segment = structure["segments"][5]
    assert segment["fields"][0]["display"] == "本次会话未做 What-If 推演（段 6 无数据）"
    assert any("不编区间" in line for line in segment["body"])


def test_whatif_report_uses_persisted_curve(service: ReportService, session_id: int) -> None:
    """段 6 的数字与推演记录一致（页面、MD/PDF/Excel 与推演面板同源）。"""

    structure = service.build(session_id)
    recorded = [
        item["payload"]
        for item in service.sessions.whatif_records(session_id)
        if item["payload"].get("summary")
    ]
    assert recorded
    curve = recorded[-1]["curve"]
    segment = structure["segments"][5]
    elasticity_field = next(
        item for item in segment["fields"] if item["label"].endswith("弹性")
    )
    assert f"{curve['elasticity']['value']:+.3f}" in elasticity_field["display"]
