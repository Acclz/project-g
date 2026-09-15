"""性能基准（技术规格 §10）：固定数据集 + 固定查询集，每项重复 N 次，输出 P50/P95/最大值与错误率。

规矩（`02-流程与规范` §3）：

* 每份结果必须带**命令、日期、机器规格、数据行数、并发数**；
* 对外只引用完整档（= 完整数仓）下的数字；
* 低于基准就写"未达标"，不写"接近达标"。

用法：

    python services/attr-api/scripts/bench.py --label p3_full --repeat 50
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
for extra in (SERVICE_ROOT, REPO_ROOT / "packages" / "attribution"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import psutil  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import connect_warehouse_readonly  # noqa: E402
from app.sandbox.runner import SandboxRunner  # noqa: E402
from app.services.decomposition import MetricEngine, Period  # noqa: E402

#: 需求说明书 §12 的基准（秒）；未列出的项本阶段不测，如实标注
THRESHOLDS_SECONDS = {
    "dashboard_day": 2.0,
    "decompose_level1": 3.0,
    "sandbox_probe": 5.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合成数仓与分解链路性能基准")
    parser.add_argument("--label", default="local", help="本轮标签（写进文件名）")
    parser.add_argument("--repeat", type=int, default=50, help="每项重复次数")
    return parser.parse_args()


def build_cases(engine: MetricEngine) -> list[tuple[str, Callable[[], Any], float | None]]:
    """固定查询集：每项都是"业务上真会发的一问"，不是为跑分编的语句。"""

    settings = get_settings()
    runner = SandboxRunner(settings)
    current = Period("2026-06-18", "2026-06-24")
    base = current.previous()
    fmcg_current = Period("2026-04-06", "2026-04-12")
    fmcg_base = fmcg_current.previous()

    def dashboard_day() -> Any:
        connection = connect_warehouse_readonly(settings.warehouse_db)
        try:
            return connection.execute(
                "SELECT SUM(gmv_cents), SUM(visitors), SUM(orders_paid)"
                " FROM dw.fact_ecom_daily WHERE day = ?",
                ("2026-06-20",),
            ).fetchone()
        finally:
            connection.close()

    def dashboard_baseline() -> Any:
        connection = connect_warehouse_readonly(settings.warehouse_db)
        try:
            return connection.execute(
                "SELECT day, SUM(gmv_cents) FROM dw.fact_ecom_daily"
                " WHERE day BETWEEN ? AND ? GROUP BY day",
                (base.start, current.end),
            ).fetchall()
        finally:
            connection.close()

    def decompose_level1() -> Any:
        return engine.decompose("ecom", base, current)

    def decompose_mixed() -> Any:
        return engine.decompose(
            "fmcg",
            fmcg_base,
            fmcg_current,
            auto_targets=["revenue", "raw_material_cost", "channel_commission"],
        )

    def drilldown_orders() -> Any:
        connection = connect_warehouse_readonly(settings.warehouse_db)
        try:
            return connection.execute(
                "SELECT channel_id, COUNT(*) AS orders, SUM(amount_cents) AS amount"
                " FROM dw.fact_order WHERE day BETWEEN ? AND ? GROUP BY channel_id",
                (base.start, current.end),
            ).fetchall()
        finally:
            connection.close()

    def sandbox_probe() -> Any:
        return runner.run_sql(
            "SELECT channel_id, SUM(gmv_cents) AS gmv FROM dw.fact_ecom_daily"
            " WHERE day BETWEEN '2026-06-18' AND '2026-06-24' GROUP BY channel_id",
            actor="bench",
        )

    return [
        ("dashboard_day", dashboard_day, THRESHOLDS_SECONDS["dashboard_day"]),
        ("dashboard_baseline_28d", dashboard_baseline, None),
        ("decompose_level1", decompose_level1, THRESHOLDS_SECONDS["decompose_level1"]),
        ("decompose_mixed_fmcg", decompose_mixed, None),
        ("drilldown_orders", drilldown_orders, None),
        ("sandbox_probe", sandbox_probe, THRESHOLDS_SECONDS["sandbox_probe"]),
    ]


def row_counts() -> dict[str, int]:
    """数据行数（§10 要求每次测量都要记录）。"""

    settings = get_settings()
    connection = connect_warehouse_readonly(settings.warehouse_db)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM dw.{table}").fetchone()[0])
            for table in ("fact_ecom_daily", "fact_order", "fact_fmcg_daily")
        }
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    engine = MetricEngine()
    counts = row_counts()
    results: list[dict[str, Any]] = []
    print(f"数据行数：{counts}")

    for name, func, threshold in build_cases(engine):
        func()  # 预热一次（含子进程启动与 SQLite 页缓存冷启动）
        durations: list[float] = []
        errors = 0
        for _ in range(args.repeat):
            started = time.perf_counter()
            try:
                func()
            except Exception as error:  # noqa: BLE001 - 基准要统计错误率，而不是自己崩掉
                errors += 1
                print(f"   [{name}] 失败：{type(error).__name__}: {error}")
            durations.append((time.perf_counter() - started) * 1000)
        ordered = sorted(durations)
        record = {
            "name": name,
            "repeat": args.repeat,
            "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
            "max_ms": round(max(ordered), 2),
            "min_ms": round(min(ordered), 2),
            "error_rate": round(errors / args.repeat, 4),
            "threshold_ms": None if threshold is None else int(threshold * 1000),
            "meets_threshold": None if threshold is None else record_meets(ordered, threshold),
        }
        results.append(record)
        verdict = "—" if record["meets_threshold"] is None else (
            "达标" if record["meets_threshold"] else "未达标"
        )
        print(
            f"{name:<24} P50={record['p50_ms']:>8.1f}ms  P95={record['p95_ms']:>8.1f}ms"
            f"  max={record['max_ms']:>8.1f}ms  错误率 {record['error_rate']:.0%}  {verdict}"
        )

    payload = {
        "label": args.label,
        "command": (
            "python services/attr-api/scripts/bench.py"
            f" --label {args.label} --repeat {args.repeat}"
        ),
        "date": datetime.now().isoformat(timespec="seconds"),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_logical": psutil.cpu_count(logical=True),
            "cpu_physical": psutil.cpu_count(logical=False),
            "memory_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        },
        "data_rows": counts,
        "concurrency": 1,
        "repeat": args.repeat,
        "results": results,
        "not_measured": [
            "端到端完整归因（含 LLM，P4 起）",
            "报告三格式导出（P5）",
            "10 并发劣化（P6）",
        ],
    }
    target = SERVICE_ROOT / "eval" / f"bench_{args.label}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"结果已写入：{target}")
    return 0


def record_meets(ordered: list[float], threshold_seconds: float) -> bool:
    """P95 是否满足 §12 的基准。"""

    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    return p95 <= threshold_seconds * 1000


if __name__ == "__main__":
    raise SystemExit(main())
