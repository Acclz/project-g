"""三格式导出与一致性校验（E6）：同一中间态 → MD / PDF / XLSX，字段级一致。"""

from __future__ import annotations

import pytest
from conftest import SmallWarehouse

from app.config import Settings
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.export import (
    ExportError,
    content_checksum,
    extract_text,
    field_rows,
    render,
    verify_consistency,
)
from app.services.report import ReportService
from app.services.sessions import SessionService


@pytest.fixture(scope="module")
def service(sandbox_settings: Settings, ecom_injection_warehouse: SmallWarehouse) -> ReportService:
    engine = MetricEngine(sandbox_settings)
    return ReportService(
        sandbox_settings, engine=engine, sessions=SessionService(sandbox_settings, engine=engine)
    )


@pytest.fixture(scope="module")
def report(service: ReportService) -> dict:
    state = service.sessions.create(
        scenario="ecom",
        base=Period("2026-06-01", "2026-06-04"),
        current=Period("2026-06-05", "2026-06-08"),
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        title="pytest 导出会话",
    )
    service.sessions.start(state.id)
    service.sessions.wait(state.id, timeout=900)
    service.sessions.whatif(state.id, factor="price_index", adjustments=(0.0, 0.1))
    return service.create(state.id, actor="pytest")


def test_three_formats_render_and_agree(report: dict) -> None:
    payloads = {fmt: render(fmt, report) for fmt in ("md", "pdf", "xlsx")}
    assert all(payloads.values())
    result = verify_consistency(report, payloads)
    assert result["consistent"] is True, result
    assert result["fields_total"] > 20
    for fmt, item in result["formats"].items():
        assert item["checksum_match"], fmt
        assert item["field_match"], (fmt, item["missing_fields"])


def test_checksum_is_stable_and_content_bound(report: dict) -> None:
    first = content_checksum(report)
    assert first == content_checksum(report)
    tampered = {**report, "segments": [*report["segments"]]}
    tampered["segments"][0] = {
        **tampered["segments"][0],
        "fields": [
            {**tampered["segments"][0]["fields"][0], "display": "被改过的取值"}
            for _ in tampered["segments"][0]["fields"]
        ],
    }
    assert content_checksum(tampered) != first, "内容变了校验和必须变"
    payloads = {fmt: render(fmt, tampered) for fmt in ("md", "pdf", "xlsx")}
    assert verify_consistency(tampered, payloads)["consistent"] is True, (
        "改的是中间态本身：三格式仍应一致（一致性的对象是同一份中间态）"
    )


def test_field_rows_cover_all_segments(report: dict) -> None:
    rows = field_rows(report)
    assert {row["segment_index"] for row in rows} == set(range(1, 8))
    assert len(rows) == sum(len(segment["fields"]) for segment in report["segments"])


def test_export_jobs_persist_file_and_download(service: ReportService, report: dict) -> None:
    report_id = report["meta"]["report_id"]
    jobs = []
    for fmt in ("md", "pdf", "xlsx"):
        job = service.export(report_id, fmt, actor="pytest")
        jobs.append(job)
        assert job["status"] == "completed"
        assert job["content_checksum"] == content_checksum(report)
        assert job["file_size"] and job["file_size"] > 0
        assert job["file_sha256"]
    assert {job["format"] for job in jobs} == {"md", "pdf", "xlsx"}
    # 三种格式落三个文件，内容校验和完全一致（E6 的第二条闸门）
    assert len({job["content_checksum"] for job in jobs}) == 1
    name, payload = service.export_bytes(jobs[0]["id"])
    assert name.endswith(".md")
    assert b"sha256:" in payload
    status = service.export_status(jobs[1]["id"])
    assert status["download_url"].endswith(f"/{jobs[1]['id']}/download")


def test_verify_exports_end_to_end(service: ReportService, report: dict) -> None:
    result = service.verify_exports(report["meta"]["report_id"])
    assert result["consistent"] is True, result
    assert result["formats_present"] == ["md", "pdf", "xlsx"]
    assert result["formats"]["pdf"]["text_chars"] > 500
    assert result["formats"]["xlsx"]["text_chars"] > 500


def test_unknown_format_is_rejected(report: dict) -> None:
    with pytest.raises(ExportError):
        render("docx", report)


def test_pdf_text_is_extractable_for_chinese(report: dict) -> None:
    """PDF 用内置 CID 字体，中文必须能被抽取出来（否则"字段级一致"无从谈起）。"""

    payload = render("pdf", report)
    text = extract_text("pdf", payload)
    assert "异动背景与问题定义" in text
    assert "可落地策略行动清单" in text
    assert "内容校验和" in text
