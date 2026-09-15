"""数仓（``dw.db``）的唯一定义处：建表 SQL 与索引。

数据库从 PostgreSQL 改为 SQLite 的取舍与理由见 ``docs/04-决策记录.md`` D-22。
SQLite 没有 schema，因此用"一个文件一个库 + ATTACH 别名 ``dw``"保住指标字典里
``dw.fact_ecom_daily`` 这类限定名（见 ``app/db.py``）。

行数目标（与需求说明书 §12、技术规格 §3.1 对齐）：

* ``fact_ecom_daily``  546 天 × 8 × 12 × 7 × 5 的组合中约 55% 活跃 ≈ 100 万行
* ``fact_order``       由 ``orders_paid`` 展开的订单明细 ≈ 160 万行
* ``fact_fmcg_daily``  546 天 × 8 × 40 × 7 = 1,223,040 行 ≈ 120 万行

合计约 380 万行（不含索引），加索引后达到 500 万行量级。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE dim_date (
        day           TEXT PRIMARY KEY,
        year          INTEGER NOT NULL,
        month         INTEGER NOT NULL,
        quarter       INTEGER NOT NULL,
        weekday       INTEGER NOT NULL,
        is_weekend    INTEGER NOT NULL,
        is_promo      INTEGER NOT NULL,
        promo_name    TEXT,
        season_factor REAL    NOT NULL,
        promo_lift    REAL    NOT NULL
    )
    """,
    """
    CREATE TABLE dim_channel (
        channel_id       INTEGER PRIMARY KEY,
        code             TEXT    NOT NULL UNIQUE,
        name             TEXT    NOT NULL,
        is_paid          INTEGER NOT NULL,
        channel_type     TEXT    NOT NULL,
        commission_scope TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE dim_category (
        category_id INTEGER PRIMARY KEY,
        code        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        parent_code TEXT    NOT NULL,
        parent_name TEXT    NOT NULL,
        level       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE dim_region (
        region_id INTEGER PRIMARY KEY,
        code      TEXT    NOT NULL UNIQUE,
        name      TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE dim_segment (
        segment_id  INTEGER PRIMARY KEY,
        code        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE dim_sku (
        sku_id               INTEGER PRIMARY KEY,
        code                 TEXT    NOT NULL UNIQUE,
        name                 TEXT    NOT NULL,
        category_id          INTEGER NOT NULL REFERENCES dim_category(category_id),
        unit_cost_cents      INTEGER NOT NULL,
        standard_price_cents INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE fact_ecom_daily (
        day             TEXT    NOT NULL,
        channel_id      INTEGER NOT NULL REFERENCES dim_channel(channel_id),
        category_id     INTEGER NOT NULL REFERENCES dim_category(category_id),
        region_id       INTEGER NOT NULL REFERENCES dim_region(region_id),
        segment_id      INTEGER NOT NULL REFERENCES dim_segment(segment_id),
        impressions     INTEGER NOT NULL,
        clicks          INTEGER NOT NULL,
        visitors        INTEGER NOT NULL,
        add_to_cart     INTEGER NOT NULL,
        orders_created  INTEGER NOT NULL,
        orders_paid     INTEGER NOT NULL,
        units           INTEGER NOT NULL,
        gmv_cents       INTEGER NOT NULL,
        refund_cents    INTEGER NOT NULL,
        PRIMARY KEY (day, channel_id, category_id, region_id, segment_id)
    )
    """,
    """
    CREATE TABLE fact_order (
        order_id    TEXT    PRIMARY KEY,
        day         TEXT    NOT NULL,
        paid_at     TEXT    NOT NULL,
        channel_id  INTEGER NOT NULL REFERENCES dim_channel(channel_id),
        category_id INTEGER NOT NULL REFERENCES dim_category(category_id),
        region_id   INTEGER NOT NULL REFERENCES dim_region(region_id),
        segment_id  INTEGER NOT NULL REFERENCES dim_segment(segment_id),
        sku_id      INTEGER NOT NULL REFERENCES dim_sku(sku_id),
        units       INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        status      TEXT    NOT NULL CHECK (status IN ('paid', 'refunded', 'cancelled'))
    )
    """,
    """
    CREATE TABLE fact_fmcg_daily (
        day                       TEXT    NOT NULL,
        channel_id                INTEGER NOT NULL REFERENCES dim_channel(channel_id),
        sku_id                    INTEGER NOT NULL REFERENCES dim_sku(sku_id),
        region_id                 INTEGER NOT NULL REFERENCES dim_region(region_id),
        units                     INTEGER NOT NULL,
        revenue_cents             INTEGER NOT NULL,
        raw_material_cents        INTEGER NOT NULL,
        logistics_cents           INTEGER NOT NULL,
        channel_commission_cents  INTEGER NOT NULL,
        other_cost_cents          INTEGER NOT NULL,
        PRIMARY KEY (day, channel_id, sku_id, region_id)
    )
    """,
)


INDEX_DDL: tuple[str, ...] = (
    # 大盘与一级拆解按天取数
    "CREATE INDEX idx_ecom_day ON fact_ecom_daily (day)",
    "CREATE INDEX idx_ecom_channel_day ON fact_ecom_daily (channel_id, day)",
    "CREATE INDEX idx_ecom_category_day ON fact_ecom_daily (category_id, day)",
    "CREATE INDEX idx_ecom_region_day ON fact_ecom_daily (region_id, day)",
    # 明细下钻：按天、按渠道、按 SKU
    "CREATE INDEX idx_order_day ON fact_order (day)",
    "CREATE INDEX idx_order_channel_day ON fact_order (channel_id, day)",
    "CREATE INDEX idx_order_status ON fact_order (status)",
    # 快消经营：按天、按 SKU、按渠道
    "CREATE INDEX idx_fmcg_day ON fact_fmcg_daily (day)",
    "CREATE INDEX idx_fmcg_sku_day ON fact_fmcg_daily (sku_id, day)",
    "CREATE INDEX idx_fmcg_channel_day ON fact_fmcg_daily (channel_id, day)",
)


def create_dw_schema(conn: sqlite3.Connection) -> None:
    """建表 + 建索引；调用方负责在事务内执行并提交。"""

    for statement in TABLE_DDL:
        conn.execute(statement)
    for statement in INDEX_DDL:
        conn.execute(statement)


def table_names() -> list[str]:
    """数仓表名清单（沙箱表名白名单的唯一来源）。"""

    names: list[str] = []
    for statement in TABLE_DDL:
        names.append(statement.split("CREATE TABLE", 1)[1].split("(", 1)[0].strip())
    return names


def index_names() -> list[str]:
    """索引名清单（生成器重建与校验脚本共用）。"""

    return [
        statement.split("CREATE INDEX", 1)[1].split("ON", 1)[0].strip()
        for statement in INDEX_DDL
    ]


def whitelist_sql() -> str:
    """沙箱白名单表名（P3 AST 校验用）的 SQL 片段形式。"""

    quoted: Iterable[str] = (f"dw.{name}" for name in table_names())
    return ", ".join(quoted)
