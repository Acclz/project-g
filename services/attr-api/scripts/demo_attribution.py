"""端到端演示（P4）：异动 → 拆解 → 假设 → 沙箱验证 → 伪相关四步 → 事件 → 结论。

这是 L1 链路（`00-需求说明书` §8.1）的可执行版本。与 P3 的 `demo_decompose.py` 的区别：
P3 只跑到"分解 + 真值核对"，本脚本把**假设与验证**这一段也真跑一遍（假设文本、沙箱证据、
统计检验、置信度、排除理由、事件匹配、结论），并把结果落库到 `app.db` 的
`sessions / session_steps / hypotheses / evidence`（P5 的前端就从这些行渲染）。

用法：``python services/attr-api/scripts/demo_attribution.py [--json <路径>] [--no-persist]``
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
from app.services.analysis import AnalysisRequest, run_analysis  # noqa: E402
from app.services.decomposition import Period, SliceFilter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="L1 链路端到端演示")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "analysis_demo.json",
        help="演示结果落盘路径",
    )
    parser.add_argument("--no-persist", action="store_true", help="不写库（只看输出）")
    return parser.parse_args()


def ground_truth_rows() -> list[Any]:
    settings = get_settings()
    connection = connect_app(settings.app_db)
    try:
        return connection.execute("SELECT * FROM ground_truth ORDER BY scenario, id").fetchall()
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    payloads: list[dict[str, Any]] = []
    for row in ground_truth_rows():
        current = Period(row["day"], row["window_end"], label="注入窗口")
        request = AnalysisRequest(
            scenario=row["scenario"],
            base=current.previous(),
            current=current,
            slice_filter=SliceFilter.from_json(row["dimension_json"]),
            title=f"真因 #{row['id']}（{row['scenario']}）",
            actor="demo",
            persist=not args.no_persist,
        )
        print("=" * 78)
        print(f"真因 #{row['id']}｜{row['note']}")
        report = run_analysis(request)
        print(report.render())
        print(
            f"\n落库：session_id={report.session_id}　步骤 {len(report.steps)} 条"
            f"　耗时 {report.duration_ms} ms"
        )
        if report.fallback_note:
            print(f"假设来源说明：{report.fallback_note}")
        payload = report.as_dict()
        payload["ground_truth"] = {
            "id": row["id"],
            "factor": row["factor"],
            "injected_contribution_cents": row["injected_contribution_cents"],
        }
        payloads.append(payload)

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps({"cases": payloads, "total": len(payloads)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=" * 78)
    print(f"合计 {len(payloads)} 条链路跑完，结果已写入：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
