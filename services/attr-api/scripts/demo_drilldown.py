"""端到端演示（P5·L2）：异动定位 → 维度透视 → 收紧切片再透视 → 逐层守恒。

这是需求说明书 §8.2（L2 下钻链路）的可执行版本，也是"切片只能收紧"这条规则的现场证据：

1. **定位**：在注入窗口上按真值维度做全公司透视（精确计算），看真因切片排到第几名；
2. **收紧**：以真值切片为锁定上下文，再往下一层透视（例如渠道内看品类），
   校验"收紧后仍然守恒"（Σ组合贡献 = 收紧后的层总变动 = 指标树在同一切片上的总变动）；
3. **会话层**：走真正的会话接口路径（``SessionService``）——创建会话锁定上下文 →
   ``drilldown`` → 后台跑 L2 → 落库 → 读回下钻记录；顺带验证放宽切片会被拒。

用法：``python services/attr-api/scripts/demo_drilldown.py [--json <路径>] [--skip-session]``
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
from app.services.sessions import ContextLocked, SessionService  # noqa: E402

#: 真值切片的排序门禁：真因组合必须落在透视表 TOP N 内（默认为 5，与需求说明书 §5.3 一致）
TOP_N = 5

#: 用来验证"放宽切片会被拒"的备选取值（必须与锁定值不同，否则属于合法收紧）
OTHER_VALUES: dict[str, tuple[str, ...]] = {
    "channel": ("natural_search", "livestream", "hypermarket", "catering"),
    "category": ("appliance", "beverage", "snack", "beauty"),
    "region": ("north", "south", "east"),
    "segment": ("new", "high_value"),
    "sku": ("SKU-0001", "SKU-0002"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="L2 下钻链路端到端演示")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "drilldown_demo.json",
        help="演示结果落盘路径",
    )
    parser.add_argument("--skip-session", action="store_true", help="跳会话层那一段（只看算法）")
    return parser.parse_args()


def ground_truth_rows() -> list[Any]:
    settings = get_settings()
    connection = connect_app(settings.app_db)
    try:
        return connection.execute("SELECT * FROM ground_truth ORDER BY scenario, id").fetchall()
    finally:
        connection.close()


def _truth_dimension(row: Any) -> tuple[str, list[str]]:
    raw = json.loads(row["dimension_json"])
    if len(raw) != 1:
        raise SystemExit(f"真值 #{row['id']} 的切片不是单维度，演示脚本不知道怎么定位：{raw}")
    dimension, values = next(iter(raw.items()))
    return dimension, list(values)


def locate_case(engine: MetricEngine, row: Any) -> dict[str, Any]:
    """第一段：按真值维度在全公司范围内透视，看真因切片排第几。"""

    scenario = row["scenario"]
    current = Period(row["day"], row["window_end"], label="注入窗口")
    base = current.previous()
    dimension, values = _truth_dimension(row)
    pivot = engine.dimension_table(
        scenario, base, current, dimensions=(dimension,), slice_filter=SliceFilter(), top_n=TOP_N
    )
    ranks = {
        "／".join(item.key): item.rank
        for item in pivot.table.contributions
        if item.key[0] in values
    }
    contributions = {
        "／".join(item.key): item.contribution
        for item in pivot.table.contributions
        if item.key[0] in values
    }
    truth_cents = int(row["injected_contribution_cents"])
    direction_ok = all(value < 0 for value in contributions.values()) == (truth_cents < 0)
    best_rank = min(ranks.values()) if ranks else None
    return {
        "phase": "locate",
        "scenario": scenario,
        "base": base.as_dict(),
        "current": current.as_dict(),
        "dimension": dimension,
        "truth_values": values,
        "truth_contribution_cents": truth_cents,
        "truth_ranks": ranks,
        "truth_contributions": contributions,
        "best_rank": best_rank,
        "top_n": TOP_N,
        "all_in_top_n": bool(ranks) and all(rank <= TOP_N for rank in ranks.values()),
        "direction_ok": direction_ok,
        "coverage": pivot.table.coverage,
        "conserved": pivot.table.relative_residual < pivot.table.tolerance,
        "layer_delta": pivot.layer_delta,
        "residual": pivot.table.residual,
        "rows": pivot.table.as_dict()["rows"],
        "render": pivot.render(limit=TOP_N),
    }


def narrow_case(engine: MetricEngine, row: Any) -> dict[str, Any]:
    """第二段：锁定真值切片后再往下一层透视，检验"收紧不失真"。"""

    scenario = row["scenario"]
    current = Period(row["day"], row["window_end"], label="注入窗口")
    base = current.previous()
    dimension, values = _truth_dimension(row)
    locked = SliceFilter({dimension: tuple(values)})
    plan = default_dimensions(engine, scenario, locked)
    inner = plan[0]
    pivot = engine.dimension_table(
        scenario, base, current, dimensions=inner, slice_filter=locked, top_n=TOP_N
    )
    return {
        "phase": "narrow",
        "locked_slice": json.loads(locked.as_json()),
        "dimensions": list(inner),
        "coverage": pivot.table.coverage,
        "conserved": pivot.table.relative_residual < pivot.table.tolerance,
        "layer_delta": pivot.layer_delta,
        "residual": pivot.table.residual,
        "rows": pivot.table.as_dict()["rows"],
        "render": pivot.render(limit=TOP_N),
    }


def session_case(service: SessionService, row: Any) -> dict[str, Any]:
    """第三段：走会话层真实路径（创建 → 下钻 → 落库 → 读回），并验证放宽会被拒。"""

    scenario = row["scenario"]
    current = Period(row["day"], row["window_end"], label="注入窗口")
    base = current.previous()
    dimension, values = _truth_dimension(row)
    state = service.create(
        scenario=scenario,
        base=base,
        current=current,
        slice_filter=SliceFilter({dimension: tuple(values)}),
        title=f"演示·下钻 #{row['id']}",
        actor="demo",
    )
    plan = default_dimensions(service.engine, scenario, state.slice_filter)
    accepted = service.drilldown(
        state.id, dimensions=plan[:1], top_n=TOP_N, actor="demo", message="再往下看一层"
    )
    done = service.wait(state.id, timeout=900)
    records = service.drilldown_records(state.id)
    summary = [item for item in records if item["payload"].get("summary")]
    widened_blocked = False
    try:
        other = next(
            (item for item in OTHER_VALUES.get(dimension, ()) if item not in values),
            None,
        )
        if other is None:
            raise SystemExit(f"维度 {dimension} 没有可用的『放宽』备选取值，无法验证拦截")
        service.drilldown(
            state.id,
            extra_slice=SliceFilter({dimension: (other,)}),
            dimensions=(("region",),),
        )
    except ContextLocked:
        widened_blocked = True
    return {
        "phase": "session",
        "session_id": state.id,
        "locked_slice": json.loads(state.slice_filter.as_json()),
        "drilldown_slice": json.loads(accepted.slice_filter.as_json()),
        "status": done.status,
        "steps": len(done.steps),
        "records": len(records),
        "summary_conserved": bool(summary and summary[-1]["payload"].get("conserved")),
        "coverage": summary[-1]["payload"].get("coverage") if summary else None,
        "widened_blocked": widened_blocked,
        "highlights": summary[-1]["payload"].get("highlights") if summary else [],
    }


def main() -> int:
    args = parse_args()
    settings = get_settings()
    engine = MetricEngine(settings)
    service = (
        None if args.skip_session else SessionService(settings, engine=engine)
    )
    cases: list[dict[str, Any]] = []
    for row in ground_truth_rows():
        print("=" * 78)
        print(f"真因 #{row['id']}｜{row['note']}")
        located = locate_case(engine, row)
        print(located["render"])
        print(
            f"\n定位：真因切片 {located['truth_values']} 排名 {located['truth_ranks']}"
            f"（最佳 {located['best_rank']}，TOP {TOP_N} 内 {located['all_in_top_n']}）"
            f"　方向一致 {located['direction_ok']}　覆盖率 {located['coverage']:.1%}"
        )
        narrowed = narrow_case(engine, row)
        print(f"\n收紧后（{'／'.join(narrowed['dimensions'])}）：")
        print(narrowed["render"])
        case = {
            "ground_truth_id": row["id"],
            "scenario": row["scenario"],
            "note": row["note"],
            "locate": located,
            "narrow": narrowed,
        }
        if service is not None:
            session = session_case(service, row)
            case["session"] = session
            print(
                f"\n会话路径：session_id={session['session_id']}　状态 {session['status']}"
                f"　下钻记录 {session['records']} 条　收紧守恒 {session['summary_conserved']}"
                f"　放宽被拒 {session['widened_blocked']}"
            )
        cases.append(case)

    payload = {
        "cases": cases,
        "total": len(cases),
        "all_conserved": all(
            case["locate"]["conserved"] and case["narrow"]["conserved"] for case in cases
        ),
        "all_direction_ok": all(case["locate"]["direction_ok"] for case in cases),
        "all_in_top_n": all(case["locate"]["all_in_top_n"] for case in cases),
        "sessions": {
            "count": sum(1 for case in cases if "session" in case),
            "all_conserved": all(
                case["session"]["summary_conserved"]
                for case in cases
                if "session" in case
            ),
            "all_widen_blocked": all(
                case["session"]["widened_blocked"] for case in cases if "session" in case
            ),
        },
        "note": (
            "口径说明：透视表的贡献是该维度组合在指标上的完整两期变化（含季节与其他因子），"
            "真值记录的是注入本身的贡献，因此这里只核对排名与方向，不比较金额；"
            "金额级别的严格比对由 P6 评测集负责。所有金额单位为分。"
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("=" * 78)
    print(
        f"合计 {payload['total']} 条真因：全部守恒 {payload['all_conserved']}，"
        f"方向一致 {payload['all_direction_ok']}，真因切片落 TOP {TOP_N} 内 "
        f"{payload['all_in_top_n']}"
    )
    print(f"结果已写入：{args.json}")
    return 0 if payload["all_conserved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
