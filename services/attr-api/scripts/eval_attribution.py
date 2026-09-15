"""E1 / E3 评测 CLI（薄壳）：真正的评测逻辑在 `app/services/eval_suite.py`。

为什么拆开：`scripts/` 不允许被服务代码 import（`02-流程与规范` §2.1），而接口层
`POST /api/eval/run` 也要跑同一套评测，所以逻辑必须落在用例层，脚本只负责打印与落盘。

用法：``python services/attr-api/scripts/eval_attribution.py --label p4_full``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
for extra in (SERVICE_ROOT, REPO_ROOT / "packages" / "attribution"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.services.eval_suite import run_attribution_eval, run_trap_eval  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E1/E3 评测")
    parser.add_argument("--label", default="local", help="本轮标签")
    return parser.parse_args()


def _percent(value: float | None) -> str:
    return "—" if value is None else format(value, ".1%")


def main() -> int:
    args = parse_args()
    attribution = run_attribution_eval()
    traps = run_trap_eval()
    metrics = attribution.metrics
    trap_metrics = traps.metrics

    print("E1 归因准确率（首轮基线）")
    print(
        f"  样本 {metrics['total']} 条　Top-1 {_percent(metrics['top1_rate'])}"
        f"　Top-3 {_percent(metrics['top3_rate'])}"
        f"　贡献额相对误差中位数 {_percent(metrics['median_relative_error'])}"
        f"　最大 {_percent(metrics['max_relative_error'])}"
    )
    for case in attribution.cases:
        print(
            f"    #{case['ground_truth_id']} 期望 {case['expected_factor']}"
            f"　Top-3 {case['ranking']}　相对误差 {_percent(case['relative_error'])}"
        )
    print("E3 伪相关陷阱集")
    print(
        f"  陷阱 {trap_metrics['total']} 条　被错误采纳 {trap_metrics['accepted']} 条"
        f"　误纳率 {_percent(trap_metrics['accept_rate'])}"
        f"（阈值 ≤{trap_metrics['threshold']:.0%}）"
        f"　带理由 {trap_metrics['cases_with_reason']}/{trap_metrics['total']}"
    )
    for case in traps.cases:
        print(
            f"    {case['id']}：{case['pseudo_verdict']}"
            f"　置信度 {case['final_confidence']:.2f}"
            f"　{'误纳' if case['accepted'] else '正确排除'}"
        )

    out_dir = SERVICE_ROOT / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        out_dir / f"attribution_eval_{args.label}.json",
        out_dir / f"trap_eval_{args.label}.json",
    ]
    for path, result in zip(paths, (attribution, traps), strict=True):
        path.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"结果已写入：{path}")
    return 0 if attribution.passed and traps.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
