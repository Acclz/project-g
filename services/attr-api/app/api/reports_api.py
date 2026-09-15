"""报告与导出路由（技术规格 §4.6）。

`structure_json` 是页面渲染的唯一数据源：预览页、Markdown、PDF、Excel 都从它出来，
所以"导出与预览完全一致"是结构决定的（保证手段见 ``app/services/export.py``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.report import ReportError, ReportService

router = APIRouter(tags=["reports"])


def service() -> ReportService:
    return ReportService()


class CreateReportRequest(BaseModel):
    """从会话中间态组装七段式报告。"""

    #: 会话 id 以路径参数为准；这里保留可选字段只是容忍前端把上下文一起回传
    session_id: int | None = None
    actor: str = "analyst"


class AnnotationRequest(BaseModel):
    """段落级批注。"""

    anchor: str = Field(description="段落锚点，例如 段3 / 段6")
    text: str
    author: str = "analyst"


class ExportRequest(BaseModel):
    """导出请求：格式只能是 md / pdf / xlsx。"""

    format: str = Field(description="md | pdf | xlsx")
    actor: str = "analyst"


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, ReportError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=f"{type(error).__name__}: {error}")


@router.post("/sessions/{session_id}/reports")
def create_report(session_id: int, payload: CreateReportRequest | None = None) -> dict[str, Any]:
    """由会话中间态组装七段报告（写 `reports.structure_json`）。"""

    actor = payload.actor if payload else "analyst"
    try:
        return service().create(session_id, actor=actor)
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.get("/reports")
def list_reports(
    domain: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """报告归档列表（按指标域 / 状态筛选）。"""

    return {"items": service().list_reports(limit=limit, domain=domain, status=status)}


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict[str, Any]:
    """报告结构（页面渲染的唯一数据源）。"""

    try:
        return service().get(report_id)
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.post("/reports/{report_id}/annotations")
def add_annotation(report_id: int, payload: AnnotationRequest) -> dict[str, Any]:
    """添加段落级批注（只追加，保留修改痕迹）。"""

    try:
        return service().add_annotation(
            report_id, anchor=payload.anchor, text=payload.text, author=payload.author
        )
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.post("/reports/{report_id}/export")
def export_report(report_id: int, payload: ExportRequest) -> dict[str, Any]:
    """导出（md | pdf | xlsx），返回导出任务与校验和。"""

    try:
        return service().export(report_id, payload.format, actor=payload.actor)
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.get("/exports/{job_id}")
def export_status(job_id: int) -> dict[str, Any]:
    """导出任务状态与下载地址（含内容校验和与文件哈希）。"""

    try:
        return service().export_status(job_id)
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error


@router.get("/exports/{job_id}/download")
def download_export(job_id: int) -> Response:
    """下载导出文件（技术规格 §4.6 的"下载地址"）。"""

    try:
        name, payload = service().export_bytes(job_id)
    except Exception as error:  # noqa: BLE001
        raise _translate(error) from error
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
