"""口径参数加载与校验。

``corpus/warehouse/calibration.csv`` 是合成数仓全部数值参数的**唯一来源**：
凡有权威出处的参数，必须带来源名称、URL 与抓取日期；拿不到出处的，显式标注"自定参数"，
不允许把自定值伪装成行业事实（`docs/guide/04-产业口径与来源清单.md` §5）。

加载即校验，缺项或来源不全直接抛 ``CalibrationError``——不让错误参数流进数仓。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PATH = REPO_ROOT / "corpus" / "warehouse" / "calibration.csv"

SOURCE_TYPES = frozenset({"权威来源", "派生计算", "近似映射", "自定参数"})
# 需要"能指到出处"的来源类型（自定参数允许没有 URL，但必须写清为什么没有）
SOURCED_TYPES = frozenset({"权威来源", "派生计算", "近似映射"})
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CalibrationError(ValueError):
    """参数表本身有问题（缺项、来源不全、权重不闭合）。"""


@dataclass(frozen=True)
class CalibrationRow:
    """一行参数：值 + 单位 + 来源三件套（类型 / 名称 / URL / 抓取日期）+ 用途说明。"""

    param_key: str
    scope: str
    value: float
    unit: str
    source_type: str
    source_name: str
    source_url: str
    captured_on: str
    used_for: str
    note: str

    @property
    def is_self_defined(self) -> bool:
        """是否属于"自定参数"（报告里引用时必须显式说明是模拟值）。"""

        return self.source_type == "自定参数"


class Calibration:
    """参数集合：按 key 取值，并保留来源，便于报告与评测引用出处。"""

    def __init__(self, rows: list[CalibrationRow], path: Path) -> None:
        self._rows = {row.param_key: row for row in rows}
        self.path = path

    # ---------- 加载与校验 ----------

    @classmethod
    def load(cls, path: Path | None = None, *, validate: bool = True) -> Calibration:
        """读取 CSV；``validate=True`` 时执行全部校验并抛出第一组问题。"""

        target = Path(path) if path is not None else DEFAULT_PATH
        if not target.exists():
            raise CalibrationError(f"参数表不存在：{target}")
        rows: list[CalibrationRow] = []
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            for index, raw in enumerate(csv.DictReader(handle), start=2):
                if not raw.get("param_key"):
                    continue
                try:
                    value = float(raw["value"])
                except (TypeError, ValueError) as error:
                    raise CalibrationError(
                        f"{target.name} 第 {index} 行 value 不是数值：{raw.get('value')!r}"
                    ) from error
                rows.append(
                    CalibrationRow(
                        param_key=raw["param_key"].strip(),
                        scope=(raw.get("scope") or "").strip(),
                        value=value,
                        unit=(raw.get("unit") or "").strip(),
                        source_type=(raw.get("source_type") or "").strip(),
                        source_name=(raw.get("source_name") or "").strip(),
                        source_url=(raw.get("source_url") or "").strip(),
                        captured_on=(raw.get("captured_on") or "").strip(),
                        used_for=(raw.get("used_for") or "").strip(),
                        note=(raw.get("note") or "").strip(),
                    )
                )
        calibration = cls(rows, target)
        if validate:
            problems = calibration.validate()
            if problems:
                raise CalibrationError("参数表校验未通过：\n  - " + "\n  - ".join(problems))
        return calibration

    def validate(self) -> list[str]:
        """返回全部问题（空列表表示通过）。校验规则见模块 docstring。"""

        problems: list[str] = []
        for key, row in self._rows.items():
            if row.source_type not in SOURCE_TYPES:
                problems.append(f"{key}: source_type 非法（{row.source_type or '空'}）")
            if row.source_type in SOURCED_TYPES:
                if not row.source_name or not row.source_url:
                    problems.append(
                        f"{key}: {row.source_type} 必须同时给出 source_name 与 source_url"
                    )
                if not DATE_PATTERN.match(row.captured_on):
                    problems.append(f"{key}: 抓取日期格式应为 YYYY-MM-DD，实际 {row.captured_on!r}")
            if row.is_self_defined and "自定" not in row.source_name:
                # 自定值必须在来源名称里就写明"这是自定参数"，不允许含混成行业事实
                problems.append(f"{key}: 自定参数的 source_name 必须显式写明'自定参数'")
        problems.extend(self._weight_closure_problems())
        return problems

    def _weight_closure_problems(self, tolerance: float = 1e-6) -> list[str]:
        """结构权重必须闭合到 1（否则"渠道占比"本身就是错的）。"""

        problems: list[str] = []
        for prefix in (
            "channel_share.ecom",
            "channel_share.fmcg",
            "category_weight",
            "region_weight",
            "segment_weight",
        ):
            total = sum(
                row.value for key, row in self._rows.items() if key.startswith(f"{prefix}.")
            )
            if abs(total - 1.0) > tolerance:
                problems.append(f"{prefix}.* 权重合计为 {total:.6f}，应为 1.0")
        return problems

    # ---------- 取值 ----------

    def has(self, key: str) -> bool:
        return key in self._rows

    def row(self, key: str) -> CalibrationRow:
        try:
            return self._rows[key]
        except KeyError as error:
            raise CalibrationError(f"参数表缺少 {key}") from error

    def value(self, key: str) -> float:
        return self.row(key).value

    def scoped(self, prefix: str) -> dict[str, float]:
        """取一组同前缀参数，返回 ``{后缀: 值}``（如 ``channel_share.ecom`` → 各渠道占比）。"""

        head = f"{prefix}."
        found = {
            key[len(head) :]: row.value
            for key, row in self._rows.items()
            if key.startswith(head)
        }
        if not found:
            raise CalibrationError(f"参数表缺少以 {prefix}. 开头的参数")
        return found

    def require(self, keys: list[str]) -> None:
        """一次性列出缺失参数，避免"跑到一半才发现少一个"。"""

        missing = [key for key in keys if key not in self._rows]
        if missing:
            raise CalibrationError("参数表缺少必需项：" + ", ".join(missing))

    def sourced_rows(self) -> list[CalibrationRow]:
        """有权威出处的参数行（``docs/guide/04`` 升级为 v1.1 时按此清单核对）。"""

        return [row for row in self._rows.values() if row.source_type in SOURCED_TYPES]

    def self_defined_rows(self) -> list[CalibrationRow]:
        return [row for row in self._rows.values() if row.is_self_defined]

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows.values())
