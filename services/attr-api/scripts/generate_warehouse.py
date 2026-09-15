"""生成合成数仓（运维脚本，禁止被服务代码 import）。

用法：

    python services/attr-api/scripts/generate_warehouse.py --profile full --reset
    python services/attr-api/scripts/generate_warehouse.py --days 10 --profile tiny   # 冒烟

实测数字（行数、耗时）会被写进 ``services/attr-api/eval/warehouse_gen_<profile>.json``，
对外引用前必须能在完整档下重跑一次（`docs/02-流程与规范.md` §3）。
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

from app.config import get_settings  # noqa: E402
from app.warehouse.generator import WarehouseGenerator  # noqa: E402


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="生成合成数仓（dw.db + app.db）")
    parser.add_argument("--seed", type=int, default=settings.warehouse_seed, help="随机种子")
    parser.add_argument("--days", type=int, default=settings.warehouse_days, help="天数（18 个月 = 546）")
    parser.add_argument(
        "--profile", default="full", choices=["full", "small", "tiny"], help="档位标签，仅用于留档"
    )
    parser.add_argument("--reset", action="store_true", help="先生成前删除既有库文件（量重置耗时）")
    parser.add_argument("--stats-json", type=Path, default=None, help="实测结果落盘路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    generator = WarehouseGenerator(seed=args.seed, days=args.days, profile=args.profile)
    stats = generator.generate(reset=args.reset)

    payload = stats.as_dict()
    target = args.stats_json or SERVICE_ROOT / "eval" / f"warehouse_gen_{args.profile}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"数仓：{settings.warehouse_db}")
    print(f"业务库：{settings.app_db}")
    print(f"区间：{stats.start_day} ~ {stats.end_day}（{stats.days} 天，种子 {stats.seed}）")
    for table, rows in stats.rows.items():
        print(f"  {table:<18} {rows:>10,} 行")
    print(f"  事实表合计          {stats.total_rows:>10,} 行")
    print(f"  真值清单            {stats.ground_truth_rows:>10} 条")
    print(f"生成耗时：{stats.duration_s:.2f}s")
    if stats.reset_duration_s is not None:
        print(f"重置耗时：{stats.reset_duration_s:.2f}s")
    print(f"实测结果已写入：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
