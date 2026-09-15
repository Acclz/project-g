"""维度表与 SKU 主数据。

这里只放**没有数值来源的静态数据**：编码、名称、层级、渠道属性。
凡是需要"行业依据"的数值（权重、增速、价格水平、毛利率、漏斗基准）一律放
``corpus/warehouse/calibration.csv``，由 ``calibration.py`` 加载并校验。

维度规模与技术规格 §3.1 一致：渠道 8 × 品类 12 × 区域 7 × 客群 5，快消 SKU 40。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.warehouse.calibration import Calibration


@dataclass(frozen=True)
class Channel:
    """渠道维度：线上/线下与是否付费，决定佣金口径与年增速取哪条来源。"""

    channel_id: int
    code: str
    name: str
    is_paid: int
    channel_type: str  # online / offline
    commission_scope: str  # online / offline（决定用哪个佣金率）


CHANNELS: tuple[Channel, ...] = (
    Channel(1, "natural_search", "自然搜索", 0, "online", "online"),
    Channel(2, "paid_ads", "付费投放", 1, "online", "online"),
    Channel(3, "private_domain", "私域", 0, "online", "online"),
    Channel(4, "livestream", "直播", 1, "online", "online"),
    Channel(5, "short_video", "短视频", 1, "online", "online"),
    Channel(6, "hypermarket", "商超", 0, "offline", "offline"),
    Channel(7, "distributor", "经销商", 0, "offline", "offline"),
    Channel(8, "catering", "餐饮", 0, "offline", "offline"),
)


@dataclass(frozen=True)
class Category:
    """品类维度：两级结构（父类 / 子类），子类对应一套售价与成本参数。"""

    category_id: int
    code: str
    name: str
    parent_code: str
    parent_name: str


CATEGORIES: tuple[Category, ...] = (
    Category(1, "grain_oil", "粮油食品", "food_beverage", "食品饮料"),
    Category(2, "beverage", "饮料", "food_beverage", "食品饮料"),
    Category(3, "tobacco_liquor", "烟酒", "food_beverage", "食品饮料"),
    Category(4, "snack", "休闲食品", "food_beverage", "食品饮料"),
    Category(5, "dairy", "乳制品", "food_beverage", "食品饮料"),
    Category(6, "health", "保健营养", "food_beverage", "食品饮料"),
    Category(7, "home_cleaning", "日化家清", "home_care", "家清个护"),
    Category(8, "personal_care", "个人护理", "home_care", "家清个护"),
    Category(9, "beauty", "美容化妆", "home_care", "家清个护"),
    Category(10, "paper", "家用纸品", "home_care", "家清个护"),
    Category(11, "apparel", "服装鞋帽", "durable", "耐用消费品"),
    Category(12, "appliance", "家电厨具", "durable", "耐用消费品"),
)


@dataclass(frozen=True)
class Region:
    """区域维度：七大区。"""

    region_id: int
    code: str
    name: str


REGIONS: tuple[Region, ...] = (
    Region(1, "east", "华东"),
    Region(2, "south", "华南"),
    Region(3, "north", "华北"),
    Region(4, "central", "华中"),
    Region(5, "southwest", "西南"),
    Region(6, "northwest", "西北"),
    Region(7, "northeast", "东北"),
)


@dataclass(frozen=True)
class Segment:
    """客群维度：五个客群，用于"同一指标在不同人群里的表现"下钻。"""

    segment_id: int
    code: str
    name: str
    description: str


SEGMENTS: tuple[Segment, ...] = (
    Segment(1, "new", "新客", "本期首次成交的客户"),
    Segment(2, "high_value", "高价值老客", "历史累计成交额排名前 20% 的老客"),
    Segment(3, "price_sensitive", "价格敏感", "仅在促销价成交的客户"),
    Segment(4, "dormant", "沉默召回", "超过 90 天未成交后被唤醒的客户"),
    Segment(5, "enterprise", "企业客户", "以企业名义采购的 B 端客户"),
)


@dataclass(frozen=True)
class Promotion:
    """大促窗口：写在 dim_date 上，让"大促期间"成为可切片的维度而不是隐式常识。"""

    name: str
    start_month: int
    start_day: int
    end_month: int
    end_day: int
    lift_param: str


PROMOTIONS: tuple[Promotion, ...] = (
    Promotion("年货节", 1, 15, 1, 25, "promo_lift.yearend"),
    Promotion("6·18", 6, 15, 6, 20, "promo_lift.618"),
    Promotion("99大促", 9, 8, 9, 10, "promo_lift.nine_nine"),
    Promotion("双11", 11, 1, 11, 11, "promo_lift.double11"),
    Promotion("双12", 12, 10, 12, 12, "promo_lift.double12"),
)


# SKU 生成：12 个品类各 3 个 SKU，销量大的 4 个品类各多 1 个，合计 40 个。
_EXTRA_SKU_CATEGORIES = frozenset({"grain_oil", "beverage", "apparel", "appliance"})
_SKU_FACTORS = (0.78, 0.95, 1.15, 1.42)


def build_sku_master(calibration: Calibration) -> list[tuple[int, str, str, int, int, int]]:
    """按品类价格与毛利率参数展开 SKU 主数据。

    返回 ``(sku_id, code, name, category_id, unit_cost_cents, standard_price_cents)``。
    成本 = 标准售价 × (1 − 毛利率)，所以"原材料成本 / 营收"的比例天然落在快消行业量级。
    """

    rows: list[tuple[int, str, str, int, int, int]] = []
    sku_id = 1
    for category in CATEGORIES:
        price = int(calibration.value(f"category_price.{category.code}"))
        margin = calibration.value(f"category_gross_margin.{category.code}")
        count = 4 if category.code in _EXTRA_SKU_CATEGORIES else 3
        for index in range(count):
            factor = _SKU_FACTORS[index]
            standard_price = int(round(price * factor))
            unit_cost = int(round(standard_price * (1.0 - margin)))
            rows.append(
                (
                    sku_id,
                    f"SKU-{sku_id:04d}",
                    f"{category.name}-{chr(ord('A') + index)}款",
                    category.category_id,
                    unit_cost,
                    standard_price,
                )
            )
            sku_id += 1
    return rows


def promo_by_day(day: date, calibration: Calibration) -> tuple[int, str | None, float]:
    """返回某天的 ``(is_promo, promo_name, promo_lift)``。

    大促是"日历上的外部冲击"，异动归因必须能把它作为外生变量引用，所以它落在维度表上。
    """

    for promo in PROMOTIONS:
        start = date(day.year, promo.start_month, promo.start_day)
        end = date(day.year, promo.end_month, promo.end_day)
        if start <= day <= end:
            return 1, promo.name, calibration.value(promo.lift_param)
    return 0, None, 1.0


def season_factor(day: date, calibration: Calibration) -> float:
    """月度季节性因子（乘在日均量级上）。"""

    return calibration.value(f"season_factor.m{day.month:02d}")


def daterange(start: date, days: int) -> list[date]:
    """生成连续的日期序列（含起始日，共 ``days`` 天）。"""

    return [start + timedelta(days=offset) for offset in range(days)]
