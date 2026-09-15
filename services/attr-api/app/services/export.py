"""报告导出：Markdown / PDF / Excel 三格式 + 校验和一致性（E6，需求说明书 §5.11）。

核心就一条：**三种格式与页面由同一份中间态渲染**，所以一致性是结构决定的，不是人工核对出来的。
实现上加两道机器可验的闸门：

1. **内容校验和**：对中间态做规范化 JSON 后取 sha256，三种格式里都印同一个校验和
   （MD 顶部、Excel 的"校验"工作表、PDF 的 Courier 行），任何一处被改都会对不上；
2. **字段级比对**：把中间态摊平成 ``段号 / 字段 / 取值`` 的表，逐一在三种格式的文本里查找。

PDF 用 reportlab 内置的 ``STSong-Light`` CID 字体（不依赖系统字体），校验和那行用 Courier，
保证 ASCII 文本一定可被抽取；pypdf 能取回中文，字段级比对因此是全保真的。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PDF_FONT = "STSong-Light"
CHECKSUM_PREFIX = "sha256:"


class ExportError(ValueError):
    """导出失败（格式不支持、中间态结构不对等）。"""


def canonical_json(structure: dict[str, Any]) -> str:
    """规范化 JSON：键排序、紧凑分隔符、NaN 走标准字符串（保证跨进程稳定）。"""

    payload = {
        "meta": structure.get("meta", {}),
        "segments": structure.get("segments", []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_checksum(structure: dict[str, Any]) -> str:
    """中间态的内容校验和（三种格式与页面必须一致的那个值）。"""

    digest = hashlib.sha256(canonical_json(structure).encode("utf-8")).hexdigest()
    return f"{CHECKSUM_PREFIX}{digest[:32]}"


def field_rows(structure: dict[str, Any]) -> list[dict[str, Any]]:
    """摊平成字段表：这是"字段级一致"的判定依据，也是 Excel 的字段工作表。"""

    rows: list[dict[str, Any]] = []
    for segment in structure.get("segments", []):
        for item in segment.get("fields", []):
            rows.append(
                {
                    "segment_index": segment["index"],
                    "segment_title": segment["title"],
                    "label": item["label"],
                    "display": item["display"],
                }
            )
    return rows


def _meta_lines(structure: dict[str, Any]) -> list[tuple[str, str]]:
    meta = structure.get("meta", {})
    return [
        ("报告标题", str(meta.get("title", ""))),
        ("会话", f"#{meta.get('session_id')}"),
        ("场景", f"{meta.get('scenario_name', '')}（{meta.get('scenario', '')}）"),
        ("口径版本", f"v{meta.get('caliber_version')}"),
        (
            "对比期间",
            f"{meta.get('base', {}).get('start')}~{meta.get('base', {}).get('end')}"
            f" → {meta.get('current', {}).get('start')}~{meta.get('current', {}).get('end')}",
        ),
        ("锁定切片", json.dumps(meta.get("slice", {}), ensure_ascii=False)),
        ("生成时间", str(meta.get("generated_at", ""))),
        ("数据版本", str(meta.get("data_digest", ""))),
    ]


def render_markdown(structure: dict[str, Any]) -> str:
    """Markdown 导出：同一份中间态的顺序渲染（七段顺序不可变）。"""

    checksum = content_checksum(structure)
    meta = structure.get("meta", {})
    lines: list[str] = [
        f"# {meta.get('title', '经营归因报告')}",
        "",
        f"> 内容校验和：`{checksum}`（MD / PDF / Excel 与页面必须一致，E6）",
        "",
    ]
    lines.extend(f"- {label}：{value}" for label, value in _meta_lines(structure))
    for segment in structure.get("segments", []):
        lines.extend(["", f"## {segment['index']}. {segment['title']}", ""])
        lines.append(f"- 数据来源：{segment['source']}")
        lines.append(f"- 统计期间：{segment['period']}")
        lines.append("")
        lines.append("| 字段 | 取值 |")
        lines.append("| --- | --- |")
        lines.extend(
            f"| {item['label']} | {item['display']} |" for item in segment.get("fields", [])
        )
        body = segment.get("body", [])
        if body:
            lines.extend(["", "**说明**", ""])
            lines.extend(f"- {item}" for item in body)
    lines.append("")
    return "\n".join(lines)


def render_xlsx(structure: dict[str, Any]) -> bytes:
    """Excel 导出：字段工作表（逐字段）+ 正文工作表 + 校验工作表。"""

    from openpyxl import Workbook

    checksum = content_checksum(structure)
    workbook = Workbook()
    fields = workbook.active
    fields.title = "报告字段"
    fields.append(["段号", "段标题", "字段", "取值"])
    for row in field_rows(structure):
        fields.append([row["segment_index"], row["segment_title"], row["label"], row["display"]])
    body = workbook.create_sheet("报告正文")
    body.append(["段号", "段标题", "数据来源", "统计期间", "段落"])
    for segment in structure.get("segments", []):
        body.append(
            [
                segment["index"],
                segment["title"],
                segment["source"],
                segment["period"],
                "\n".join(segment.get("body", [])),
            ]
        )
    check = workbook.create_sheet("校验")
    check.append(["项", "值"])
    check.append(["内容校验和", checksum])
    for label, value in _meta_lines(structure):
        check.append([label, value])
    for sheet in (fields, body, check):
        sheet.freeze_panes = "A2"
        for column in sheet.columns:
            width = max((len(str(cell.value or "")) for cell in column), default=8)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_pdf(structure: dict[str, Any]) -> bytes:
    """PDF 导出：中文字体用内置 CID 字体，校验和行用 Courier（保证可抽取）。"""

    pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))
    checksum = content_checksum(structure)
    meta = structure.get("meta", {})
    title_style = ParagraphStyle("title", fontName=PDF_FONT, fontSize=16, leading=22)
    head_style = ParagraphStyle("head", fontName=PDF_FONT, fontSize=12, leading=18)
    body_style = ParagraphStyle("body", fontName=PDF_FONT, fontSize=9, leading=14)
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=str(meta.get("title", "经营归因报告")),
    )
    flow: list[Any] = [
        Paragraph(str(meta.get("title", "经营归因报告")), title_style),
        Spacer(1, 4),
        Paragraph(f"内容校验和 {checksum}", body_style),
        Spacer(1, 6),
    ]
    flow.append(
        _table(
            [[label, value] for label, value in _meta_lines(structure)],
            body_style,
            widths=[35 * mm, None],
        )
    )
    for segment in structure.get("segments", []):
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(f"{segment['index']}. {segment['title']}", head_style))
        flow.append(Paragraph(f"数据来源：{segment['source']}", body_style))
        flow.append(Paragraph(f"统计期间：{segment['period']}", body_style))
        flow.append(Spacer(1, 4))
        rows = [[item["label"], item["display"]] for item in segment.get("fields", [])]
        if rows:
            flow.append(_table(rows, body_style, widths=[60 * mm, None]))
        for line in segment.get("body", []):
            flow.append(Spacer(1, 2))
            flow.append(Paragraph(str(line), body_style))
    document.build(flow)
    return buffer.getvalue()


def _table(rows: list[list[str]], style: ParagraphStyle, *, widths: list[Any]) -> Table:
    table = Table(
        [[Paragraph(str(cell), style) for cell in row] for row in rows],
        colWidths=widths,
        repeatRows=0,
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9aa5b1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


RENDERERS = {"md": render_markdown, "xlsx": render_xlsx, "pdf": render_pdf}


def render(fmt: str, structure: dict[str, Any]) -> bytes:
    """按格式渲染成字节（MD 也统一返回 bytes，落盘与校验一条路径）。"""

    renderer = RENDERERS.get(fmt)
    if renderer is None:
        raise ExportError(f"不支持的导出格式：{fmt}（可用：{', '.join(RENDERERS)}）")
    payload = renderer(structure)
    return payload.encode("utf-8") if isinstance(payload, str) else payload


def extract_text(fmt: str, payload: bytes) -> str:
    """把导出内容抽成文本，供字段级比对使用。"""

    if fmt == "md":
        return payload.decode("utf-8")
    if fmt == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                chunks.extend(str(cell) for cell in row if cell is not None)
        workbook.close()
        return "\n".join(chunks)
    if fmt == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(payload))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ExportError(f"不支持的导出格式：{fmt}")


def _normalize(text: str) -> str:
    """去掉所有空白：PDF 表格里的换行/空格不该被当成"字段不一致"。"""

    return re.sub(r"\s+", "", text)


def verify_consistency(
    structure: dict[str, Any], payloads: dict[str, bytes]
) -> dict[str, Any]:
    """E6 的判定函数：三格式的校验和一致 + 每个字段的取值都能在三种格式里找到。"""

    checksum = content_checksum(structure)
    rows = field_rows(structure)
    result: dict[str, Any] = {
        "checksum": checksum,
        "fields_total": len(rows),
        "segments_total": len(structure.get("segments", [])),
        "formats": {},
    }
    for fmt, payload in payloads.items():
        text = _normalize(extract_text(fmt, payload))
        missing_checksum = checksum not in text
        missing_fields = [
            f"段{row['segment_index']}·{row['label']}"
            for row in rows
            if _normalize(row["display"]) not in text
        ]
        result["formats"][fmt] = {
            "checksum_match": not missing_checksum,
            "fields_total": len(rows),
            "fields_missing": len(missing_fields),
            "missing_fields": missing_fields[:20],
            "field_match": not missing_fields,
            "bytes": len(payload),
            "text_chars": len(text),
        }
    result["formats_present"] = sorted(payloads)
    result["consistent"] = (
        sorted(payloads) == sorted(RENDERERS)
        and all(
            item["checksum_match"] and item["field_match"] for item in result["formats"].values()
        )
    )
    return result


__all__ = [
    "CHECKSUM_PREFIX",
    "ExportError",
    "PDF_FONT",
    "RENDERERS",
    "canonical_json",
    "content_checksum",
    "extract_text",
    "field_rows",
    "render",
    "render_markdown",
    "render_pdf",
    "render_xlsx",
    "verify_consistency",
]
