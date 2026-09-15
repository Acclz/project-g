"""L1 链路编排测试：步骤流、假设落库、证据链、结论组装。"""

from __future__ import annotations

from conftest import SmallWarehouse

from app.config import Settings
from app.db import connect_app
from app.sandbox.runner import SandboxRunner
from app.services.analysis import AnalysisRequest, run_analysis
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.seed import database_overview, seed_reference_data


def _request(persist: bool) -> AnalysisRequest:
    return AnalysisRequest(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        title="pytest 分析",
        actor="pytest",
        persist=persist,
    )


def test_reference_data_seeding_is_idempotent(sandbox_settings: Settings) -> None:
    engine = MetricEngine(sandbox_settings)
    seed_reference_data(engine)  # 首次可能已有数据（同一会话里别的用例先播种过）
    second = seed_reference_data(engine)
    assert second.as_dict() == {"users": 0, "metrics": 0, "metric_versions": 0, "events": 0}
    overview = database_overview(engine)
    assert overview["users"] >= 3
    assert overview["metric_definitions"] >= 20
    assert overview["events"] >= 5


def test_run_analysis_without_persistence(sandbox_settings: Settings) -> None:
    engine = MetricEngine(sandbox_settings)
    sandbox = SandboxRunner(sandbox_settings)
    report = run_analysis(_request(persist=False), engine=engine, sandbox=sandbox)
    assert report.session_id is None
    kinds = [step["kind"] for step in report.steps]
    assert kinds[:2] == ["query", "hypothesis"]
    assert "verify" in kinds and "event" in kinds
    assert report.decomposition.ok
    assert report.conclusion["primary_cause"]
    assert "不混用" in report.conclusion["disclaimer"]


def test_run_analysis_persists_hypotheses_and_evidence(
    sandbox_settings: Settings, ecom_injection_warehouse: SmallWarehouse
) -> None:
    engine = MetricEngine(sandbox_settings)
    sandbox = SandboxRunner(sandbox_settings)
    report = run_analysis(_request(persist=True), engine=engine, sandbox=sandbox)
    assert report.session_id and report.session_id > 0
    connection = connect_app(sandbox_settings.app_db)
    try:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (report.session_id,)
        ).fetchone()
        assert session["status"] == "completed"
        assert session["title"] == "pytest 分析"
        steps = connection.execute(
            "SELECT kind, duration_ms FROM session_steps WHERE session_id = ? ORDER BY seq",
            (report.session_id,),
        ).fetchall()
        assert [row["kind"] for row in steps] == [step["kind"] for step in report.steps]
        assert all(row["duration_ms"] >= 0 for row in steps)
        hypotheses = connection.execute(
            "SELECT * FROM hypotheses WHERE session_id = ?", (report.session_id,)
        ).fetchall()
        assert len(hypotheses) == len(report.hypotheses)
        for row in hypotheses:
            assert row["statement"] and row["reason"]
            assert row["status"] in ("verified", "excluded", "insufficient")
        evidence = connection.execute(
            "SELECT * FROM evidence WHERE session_id = ?", (report.session_id,)
        ).fetchall()
        assert len(evidence) == len(report.hypotheses), "每条假设都要留下证据链"
        assert all(row["sql_digest"] for row in evidence)
    finally:
        connection.close()


def test_report_renders_all_sections(sandbox_settings: Settings) -> None:
    engine = MetricEngine(sandbox_settings)
    report = run_analysis(
        _request(persist=False), engine=engine, sandbox=SandboxRunner(sandbox_settings)
    )
    text = report.render()
    for section in ("假设：", "伪相关四步检查：", "事件匹配：", "结论："):
        assert section in text
    payload = report.as_dict()
    assert payload["hypotheses"] and payload["steps"]
    assert payload["conclusion"]["primary_cause"]
