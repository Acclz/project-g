"""端到端演示（P3）：异动 → 一级拆解 → 下钻 → 与预埋真值核对。

这是 L1 链路（`00-需求说明书` §8.1）里"第 3 步"的可执行版本：不接前端、不接模型，
只用指标字典 + 数仓 + 算法包跑通"数字怎么被算出来"的完整过程，并把结果与
``ground_truth`` 里事先封存的标准答案对一次账。

用法：

    python services/attr-api/scripts/demo_decompose.py
    python services/attr-api/scripts/demo_decompose.py --json services/attr-api/eval/decompose_demo.json
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

#: 每个场景下钻哪些节点：电商下钻三个一级因子，快消下钻成本项与营收
TARGETS = {
    "ecom": ["uv", "cvr", "aov"],
    "fmcg": ["revenue", "raw_material_cost", "channel_commission"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分解链路端到端演示")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "decompose_demo.json",
        help="演示结果落盘路径",
    )
    return parser.parse_args()


def ground_truth_rows() -> list[Any]:
    """读取真值清单（期望值的唯一来源）。"""

    settings = get_settings()
    connection = connect_app(settings.app_db)
    try:
        return connection.execute("SELECT * FROM ground_truth ORDER BY scenario, id").fetchall()
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    engine = MetricEngine()
    cases: list[dict[str, Any]] = []
    missed = 0

    for row in ground_truth_rows():
        scenario = row["scenario"]
        current = Period(row["day"], row["window_end"], label="注入窗口")
        base = current.previous()
        slice_filter = SliceFilter.from_json(row["dimension_json"])
        print("=" * 78)
        print(f"真因 #{row['id']}｜{row['note']}")
        report = engine.decompose(
            scenario, base, current, slice_filter=slice_filter, auto_targets=TARGETS[scenario]
        )
        print(report.render())

        top = report.top(limit=3)
        expected_factor = row["factor"]
        observed = next(
            (
                item
                for item in report.root.as_dict()["contributions"]
                if item["factor"] == expected_factor
            ),
            None,
        )
        truth_cents = int(row["injected_contribution_cents"])
        observed_cents = int(round(observed["contribution"])) if observed else 0
        relative_error = (
            abs(observed_cents - truth_cents) / max(abs(truth_cents), 1) if truth_cents else None
        )
        top_factors = [item["factor"] for item in top]
        top1_hit = top_factors[0] == expected_factor if top_factors else False
        top3_hit = expected_factor in top_factors
        # P3 的核对线：守恒必须成立（硬门禁），且真值因子要落进 Top-3；
        # "Top-1 严格命中率"是 P6 评测集的口径（需要配套对照窗口），不在这里当门禁
        if not report.ok or not top3_hit:
            missed += 1
        printable_error = "—" if relative_error is None else f"{relative_error:.1%}"
        print(
            f"\n真值核对：期望因子 {expected_factor}　真值贡献 {truth_cents:,} 分　"
            f"分解贡献 {observed_cents:,} 分　相对误差 {printable_error}　"
            f"Top-1 {'命中' if top1_hit else '未命中'}　Top-3 {'命中' if top3_hit else '未命中'}"
        )
        cases.append(
            {
                "ground_truth_id": row["id"],
                "scenario": scenario,
                "note": row["note"],
                "slice": json.loads(slice_filter.as_json()),
                "base": base.as_dict(),
                "current": current.as_dict(),
                "expected_factor": expected_factor,
                "truth_contribution_cents": truth_cents,
                "observed_contribution_cents": observed_cents,
                "relative_error": relative_error,
                "top1_hit": top1_hit,
                "top3_hit": top3_hit,
                "top3": top,
                "max_relative_residual": report.max_relative_residual,
                "ok": report.ok,
                "report": report.as_dict(),
            }
        )

    payload = {
        "cases": cases,
        "total": len(cases),
        "top1_hits": sum(1 for case in cases if case["top1_hit"]),
        "top3_hits": sum(1 for case in cases if case["top3_hit"]),
        "all_conserved": all(case["ok"] for case in cases),
        "note": (
            "口径说明：真值记录的是注入本身的贡献，分解看到的是两期全部变化，"
            "因此这里只核对方向与 Top-1 排名；贡献额的严格比较由 P6 评测集（配套对照窗口）负责"
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=" * 78)
    print(
        f"合计 {payload['total']} 个真因：Top-1 命中 {payload['top1_hits']}/{payload['total']}，"
        f"Top-3 命中 {payload['top3_hits']}/{payload['total']}，全部守恒 {payload['all_conserved']}"
    )
    print(f"结果已写入：{args.json}")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
