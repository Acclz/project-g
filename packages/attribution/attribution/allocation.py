"""维度贡献：优先精确，其次按占比分摊（技术规格 §5.5、需求说明书 §5.3）。

纯算法模块：无 IO、无框架依赖；所有浮点比较只走 ``conservation.assert_conservation``。
两条路径的边界写死在这里，避免两种典型误导：能精确却去分摊（偷懒）、用了分摊不标注（误导）。

* **精确**（默认）：按维度组合直接算该组合的差值 ``c(d) = Y_d(现期) − Y_d(基期)``。
  根指标对事实行可加（GMV = Σ 行金额、毛利额 = Σ 行盈亏），所以
  ``Σ_组合 c(d) = ΔY`` 是恒等式，标记 ``heuristic=False``。
* **降级**：仅当某组合缺数据（取值为 NULL，因子在该组合内无法定义）时，退化为按占比分摊差值，
  标记 ``heuristic=True``。它是**启发式、非唯一解**，报告与手册里必须如实说明。

一处口径澄清（如实记录，不含糊）：技术规格 §5.5 括号里写作
``c(d) = ΔY × 该切片占比变化 / Σ|占比变化|``，而"占比变化"是带符号、Σ ≡ 0 的量，
照字面实现会得到 ``Σc = 0 ≠ ΔY``，直接违反"贡献必须守恒"的红线。因此这里按需求说明书 §5.3 与
术语手册 T2 的表述（"按维度占比分摊差值"）实现为按**占比**分摊，分母取占比之和，守恒得以保住。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .conservation import DEFAULT_TOLERANCE, assert_conservation, conservation_residual


class AllocationError(ValueError):
    """维度贡献的前置条件不满足（维度为空、权重为负、取值非有限等）。"""


@dataclass(frozen=True)
class Contribution:
    """单个维度组合的贡献：键、两期取数、贡献额与贡献率。"""

    key: tuple[str, ...]
    contribution: float
    base: float
    current: float
    share_of_change: float | None
    rank: int
    in_top: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": list(self.key),
            "contribution": self.contribution,
            "base": self.base,
            "current": self.current,
            "contribution_rate": self.share_of_change,
            "rank": self.rank,
            "in_top": self.in_top,
            "note": self.note,
        }


@dataclass(frozen=True)
class ContributionTable:
    """一张维度透视表：按绝对贡献排序的贡献清单 + 覆盖率 + 守恒残差。"""

    dimensions: tuple[str, ...]
    contributions: tuple[Contribution, ...]
    total_base: float
    total_current: float
    total_delta: float
    top_n: int
    coverage: float
    tolerance: float
    residual: float
    relative_residual: float
    heuristic: bool
    notes: tuple[str, ...] = ()

    @property
    def top(self) -> tuple[Contribution, ...]:
        """TOP N 组合（按绝对贡献排序取前 N）。"""

        return tuple(item for item in self.contributions if item.in_top)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "rows": [item.as_dict() for item in self.contributions],
            "top_n": self.top_n,
            "coverage": self.coverage,
            "total_base": self.total_base,
            "total_current": self.total_current,
            "total_delta": self.total_delta,
            "residual": self.residual,
            "relative_residual": self.relative_residual,
            "heuristic": self.heuristic,
            "notes": list(self.notes),
        }

    def render(self, *, limit: int | None = None) -> str:
        """人话版透视表（报告第 3 段与讲解卡共用）。"""

        rows = self.contributions if limit is None else self.contributions[:limit]
        head = "／".join(self.dimensions)
        lines = [
            f"维度透视（{head}，共 {len(self.contributions)} 个组合，"
            f"TOP {self.top_n} 覆盖率 {self.coverage:.1%}"
            + ("，占比分摊口径：启发式、非唯一解" if self.heuristic else "，按组合精确计算")
            + "）"
        ]
        for item in rows:
            rate = "—" if item.share_of_change is None else f"{item.share_of_change:+.1%}"
            lines.append(
                f"  {item.rank:>2}. {'／'.join(item.key)}：{item.contribution:+,.0f} 分"
                f"（贡献率 {rate}）{' [TOP]' if item.in_top else ''}"
            )
            if item.note:
                lines.append(f"      注意：{item.note}")
        return "\n".join(lines)


def _validate_dimensions(
    dimensions: Sequence[str], *, max_dimensions: int | None
) -> tuple[str, ...]:
    names = tuple(str(item) for item in dimensions)
    if not names:
        raise AllocationError("维度贡献至少要指定一个维度")
    if len(set(names)) != len(names):
        raise AllocationError(f"维度列表有重复项：{list(names)}")
    if max_dimensions is not None and len(names) > max_dimensions:
        raise AllocationError(
            f"单次交叉最多 {max_dimensions} 个维度（需求说明书 §5.3），"
            f"收到 {len(names)} 个：{list(names)}"
        )
    return names


def _validate_key(key: Any, width: int) -> tuple[str, ...]:
    if isinstance(key, str):
        raise AllocationError(f"维度组合必须是序列而不是字符串：{key!r}")
    values = tuple(str(item) for item in key)
    if len(values) != width:
        raise AllocationError(f"维度组合 {values} 的长度与维度数 {width} 不一致")
    return values


def _check_finite(value: float, key: tuple[str, ...]) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise AllocationError(f"组合 {key} 的取值不是有限数：{value!r}")
    return number


def _clean_mapping(
    values: Mapping[tuple[str, ...], float], width: int, name: str
) -> dict[tuple[str, ...], float]:
    """统一校验：键是长度正确的维度组合，取值是有限数。"""

    cleaned: dict[tuple[str, ...], float] = {}
    for key, value in values.items():
        checked_key = _validate_key(key, width)
        if checked_key in cleaned:
            raise AllocationError(f"{name} 里出现重复的维度组合：{list(checked_key)}")
        cleaned[checked_key] = _check_finite(value, checked_key)
    return cleaned


def _build_table(
    *,
    dimensions: tuple[str, ...],
    contributions: dict[tuple[str, ...], float],
    base: dict[tuple[str, ...], float],
    current: dict[tuple[str, ...], float],
    notes: dict[tuple[str, ...], str],
    total_base: float,
    total_current: float,
    total_delta: float,
    top_n: int,
    tolerance: float,
    heuristic: bool,
    extra_notes: Sequence[str],
    expected_delta: float | None,
) -> ContributionTable:
    """排序、定 TOP N、算覆盖率，并过全项目统一的守恒断言（唯一比较入口）。"""

    if top_n < 1:
        raise AllocationError(f"TOP N 必须为正整数：{top_n}")
    values = list(contributions.values())
    residual = assert_conservation(values, total_delta, tolerance)
    # 交叉校验：透视表的总变动必须等于指标树在同一切片上给出的总变动（两个独立来源）
    if expected_delta is not None:
        assert_conservation(values, float(expected_delta), tolerance)
    _, relative = conservation_residual(values, total_delta)

    ordered = sorted(
        contributions.items(),
        key=lambda item: (-abs(item[1]), item[0]),
    )
    rows: list[Contribution] = []
    for index, (key, value) in enumerate(ordered, start=1):
        rate = None if total_delta == 0 else value / total_delta
        rows.append(
            Contribution(
                key=key,
                contribution=value,
                base=base.get(key, 0.0),
                current=current.get(key, 0.0),
                share_of_change=rate,
                rank=index,
                in_top=index <= top_n,
                note=notes.get(key, ""),
            )
        )
    magnitudes = sum(abs(item.contribution) for item in rows)
    top_magnitudes = sum(abs(item.contribution) for item in rows if item.in_top)
    coverage = 0.0 if magnitudes == 0 else top_magnitudes / magnitudes
    table_notes = list(extra_notes)
    if total_delta == 0:
        table_notes.append("总变动为 0，贡献率没有定义：只输出绝对贡献，不用 0 蒙混")
    return ContributionTable(
        dimensions=dimensions,
        contributions=tuple(rows),
        total_base=total_base,
        total_current=total_current,
        total_delta=total_delta,
        top_n=top_n,
        coverage=coverage,
        tolerance=tolerance,
        residual=residual,
        relative_residual=relative,
        heuristic=heuristic,
        notes=tuple(table_notes),
    )


def exact_contributions(
    base: Mapping[tuple[str, ...], float],
    current: Mapping[tuple[str, ...], float],
    *,
    dimensions: Sequence[str],
    top_n: int = 5,
    tolerance: float = DEFAULT_TOLERANCE,
    max_dimensions: int | None = 3,
    expected_delta: float | None = None,
) -> ContributionTable:
    """精确口径：按维度组合直接算差值。

    某一期没有出现的组合按 0 计（该组合当期无业务，基期取 0 是加法结构的正确读法），
    并在行上留一条 note 说明；因子在该组合内无法定义的情况应走 ``allocate_by_share``。
    """

    names = _validate_dimensions(dimensions, max_dimensions=max_dimensions)
    width = len(names)
    base_values = _clean_mapping(base, width, "base")
    current_values = _clean_mapping(current, width, "current")
    contributions: dict[tuple[str, ...], float] = {}
    notes: dict[tuple[str, ...], str] = {}
    for key in sorted(set(base_values) | set(current_values)):
        left = base_values.get(key)
        right = current_values.get(key)
        contributions[key] = (0.0 if right is None else right) - (0.0 if left is None else left)
        if left is None:
            notes[key] = "基期没有这个组合，基期按 0 计（新出现的组合）"
        elif right is None:
            notes[key] = "现期没有这个组合，现期按 0 计（已消失的组合）"
    return _build_table(
        dimensions=names,
        contributions=contributions,
        base=base_values,
        current=current_values,
        notes=notes,
        total_base=sum(base_values.values()),
        total_current=sum(current_values.values()),
        total_delta=sum(current_values.values()) - sum(base_values.values()),
        top_n=top_n,
        tolerance=tolerance,
        heuristic=False,
        extra_notes=(),
        expected_delta=expected_delta,
    )


def allocate_by_share(
    delta: float,
    shares: Mapping[tuple[str, ...], float],
    *,
    dimensions: Sequence[str],
    top_n: int = 5,
    tolerance: float = DEFAULT_TOLERANCE,
    max_dimensions: int | None = 3,
    note: str = "",
) -> ContributionTable:
    """降级口径：按占比分摊差值（启发式、非唯一解，``heuristic=True``）。

    ``shares`` 是各组合的占比（例如现期该组合占母指标的比重）。分摊结果
    ``c(d) = ΔY × share_d / Σ share_d`` 仍然严格闭合到 ΔY——分摊可以不准，但不可以不平。
    """

    names = _validate_dimensions(dimensions, max_dimensions=max_dimensions)
    width = len(names)
    weights = _clean_mapping(shares, width, "shares")
    negative = [list(key) for key, weight in weights.items() if weight < 0]
    if negative:
        raise AllocationError(f"占比不能为负：{negative}")
    if not weights:
        raise AllocationError("按占比分摊至少要有一个组合")
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise AllocationError("按占比分摊要求占比之和为正，收到的全是 0")
    amount = _check_finite(delta, ("ΔY",))
    contributions = {
        key: amount * weight / total_weight for key, weight in sorted(weights.items())
    }
    reason = note or "该组合缺数据或因子在此组合内无法定义，按占比分摊（启发式、非唯一解）"
    return _build_table(
        dimensions=names,
        contributions=contributions,
        base={},
        current={},
        notes={key: reason for key in contributions},
        total_base=0.0,
        total_current=0.0,
        total_delta=amount,
        top_n=top_n,
        tolerance=tolerance,
        heuristic=True,
        extra_notes=(reason,),
        expected_delta=None,
    )


__all__ = [
    "AllocationError",
    "Contribution",
    "ContributionTable",
    "allocate_by_share",
    "exact_contributions",
]
