"""端到端演示（P5·L3 后半）：七段式报告 + 三格式导出 + 一致性比对（E6）。

这是需求说明书 §9（七段式报告）与 §5.11（单一中间态）的可执行版本，也是 E6 的出数脚本：

1. **组装**：拿一条会话（含 L1 分解、L2 下钻、What-If 推演），组装成七段中间态；
2. **导出**：由同一份中间态渲染 Markdown / PDF / Excel；
3. **比对**：三种格式的**内容校验和**必须一致，且每个字段的取值都要在三种格式里找到
   （字段级一致，缺一个就报出来）。

用法：``python services/attr-api/scripts/demo_report.py [--json <路径>] [--session <id>]``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
for extra in (SERVICE_ROOT, REPO_ROOT / "packages" / "attribution"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.config import get_settings  # noqa: E402
from app.db import connect_app  # noqa: E402
from app.services.decomposition import MetricEngine, Period, SliceFilter  # noqa: E402
from app.services.drilldown import default_dimensions  # noqa: E402
from app.services.report import ReportService  # noqa: E402
from app.services.sessions import SessionService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="七段式报告与导出一致性演示")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "report_consistency_full.json",
        help="一致性比对结果落盘路径",
    )
    parser.add_argument("--session", type=int, default=None, help="复用已有会话 id")
    parser.add_argument("--keep-session", action="store_true", help="演示完不新建会话")
    return parser.parse_args()


def ground_truth_context() -> tuple[str, Period, Period, SliceFilter, str]:
    connection = connect_app(get_settings().app_db)
    try:
        row = connection.execute(
            "SELECT * FROM ground_truth WHERE scenario = 'ecom' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit("数仓里没有真值清单，先跑 generate_warehouse.py --profile full")
    current = Period(row["day"], row["window_end"], label="真因窗口")
    return (
        row["scenario"],
        current.previous(),
        current,
        SliceFilter.from_json(row["dimension_json"]),
        str(row["note"]),
    )


def prepare_session(service: SessionService, keep: bool) -> tuple[int, dict[str, Any]]:
    """跑一条完整会话：L1 → L2 下钻 → What-If，作为报告的输入。"""

    scenario, base, current, slice_filter, note = ground_truth_context()
    state = service.create(
        scenario=scenario,
        base=base,
        current=current,
        slice_filter=slice_filter,
        title=f"演示·报告（{scenario} 真因 #{1}）",
        actor="demo",
    )
    print(f"会话 #{state.id} 已创建：{scenario} {base.start}~{base.end} → "
          f"{current.start}~{current.end}　切片 {slice_filter.as_json()}")
    print(f"真值说明：{note}")
    service.start(state.id, actor="demo")
    done = service.wait(state.id, timeout=900)
    print(f"L1 完成：状态 {done.status}，步骤 {len(done.steps)} 条")
    plan = default_dimensions(service.engine, scenario, state.slice_filter)
    service.drilldown(state.id, dimensions=plan[:1], top_n=5, actor="demo")
    done = service.wait(state.id, timeout=900)
    print(f"L2 完成：状态 {done.status}，下钻记录 {len(service.drilldown_records(state.id))} 条")
    result = service.whatif(state.id, factor="price_index", actor="demo")
    elasticity = result["whatif"]["curve"]["elasticity"]
    print(
        f"What-If 完成：价格水平弹性 {elasticity['value']:+.3f}"
        f"（90% 区间 {elasticity['low']:+.3f} ~ {elasticity['high']:+.3f}）"
    )
    _ = keep
    return state.id, {"scenario": scenario, "note": note}


def main() -> int:
    args = parse_args()
    settings = get_settings()
    engine = MetricEngine(settings)
    service = ReportService(settings, engine=engine)
    context: dict[str, Any] = {}
    if args.session:
        session_id = args.session
    else:
        session_id, context = prepare_session(service.sessions, args.keep_session)

    report = service.create(session_id, actor="demo")
    report_id = report["meta"]["report_id"]
    print("=" * 78)
    print(f"报告 #{report_id}：{report['meta']['title']}")
    for segment in report["segments"]:
        print(f"  第 {segment['index']} 段 {segment['title']}：字段 {len(segment['fields'])} 个")

    jobs = []
    for fmt in ("md", "pdf", "xlsx"):
        job = service.export(report_id, fmt, actor="demo")
        jobs.append(job)
        print(
            f"  导出 {fmt}：{job['file_name']}（{job['file_size']} 字节，"
            f"校验和 {job['content_checksum']}，文件 sha256 {job['file_sha256'][:16]}…）"
        )

    verification = service.verify_exports(report_id)
    print("=" * 78)
    print(
        f"字段级比对：共 {verification['fields_total']} 个字段 × 3 种格式；"
        f"一致 {verification['consistent']}"
    )
    for fmt, item in verification["formats"].items():
        print(
            f"  {fmt}: 校验和一致 {item['checksum_match']}　"
            f"字段缺失 {item['fields_missing']}/{item['fields_total']}　"
            f"文本 {item['text_chars']} 字符"
        )
        if item["missing_fields"]:
            print(f"    缺失示例：{item['missing_fields']}")

    payload = {
        "session_id": session_id,
        "report_id": report_id,
        "context": context,
        "segments": [
            {
                "index": segment["index"],
                "title": segment["title"],
                "fields": len(segment["fields"]),
            }
            for segment in report["segments"]
        ],
        "exports": jobs,
        "verification": verification,
        "note": (
            "E6 口径：三种格式与页面由同一份 structure_json 渲染；判定 = 内容校验和一致 + "
            "每个字段的取值文本都能在三种格式的抽取文本里找到（PDF 用 pypdf 抽取内置 CID 字体）。"
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"结果已写入：{args.json}")
    return 0 if verification["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
