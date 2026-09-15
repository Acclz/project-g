"""维度主数据测试：规模、层级与日历口径。"""

from __future__ import annotations

from datetime import date

from app.warehouse import dimensions as dims
from app.warehouse.calibration import Calibration


def test_dimension_scale_matches_spec() -> None:
    """技术规格 §3.1：渠道 8、品类 12、区域 7、客群 5、SKU 40。"""

    calibration = Calibration.load()
    skus = dims.build_sku_master(calibration)
    assert (len(dims.CHANNELS), len(dims.CATEGORIES), len(dims.REGIONS), len(dims.SEGMENTS)) == (
        8,
        12,
        7,
        5,
    )
    assert len(skus) == 40
    assert len({row[0] for row in skus}) == 40
    assert len({row[1] for row in skus}) == 40


def test_categories_are_two_level_and_skus_reference_them() -> None:
    calibration = Calibration.load()
    parents = {category.parent_code for category in dims.CATEGORIES}
    assert len(parents) == 3, "12 个品类应归到 3 个父类下"
    category_ids = {category.category_id for category in dims.CATEGORIES}
    for sku in dims.build_sku_master(calibration):
        assert sku[3] in category_ids
        assert sku[4] < sku[5], "单位成本必须低于标准售价"


def test_promo_calendar_flags_known_dates() -> None:
    calibration = Calibration.load()
    assert dims.promo_by_day(date(2026, 6, 17), calibration)[:2] == (1, "6·18")
    assert dims.promo_by_day(date(2026, 11, 11), calibration)[0] == 1
    assert dims.promo_by_day(date(2026, 6, 1), calibration) == (0, None, 1.0)
    assert dims.promo_by_day(date(2026, 6, 17), calibration)[2] > 1.0


def test_season_factor_lookup() -> None:
    calibration = Calibration.load()
    assert dims.season_factor(date(2026, 11, 5), calibration) == calibration.value(
        "season_factor.m11"
    )
