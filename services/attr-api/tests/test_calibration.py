"""参数表校验测试：来源必须可指认，权重必须闭合，缺项必须报错。"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from app.warehouse.calibration import DEFAULT_PATH, Calibration, CalibrationError

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_calibration_loads_and_covers_required_params() -> None:
    calibration = Calibration.load()
    required = ["ecom.ctr_base", "fmcg.units_base", "season_factor.m06", "promo_lift.double11"]
    calibration.require(required)
    assert len(calibration) > 100, "参数表应覆盖维度权重、增速与场景漏斗"


def test_sourced_params_have_url_and_capture_date() -> None:
    """凡标了权威来源/派生计算/近似映射的参数，都必须能指到 URL 与抓取日期。"""

    calibration = Calibration.load()
    sourced = calibration.sourced_rows()
    assert sourced, "至少要有若干权威来源参数，否则合成的'行业感'无从谈起"
    for row in sourced:
        assert row.source_url.startswith("http"), f"{row.param_key} 缺少来源 URL"
        assert DATE_PATTERN.match(row.captured_on), f"{row.param_key} 抓取日期格式不对"
        assert row.source_name, f"{row.param_key} 缺少来源名称"


def test_self_defined_params_explain_themselves() -> None:
    """自定参数必须在来源名称里就标明"自定"，防止被当成行业事实引用。"""

    calibration = Calibration.load()
    self_defined = calibration.self_defined_rows()
    assert len(self_defined) > 50
    for row in self_defined:
        assert "自定" in row.source_name, f"{row.param_key} 没有显式标注为自定参数"


def test_weight_groups_close_to_one() -> None:
    calibration = Calibration.load()
    for prefix in ("channel_share.ecom", "channel_share.fmcg", "category_weight", "segment_weight"):
        total = sum(calibration.scoped(prefix).values())
        assert abs(total - 1.0) < 1e-6, f"{prefix} 权重合计 {total}"


def test_missing_key_raises() -> None:
    calibration = Calibration.load()
    with pytest.raises(CalibrationError):
        calibration.value("ecom.not_exist")


def test_invalid_source_type_is_rejected(tmp_path: Path) -> None:
    """来源类型写错必须当场失败——否则"来源"这一列就成了装饰。"""

    broken = tmp_path / "calibration.csv"
    with DEFAULT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys())
    rows[0]["source_type"] = "大概算权威"
    with broken.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(CalibrationError):
        Calibration.load(broken)
