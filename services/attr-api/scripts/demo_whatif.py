"""端到端演示（P5·L3 前半）：What-If 推演——弹性区间、参数曲线、前提条件与把握度。

这是需求说明书 §5.8（What-If 策略推演规则）与 §8.3 第 1~2 步的可执行版本：

1. **白名单**：可干预因子来自指标字典；不可干预因子（宏观、天气、竞品动作）必须被拒绝；
2. **单点弹性**：每个可估因子给出弹性点估计 + bootstrap 区间 + 样本量 + r² + 把握度；
3. **参数曲线**：±30% 档位扫描，输出区间而不是承诺值；超出范围必须显式标注外推；
4. **前提条件**：单因子局部均衡、代理口径、窗口、可复现种子，逐条写出来。

用法：``python services/attr-api/scripts/demo_whatif.py [--json <路径>] [--breakdown]``
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
from app.services.whatif import (  # noqa: E402
    WhatIfError,
    WhatIfRequest,
    intervenable_factors,
    run_whatif,
)

#: 演示用真因窗口（与 ground_truth 一致，便于与其他脚本对账）
CASES = {
    "ecom": {"day": "2026-06-05", "window_end": "2026-06-11"},
    "fmcg": {"day": "2026-04-06", "window_end": "2026-04-19"},
}

#: 不可干预因子的示范（必须被拒绝，这就是"不许编数"的现场证据）
REJECTED_FACTORS = {
    "ecom": ["cvr", "competitor_price", "weather"],
    "fmcg": ["raw_material_cost", "macro_ppi"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="L3 What-If 推演演示")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "whatif_demo.json",
        help="演示结果落盘路径",
    )
    parser.add_argument("--breakdown", action="store_true", help="额外按渠道分别估弹性")
    return parser.parse_args()


def ground_truth_slice(scenario: str) -> tuple[str, list[str]]:
    """取该场景第一条真值的切片作为推演上下文（有真值背书，便于讲解）。"""

    connection = connect_app(get_settings().app_db)
    try:
        row = connection.execute(
            "SELECT dimension_json FROM ground_truth WHERE scenario = ? ORDER BY id LIMIT 1",
            (scenario,),
        ).fetchone()
    finally:
        connection.close()
    raw = json.loads(row["dimension_json"]) if row else {}
    if len(raw) == 1:
        dimension, values = next(iter(raw.items()))
        return dimension, list(values)
    return "", []


def main() -> int:
    args = parse_args()
    engine = MetricEngine(get_settings())
    cases: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for scenario, window in CASES.items():
        current = Period(window["day"], window["window_end"], label="真因窗口")
        base = current.previous()
        dimension, values = ground_truth_slice(scenario)
        slice_filter = SliceFilter({dimension: tuple(values)}) if dimension else SliceFilter()
        print("=" * 78)
        print(f"【{scenario}】窗口 {current.start}~{current.end}　切片 {values or '全公司'}")
        factors = intervenable_factors(engine, scenario)
        print(
            "可干预因子："
            + "、".join(
                f"{item['code']}{'' if item['estimable'] else '（不可估）'}" for item in factors
            )
        )

        for factor in REJECTED_FACTORS[scenario]:
            try:
                run_whatif(
                    WhatIfRequest(
                        scenario=scenario,
                        base=base,
                        current=current,
                        factor=factor,
                        slice_filter=slice_filter,
                    ),
                    engine=engine,
                )
            except WhatIfError as error:
                rejected.append({"scenario": scenario, "factor": factor, "reason": str(error)})
                print(f"  拒绝推演 {factor}：{error}")
            else:
                print(f"  ！！{factor} 本应被拒绝，却跑出了曲线——这是缺陷，不是特性")
                return 1

        for item in factors:
            code = item["code"]
            if not item["estimable"]:
                print(f"  {code}：不可估——{item['reject_reason']}")
                continue
            request = WhatIfRequest(
                scenario=scenario,
                base=base,
                current=current,
                factor=code,
                slice_filter=slice_filter,
                dimensions=(("channel",),) if args.breakdown else (),
            )
            report = run_whatif(request, engine=engine)
            print()
            print(report.render())
            cases.append({"scenario": scenario, "factor": code, "report": report.as_dict()})

    payload = {
        "cases": cases,
        "rejected": rejected,
        "total": len(cases),
        "rejected_total": len(rejected),
        "all_with_ranges": all(
            all(
                point["low"] <= point["expected"] <= point["high"]
                for point in case["report"]["curve"]["points"]
            )
            for case in cases
        ),
        "note": (
            "口径说明：弹性由目标指标与因子代理的日序列估计（对数—对数回归；样本不足降级为有限差分），"
            "区间为 bootstrap 百分位区间；所有金额单位为分（代理口径除外，件单价/佣金率为无量纲或元）。"
            "弹性是历史共动，不是因果保证。"
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("=" * 78)
    print(
        f"合计 {payload['total']} 个可估因子跑出区间曲线，"
        f"拒绝 {payload['rejected_total']} 个不可干预/不可估因子，"
        f"所有档位区间包住点估计 {payload['all_with_ranges']}"
    )
    print(f"结果已写入：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
