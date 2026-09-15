"""合成数仓生成器：固定种子、真值预埋、口径可追溯。

目标规模（对应需求说明书 §12 与 P2 出口条件"500 万行可复现生成"）：

* ``fact_ecom_daily``  546 天 × 3360 个维度组合中约 55% 活跃 ≈ 100 万行
* ``fact_order``       由支付成功单展开的订单明细 ≈ 170 万行
* ``fact_fmcg_daily``  546 天 × 8 渠道 × 40 SKU × 7 区域 = 122 万行

三条保证：

1. **恒等式按构造成立**：UV ≡ 曝光 × CTR（令访客数 = 点击数）、GMV = Σ 订单金额、
   件单价 = GMV ÷ 销量、连带率 = 销量 ÷ 支付单数，因此指标树的守恒断言天然可过。
2. **聚合表与明细表一致**：``fact_ecom_daily`` 的每个计数都直接由 ``fact_order`` 的订单聚合而来，
   不是两次独立随机（对账断言见 ``verify.py``）。
3. **真值由解析式给出**：注入只改动单一因子，期望值由 ``groundtruth.py`` 独立算出，
   并在窗口上做 LMDI 复核；禁止用被测系统的分解结果当期望值。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.db import connect_app, connect_warehouse_readwrite
from app.warehouse import app_schema, dw_schema
from app.warehouse import dimensions as dims
from app.warehouse.calibration import Calibration, CalibrationError
from app.warehouse.groundtruth import (
    InjectionSpec,
    counterfactual_metric,
    lmdi_factor_contribution,
    load_injections,
)

CHUNK_DAYS = 12
STATUS_PAID = 0
STATUS_REFUNDED = 1
STATUS_CANCELLED = 2
STATUS_LABELS = {STATUS_PAID: "paid", STATUS_REFUNDED: "refunded", STATUS_CANCELLED: "cancelled"}
SECONDS_PER_DAY = 86_400
DAYS_PER_YEAR = 365.25


@dataclass
class GenerationStats:
    """一次生成的实测结果：行数、耗时、真值条数——对外只允许引用这里的数字。"""

    seed: int
    days: int
    start_day: str
    end_day: str
    profile: str
    rows: dict[str, int] = field(default_factory=dict)
    ground_truth_rows: int = 0
    skipped_injections: list[str] = field(default_factory=list)
    reset_duration_s: float | None = None
    duration_s: float = 0.0
    generated_at: str = ""

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    def as_dict(self) -> dict[str, Any]:
        """转成可写入 ``eval/`` 与 ``03-进度与质量.md`` 的字典。"""

        payload = {
            "seed": self.seed,
            "days": self.days,
            "start_day": self.start_day,
            "end_day": self.end_day,
            "profile": self.profile,
            "facts_total_rows": self.total_rows,
            "facts_rows": dict(self.rows),
            "ground_truth_rows": self.ground_truth_rows,
            "skipped_injections": list(self.skipped_injections),
            "duration_s": round(self.duration_s, 3),
            "generated_at": self.generated_at,
        }
        if self.reset_duration_s is not None:
            payload["reset_duration_s"] = round(self.reset_duration_s, 3)
        return payload


def reset_warehouse(paths: list[Path]) -> float:
    """删除数仓与业务库文件（含 WAL/SHM），返回耗时秒数。

    只允许作用于 ``.data/`` 下的库文件——调用方传入的是显式路径，不做通配。
    """

    started = time.perf_counter()
    for path in paths:
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(f"{path}{suffix}")
            if target.exists():
                target.unlink()
    return time.perf_counter() - started


class WarehouseGenerator:
    """合成数仓生成器。所有随机性都来自 ``numpy`` 的 ``default_rng(seed)``。"""

    def __init__(
        self,
        calibration: Calibration | None = None,
        injections: list[InjectionSpec] | None = None,
        *,
        seed: int | None = None,
        days: int | None = None,
        start_day: date | None = None,
        profile: str = "full",
    ) -> None:
        settings = get_settings()
        self.calibration = calibration or Calibration.load()
        self.injections = injections if injections is not None else load_injections()
        self.seed = settings.warehouse_seed if seed is None else seed
        self.days = settings.warehouse_days if days is None else days
        self.start_day = start_day or date.fromisoformat(settings.warehouse_start_day)
        self.profile = profile
        self.end_day = self.start_day + timedelta(days=self.days - 1)
        # 只保留与生成区间有交集的注入：小样本跑（测试/冒烟）不会因为窗外注入而失败
        skipped = [
            spec.id
            for spec in self.injections
            if spec.end_day < self.start_day or spec.start_day > self.end_day
        ]
        self.skipped_injections = skipped
        self.injections = [spec for spec in self.injections if spec.id not in skipped]
        self._prepare()

    # ------------------------------------------------------------------ 准备

    def _prepare(self) -> None:
        """把参数表与维度编码展开成向量，避免在生成循环里反复查字典。"""

        calib = self.calibration
        self._channel_ids = np.array([c.channel_id for c in dims.CHANNELS])
        self._channel_ecom_w = np.array(
            [calib.value(f"channel_share.ecom.{c.code}") for c in dims.CHANNELS]
        )
        self._channel_fmcg_w = np.array(
            [calib.value(f"channel_share.fmcg.{c.code}") for c in dims.CHANNELS]
        )
        self._channel_growth = np.array(
            [calib.value(f"channel_growth.{c.code}") for c in dims.CHANNELS]
        )
        self._channel_commission = np.array(
            [
                calib.value(
                    "fmcg.commission_rate_"
                    + ("online" if channel.channel_type == "online" else "offline")
                )
                for channel in dims.CHANNELS
            ]
        )

        self._category_ids = np.array([c.category_id for c in dims.CATEGORIES])
        self._category_code_index = {c.code: index for index, c in enumerate(dims.CATEGORIES)}
        self._category_w = np.array(
            [calib.value(f"category_weight.{c.code}") for c in dims.CATEGORIES]
        )
        self._category_growth = np.array(
            [calib.value(f"category_growth.{c.code}") for c in dims.CATEGORIES]
        )

        self._region_ids = np.array([r.region_id for r in dims.REGIONS])
        self._region_code_index = {r.code: index for index, r in enumerate(dims.REGIONS)}
        self._region_w = np.array([calib.value(f"region_weight.{r.code}") for r in dims.REGIONS])
        self._region_logistics = np.array(
            [calib.value(f"region_logistics_factor.{r.code}") for r in dims.REGIONS]
        )

        self._segment_ids = np.array([s.segment_id for s in dims.SEGMENTS])
        self._segment_code_index = {s.code: index for index, s in enumerate(dims.SEGMENTS)}
        self._segment_w = np.array([calib.value(f"segment_weight.{s.code}") for s in dims.SEGMENTS])

        self._sku_rows = dims.build_sku_master(calib)
        self._sku_ids = np.array([row[0] for row in self._sku_rows])
        self._sku_category_id = np.array([row[3] for row in self._sku_rows])
        self._sku_unit_cost = np.array([row[4] for row in self._sku_rows], dtype=np.int64)
        self._sku_price = np.array([row[5] for row in self._sku_rows], dtype=np.int64)
        # SKU 结构权重：越便宜的 SKU 卖得越多（价格弹性的简化），同品类内归一化
        inverse_price = 1.0 / self._sku_price
        self._sku_w = np.zeros(len(self._sku_rows))
        for index, category in enumerate(dims.CATEGORIES):
            selector = self._sku_category_id == category.category_id
            weights = inverse_price[selector] / inverse_price[selector].sum()
            self._sku_w[selector] = weights * self._category_w[index]
        self._sku_code_index = {row[1]: index for index, row in enumerate(self._sku_rows)}
        self._sku_lookup, self._sku_lookup_count = self._build_sku_lookup()

        self._channel_code_index = {c.code: index for index, c in enumerate(dims.CHANNELS)}
        self._ecom_grid = self._build_ecom_grid()
        self._ecom_lambda = self._build_ecom_lambda()
        self._ecom_trend = self._build_ecom_trend()
        self._fmcg_grid = self._build_fmcg_grid()
        self._fmcg_lambda = self._build_fmcg_lambda()

    def _build_sku_lookup(self) -> tuple[np.ndarray, np.ndarray]:
        counts = np.array(
            [
                int((self._sku_category_id == category.category_id).sum())
                for category in dims.CATEGORIES
            ]
        )
        lookup = np.zeros((len(dims.CATEGORIES), int(counts.max())), dtype=np.int64)
        for index, category in enumerate(dims.CATEGORIES):
            selector = np.flatnonzero(self._sku_category_id == category.category_id)
            lookup[index, : len(selector)] = self._sku_ids[selector]
        return lookup, counts

    @staticmethod
    def _grid(shapes: list[int]) -> list[np.ndarray]:
        """构造多维组合网格的索引数组（最后一个维度变化最快）。"""

        total = int(np.prod(shapes))
        axes: list[np.ndarray] = []
        for position, size in enumerate(shapes):
            inner = int(np.prod(shapes[position + 1 :]))
            axes.append(np.tile(np.repeat(np.arange(size), inner), total // (size * inner)))
        return axes

    def _build_ecom_grid(self) -> dict[str, np.ndarray]:
        shapes = [len(dims.CHANNELS), len(dims.CATEGORIES), len(dims.REGIONS), len(dims.SEGMENTS)]
        channel, category, region, segment = self._grid(shapes)
        return {"channel": channel, "category": category, "region": region, "segment": segment}

    def _build_ecom_lambda(self) -> np.ndarray:
        grid = self._ecom_grid
        cells = len(grid["channel"])
        weights = (
            self._channel_ecom_w[grid["channel"]]
            * self._category_w[grid["category"]]
            * self._region_w[grid["region"]]
            * self._segment_w[grid["segment"]]
        )
        base = self.calibration.value("ecom.impressions_base")
        return base * weights * cells

    def _build_ecom_trend(self) -> np.ndarray:
        grid = self._ecom_grid
        return (1.0 + self._category_growth[grid["category"]]) * (
            1.0 + self._channel_growth[grid["channel"]]
        )

    def _build_fmcg_grid(self) -> dict[str, np.ndarray]:
        shapes = [len(dims.CHANNELS), len(self._sku_rows), len(dims.REGIONS)]
        channel, sku, region = self._grid(shapes)
        return {"channel": channel, "sku": sku, "region": region}

    def _build_fmcg_lambda(self) -> np.ndarray:
        grid = self._fmcg_grid
        cells = len(grid["channel"])
        weights = (
            self._channel_fmcg_w[grid["channel"]]
            * self._sku_w[grid["sku"]]
            * self._region_w[grid["region"]]
        )
        base = self.calibration.value("fmcg.units_base")
        return base * weights * cells

    # ------------------------------------------------------------------ 生成

    def generate(
        self,
        dw_path: Path | None = None,
        app_path: Path | None = None,
        *,
        reset: bool = False,
    ) -> GenerationStats:
        """生成维度表、三张事实表与真值清单。``reset=True`` 时先删库重建（用于测重置耗时）。"""

        settings = get_settings()
        dw_target = Path(dw_path) if dw_path is not None else settings.warehouse_db
        app_target = Path(app_path) if app_path is not None else settings.app_db
        for target in (dw_target, app_target):
            target.parent.mkdir(parents=True, exist_ok=True)

        stats = GenerationStats(
            seed=self.seed,
            days=self.days,
            start_day=self.start_day.isoformat(),
            end_day=self.end_day.isoformat(),
            profile=self.profile,
            skipped_injections=list(self.skipped_injections),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        if reset:
            stats.reset_duration_s = reset_warehouse([dw_target, app_target])

        started = time.perf_counter()
        dw_conn = connect_warehouse_readwrite(dw_target)
        app_conn = connect_app(app_target)
        try:
            self._rng = np.random.default_rng(self.seed)
            dw_schema.create_dw_schema(dw_conn)
            app_schema.create_app_schema(app_conn)
            dw_conn.commit()
            app_conn.commit()

            stats.rows["dim_date"] = self._write_dims(dw_conn)
            ecom_daily_rows, order_rows, truth_records = self._generate_ecom(dw_conn)
            stats.rows["fact_ecom_daily"] = ecom_daily_rows
            stats.rows["fact_order"] = order_rows
            fmcg_rows, fmcg_truth = self._generate_fmcg(dw_conn)
            stats.rows["fact_fmcg_daily"] = fmcg_rows
            truth_records.extend(fmcg_truth)
            self._write_ground_truth(app_conn, truth_records)
            stats.ground_truth_rows = len(truth_records)
            app_conn.commit()
        finally:
            dw_conn.close()
            app_conn.close()
        stats.duration_s = time.perf_counter() - started
        return stats

    def _write_dims(self, conn: sqlite3.Connection) -> int:
        days = dims.daterange(self.start_day, self.days)
        date_rows = []
        for day in days:
            is_promo, promo_name, promo_lift = dims.promo_by_day(day, self.calibration)
            date_rows.append(
                (
                    day.isoformat(),
                    day.year,
                    day.month,
                    (day.month - 1) // 3 + 1,
                    day.weekday(),
                    1 if day.weekday() >= 5 else 0,
                    is_promo,
                    promo_name,
                    dims.season_factor(day, self.calibration),
                    promo_lift,
                )
            )
        conn.executemany(
            "INSERT INTO dim_date (day, year, month, quarter, weekday, is_weekend, is_promo,"
            " promo_name, season_factor, promo_lift) VALUES (?,?,?,?,?,?,?,?,?,?)",
            date_rows,
        )
        conn.executemany(
            "INSERT INTO dim_channel"
            " (channel_id, code, name, is_paid, channel_type, commission_scope)"
            " VALUES (?,?,?,?,?,?)",
            [
                (c.channel_id, c.code, c.name, c.is_paid, c.channel_type, c.commission_scope)
                for c in dims.CHANNELS
            ],
        )
        conn.executemany(
            "INSERT INTO dim_category (category_id, code, name, parent_code, parent_name, level)"
            " VALUES (?,?,?,?,?,?)",
            [
                (c.category_id, c.code, c.name, c.parent_code, c.parent_name, 2)
                for c in dims.CATEGORIES
            ],
        )
        conn.executemany(
            "INSERT INTO dim_region (region_id, code, name) VALUES (?,?,?)",
            [(r.region_id, r.code, r.name) for r in dims.REGIONS],
        )
        conn.executemany(
            "INSERT INTO dim_segment (segment_id, code, name, description) VALUES (?,?,?,?)",
            [(s.segment_id, s.code, s.name, s.description) for s in dims.SEGMENTS],
        )
        conn.executemany(
            "INSERT INTO dim_sku (sku_id, code, name, category_id, unit_cost_cents,"
            " standard_price_cents) VALUES (?,?,?,?,?,?)",
            self._sku_rows,
        )
        conn.commit()
        return len(dims.CHANNELS) + len(dims.CATEGORIES) + len(dims.REGIONS) + len(
            dims.SEGMENTS
        ) + len(self._sku_rows)

    # ------------------------------------------------------------- 电商场景

    def _generate_ecom(
        self, conn: sqlite3.Connection
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """生成 ``fact_ecom_daily`` 与 ``fact_order``，并返回真值记录。"""

        calib = self.calibration
        grid = self._ecom_grid
        cells = len(grid["channel"])
        days = dims.daterange(self.start_day, self.days)
        ctr_base = calib.value("ecom.ctr_base")
        cart_rate = calib.value("ecom.cart_rate_base")
        order_rate = calib.value("ecom.order_rate_base")
        pay_rate = calib.value("ecom.pay_success_rate_base")
        active_rate = calib.value("ecom.active_combo_rate")
        impressions_sigma = calib.value("ecom.impressions_sigma")
        price_sigma = calib.value("ecom.unit_price_sigma")
        attach_extra = calib.value("ecom.attach_rate_extra")
        refund_rate = calib.value("ecom.refund_rate")
        weekend_lift = calib.value("weekend_lift.ecom")

        ecom_specs = [spec for spec in self.injections if spec.scenario == "ecom"]
        masks = {spec.id: self._injection_cell_mask("ecom", spec) for spec in ecom_specs}
        accumulators: dict[str, dict[str, float]] = {
            spec.id: {
                "base_factor": 0.0,
                "inj_factor": 0.0,
                "metric": 0.0,
                "visitors": 0.0,
            }
            for spec in ecom_specs
        }

        daily_rows = 0
        order_rows = 0
        order_seq = 0
        for chunk_start in range(0, len(days), CHUNK_DAYS):
            chunk = days[chunk_start : chunk_start + CHUNK_DAYS]
            n_days = len(chunk)
            years = np.array([(day - self.start_day).days / DAYS_PER_YEAR for day in chunk])
            season = np.array([dims.season_factor(day, calib) for day in chunk])
            promo = np.array([dims.promo_by_day(day, calib)[2] for day in chunk])
            weekend = np.array([weekend_lift if day.weekday() >= 5 else 1.0 for day in chunk])

            active = self._rng.random((cells, n_days)) < active_rate
            noise = np.exp(
                self._rng.normal(0.0, impressions_sigma, size=(cells, n_days))
                - impressions_sigma**2 / 2.0
            )
            level = season * promo * weekend
            mu = self._ecom_lambda[:, None] * level[None, :] * noise
            mu *= self._ecom_trend[:, None] ** years[None, :]

            impressions0 = self._rng.poisson(mu)
            ctr = np.clip(
                ctr_base * np.exp(self._rng.normal(0.0, 0.08, size=(cells, n_days))), 0.001, 0.5
            )
            clicks0 = self._rng.binomial(impressions0, ctr)
            cart = np.clip(
                cart_rate * np.exp(self._rng.normal(0.0, 0.06, size=(cells, n_days))), 0.01, 0.9
            )
            atc0 = self._rng.binomial(clicks0, cart)
            order = np.clip(
                order_rate * np.exp(self._rng.normal(0.0, 0.06, size=(cells, n_days))), 0.01, 0.99
            )
            created0 = self._rng.binomial(atc0, order)
            pay = np.clip(
                pay_rate * np.exp(self._rng.normal(0.0, 0.04, size=(cells, n_days))), 0.01, 0.999
            )
            paid0 = self._rng.binomial(created0, pay)

            impressions = impressions0.copy()
            clicks = clicks0.copy()
            atc = atc0.copy()
            created = created0.copy()
            paid = paid0.copy()
            applied: dict[str, np.ndarray] = {}
            for spec in ecom_specs:
                day_mask = np.array([spec.start_day <= day <= spec.end_day for day in chunk])
                if not day_mask.any():
                    continue
                mask = active & masks[spec.id][:, None] & day_mask[None, :]
                if not mask.any():
                    continue
                applied[spec.id] = mask
                multiplier = spec.multiplier
                if spec.leaf_factor == "impressions":
                    impressions[mask] = np.rint(impressions0[mask] * multiplier).astype(np.int64)
                    clicks[mask] = np.minimum(
                        np.rint(clicks0[mask] * multiplier).astype(np.int64), impressions[mask]
                    )
                    atc[mask] = np.minimum(
                        np.rint(atc0[mask] * multiplier).astype(np.int64), clicks[mask]
                    )
                    created[mask] = np.minimum(
                        np.rint(created0[mask] * multiplier).astype(np.int64), atc[mask]
                    )
                    paid[mask] = np.minimum(
                        np.rint(paid0[mask] * multiplier).astype(np.int64), created[mask]
                    )
                elif spec.leaf_factor == "pay_success_rate":
                    paid[mask] = np.minimum(
                        np.rint(paid0[mask] * multiplier).astype(np.int64), created[mask]
                    )
                else:
                    raise CalibrationError(f"电商场景不支持的注入叶子因子：{spec.leaf_factor}")

            refunded = self._rng.binomial(paid, refund_rate)
            paid_net = paid - refunded

            # 真值按"数仓里能查到的口径"记账：CVR 用净支付单数（orders_paid 列），
            # 因此反事实一侧也要按同样口径扣掉退款，否则记录的因子值对不上 SQL 复算。
            for spec in ecom_specs:
                mask = applied.get(spec.id)
                if mask is None:
                    continue
                if spec.leaf_factor == "impressions":
                    base_factor = float(clicks0[mask].sum())
                    injected_factor = float(clicks[mask].sum())
                else:
                    base_refunded = self._rng.binomial(paid0[mask], refund_rate)
                    base_factor = float(paid0[mask].sum() - base_refunded.sum())
                    injected_factor = float(paid_net[mask].sum())
                accumulators[spec.id]["base_factor"] += base_factor
                accumulators[spec.id]["inj_factor"] += injected_factor
                accumulators[spec.id]["visitors"] += float(clicks[mask].sum())

            order_arrays = self._build_ecom_orders(
                chunk,
                grid,
                active,
                created,
                paid_net,
                refunded,
                price_sigma,
                attach_extra,
                order_seq,
            )
            order_seq += int(order_arrays["rows"])
            order_rows += int(order_arrays["rows"])
            daily, truth_totals = self._write_ecom_chunk(
                conn, chunk, grid, active, impressions, clicks, atc, created, paid_net,
                order_arrays,
            )
            daily_rows += daily
            for spec_id, totals in truth_totals.items():
                accumulators[spec_id]["metric"] += totals["metric"]
            conn.commit()

        truth_records = self._finish_ecom_truth(ecom_specs, accumulators)
        return daily_rows, order_rows, truth_records

    def _build_ecom_orders(
        self,
        chunk: list[date],
        grid: dict[str, np.ndarray],
        active: np.ndarray,
        created: np.ndarray,
        paid_net: np.ndarray,
        refunded: np.ndarray,
        price_sigma: float,
        attach_extra: float,
        order_seq: int,
    ) -> dict[str, Any]:
        """把"每格每天支付成功单数"展开成订单明细，并聚合回日粒度。"""

        cells = len(grid["channel"])
        n_days = len(chunk)
        flat_created = np.where(active, created, 0).reshape(-1)
        flat_paid = np.where(active, paid_net, 0).reshape(-1)
        flat_refunded = np.where(active, refunded, 0).reshape(-1)
        total = int(flat_created.sum())
        if total == 0:
            empty = np.zeros(cells * n_days, dtype=np.int64)
            return {"rows": 0, "gmv": empty, "refund": empty, "units": empty}

        cell_index = np.repeat(np.arange(cells * n_days), flat_created)
        starts = np.cumsum(flat_created) - flat_created
        position = np.arange(total) - np.repeat(starts, flat_created)
        paid_per_order = np.repeat(flat_paid, flat_created)
        refund_per_order = np.repeat(flat_refunded, flat_created)
        status = np.where(
            position < paid_per_order,
            STATUS_PAID,
            np.where(
                position < paid_per_order + refund_per_order, STATUS_REFUNDED, STATUS_CANCELLED
            ),
        )

        cell_channel = grid["channel"][cell_index // n_days]
        cell_category = grid["category"][cell_index // n_days]
        cell_region = grid["region"][cell_index // n_days]
        cell_segment = grid["segment"][cell_index // n_days]
        day_index = cell_index % n_days

        # 订单内 SKU：同品类内均匀挑选（SKU 结构权重已体现在选品概率之外，属简化假设）
        sku_slot = self._rng.integers(0, self._sku_lookup_count[cell_category])
        sku_id = self._sku_lookup[cell_category, sku_slot]
        sku_row = np.searchsorted(self._sku_ids, sku_id)
        price = self._sku_price[sku_row]
        noise = np.exp(
            self._rng.normal(0.0, price_sigma, size=total) - price_sigma**2 / 2.0
        )
        unit_price = np.maximum(np.rint(price * noise).astype(np.int64), 1)
        units = 1 + self._rng.binomial(1, attach_extra, size=total)
        amount = unit_price * units
        second_of_day = self._rng.integers(0, SECONDS_PER_DAY, size=total)

        paid_selector = status == STATUS_PAID
        refund_selector = status == STATUS_REFUNDED
        gmv = np.bincount(
            cell_index[paid_selector], weights=amount[paid_selector], minlength=cells * n_days
        )
        refund_cents = np.bincount(
            cell_index[refund_selector], weights=amount[refund_selector], minlength=cells * n_days
        )
        units_paid = np.bincount(
            cell_index[paid_selector], weights=units[paid_selector], minlength=cells * n_days
        )

        day_strings = [day.isoformat() for day in chunk]
        order_day = day_index
        order_id = [
            f"{day_strings[order_day[index]]}{order_seq + index:09d}" for index in range(total)
        ]
        paid_at = [
            f"{day_strings[order_day[index]]} "
            f"{second_of_day[index] // 3600:02d}:{second_of_day[index] % 3600 // 60:02d}:"
            f"{second_of_day[index] % 60:02d}"
            for index in range(total)
        ]
        return {
            "rows": total,
            "order_id": order_id,
            "paid_at": paid_at,
            "day": [day_strings[index] for index in order_day],
            "channel_id": self._channel_ids[cell_channel],
            "category_id": self._category_ids[cell_category],
            "region_id": self._region_ids[cell_region],
            "segment_id": self._segment_ids[cell_segment],
            "sku_id": sku_id,
            "units": units,
            "amount_cents": amount,
            "status": [STATUS_LABELS[int(code)] for code in status],
            "gmv": np.rint(gmv).astype(np.int64),
            "refund": np.rint(refund_cents).astype(np.int64),
            "units_paid": np.rint(units_paid).astype(np.int64),
        }

    def _write_ecom_chunk(
        self,
        conn: sqlite3.Connection,
        chunk: list[date],
        grid: dict[str, np.ndarray],
        active: np.ndarray,
        impressions: np.ndarray,
        clicks: np.ndarray,
        atc: np.ndarray,
        created: np.ndarray,
        paid_net: np.ndarray,
        orders: dict[str, Any],
    ) -> tuple[int, dict[str, dict[str, float]]]:
        """写入一个分块：先订单明细，再日聚合；返回日行数与各注入的观测口径合计。"""

        cells = len(grid["channel"])
        n_days = len(chunk)
        keep = active.reshape(-1)
        flat_index = np.flatnonzero(keep)
        cell_of_row = flat_index // n_days
        day_of_row = flat_index % n_days
        day_strings = [day.isoformat() for day in chunk]
        gmv = orders["gmv"]
        refund = orders["refund"]
        units_paid = orders["units_paid"]

        daily_columns = [
            [day_strings[index] for index in day_of_row],
            self._channel_ids[grid["channel"][cell_of_row]],
            self._category_ids[grid["category"][cell_of_row]],
            self._region_ids[grid["region"][cell_of_row]],
            self._segment_ids[grid["segment"][cell_of_row]],
            impressions.reshape(-1)[flat_index],
            clicks.reshape(-1)[flat_index],
            clicks.reshape(-1)[flat_index],  # visitors ≡ clicks：UV = 曝光 × CTR 的构造保证
            atc.reshape(-1)[flat_index],
            created.reshape(-1)[flat_index],
            paid_net.reshape(-1)[flat_index],
            units_paid[flat_index],
            gmv[flat_index],
            refund[flat_index],
        ]
        self._insert_rows(
            conn,
            "fact_ecom_daily",
            [
                "day",
                "channel_id",
                "category_id",
                "region_id",
                "segment_id",
                "impressions",
                "clicks",
                "visitors",
                "add_to_cart",
                "orders_created",
                "orders_paid",
                "units",
                "gmv_cents",
                "refund_cents",
            ],
            daily_columns,
        )
        if orders["rows"]:
            self._insert_rows(
                conn,
                "fact_order",
                [
                    "order_id",
                    "day",
                    "paid_at",
                    "channel_id",
                    "category_id",
                    "region_id",
                    "segment_id",
                    "sku_id",
                    "units",
                    "amount_cents",
                    "status",
                ],
                [
                    orders["order_id"],
                    orders["day"],
                    orders["paid_at"],
                    orders["channel_id"],
                    orders["category_id"],
                    orders["region_id"],
                    orders["segment_id"],
                    orders["sku_id"],
                    orders["units"],
                    orders["amount_cents"],
                    orders["status"],
                ],
            )

        truth_totals: dict[str, dict[str, float]] = {}
        for spec in self.injections:
            if spec.scenario != "ecom":
                continue
            day_mask = np.array([spec.start_day <= day <= spec.end_day for day in chunk])
            if not day_mask.any():
                continue
            cell_mask = self._injection_cell_mask("ecom", spec)
            mask = active & cell_mask[:, None] & day_mask[None, :]
            if not mask.any():
                continue
            truth_totals[spec.id] = {"metric": float(gmv.reshape(cells, n_days)[mask].sum())}
        return int(keep.sum()), truth_totals

    def _finish_ecom_truth(
        self, specs: list[InjectionSpec], accumulators: dict[str, dict[str, float]]
    ) -> list[dict[str, Any]]:
        """由注入前后的因子水平与观测口径，算出期望贡献额（并做 LMDI 复核）。"""

        records: list[dict[str, Any]] = []
        for spec in specs:
            totals = accumulators[spec.id]
            visitors = totals["visitors"]
            if visitors <= 0:
                raise CalibrationError(f"注入 {spec.id} 的切片没有访客数据，无法给出稳定真值")
            if spec.leaf_factor == "impressions":
                # 曝光下滑直接改变 UV（访客数 = 点击数，见模块 docstring 的恒等式约定）
                base_factor = totals["base_factor"]
                inj_factor = totals["inj_factor"]
                factor_label = "uv"
            else:
                # 支付成功率下滑改变的是 CVR = 支付单数 ÷ 访客数
                base_factor = totals["base_factor"] / visitors
                inj_factor = totals["inj_factor"] / visitors
                factor_label = "cvr"
            observed = int(round(totals["metric"]))
            if base_factor <= 0 or inj_factor <= 0 or observed <= 0:
                raise CalibrationError(
                    f"注入 {spec.id} 的切片太小（base={base_factor} inj={inj_factor} "
                    f"metric={observed}），无法给出稳定真值：请扩大切片或改注入窗口"
                )
            counterfactual = counterfactual_metric(observed, base_factor, inj_factor)
            contribution = observed - counterfactual
            residual = lmdi_factor_contribution(
                float(counterfactual), float(observed), base_factor, inj_factor
            ) - contribution
            if abs(residual) > 1.0:
                raise CalibrationError(f"注入 {spec.id} 的 LMDI 复核残差过大：{residual:.6f} 分")
            records.append(
                {
                    "scenario": spec.scenario,
                    "day": spec.start_day.isoformat(),
                    "window_end": spec.end_day.isoformat(),
                    "dimension_json": spec.filter_json(),
                    "factor": spec.factor,
                    "leaf_factor": spec.leaf_factor,
                    "injection_pct": spec.injection_pct,
                    "factor_base_value": base_factor,
                    "factor_injected_value": inj_factor,
                    "metric_counterfactual_cents": counterfactual,
                    "metric_observed_cents": observed,
                    "injected_contribution_cents": contribution,
                    "observed_slice_metric_cents": observed,
                    "verify_sql": self._verify_sql(spec),
                    "note": (
                        f"{spec.note}；因子口径 {factor_label}"
                        f"（{base_factor:.6f} → {inj_factor:.6f}）；"
                        f"LMDI 复核残差 {residual:.3e} 分"
                    ),
                }
            )
        return records

    # ------------------------------------------------------------- 快消场景

    def _generate_fmcg(self, conn: sqlite3.Connection) -> tuple[int, list[dict[str, Any]]]:
        """生成 ``fact_fmcg_daily``（全组合不稀疏：8 × 40 × 7 × 546 行）。"""

        calib = self.calibration
        grid = self._fmcg_grid
        cells = len(grid["channel"])
        days = dims.daterange(self.start_day, self.days)
        units_sigma = calib.value("fmcg.units_sigma")
        logistics_base = calib.value("fmcg.logistics_per_unit_cents")
        other_ratio = calib.value("fmcg.other_cost_ratio")
        cost_drift = calib.value("fmcg.cost_drift_annual")
        weekend_lift = calib.value("weekend_lift.fmcg")

        specs = [spec for spec in self.injections if spec.scenario == "fmcg"]
        masks = {spec.id: self._injection_cell_mask("fmcg", spec) for spec in specs}
        accumulators: dict[str, dict[str, float]] = {
            spec.id: {
                "cost_inj": 0.0,
                "cost_base": 0.0,
                "non_cost": 0.0,
                "units": 0.0,
            }
            for spec in specs
        }

        price = self._sku_price[grid["sku"]]
        unit_cost = self._sku_unit_cost[grid["sku"]]
        commission_rate = self._channel_commission[grid["channel"]]
        logistics_rate = logistics_base * self._region_logistics[grid["region"]]
        trend = (1.0 + self._category_growth[
            self._sku_category_index(grid["sku"])
        ]) * (1.0 + self._channel_growth[grid["channel"]])

        rows = 0
        for chunk_start in range(0, len(days), CHUNK_DAYS):
            chunk = days[chunk_start : chunk_start + CHUNK_DAYS]
            n_days = len(chunk)
            years = np.array([(day - self.start_day).days / DAYS_PER_YEAR for day in chunk])
            season = np.array([dims.season_factor(day, calib) for day in chunk])
            promo = np.array([dims.promo_by_day(day, calib)[2] for day in chunk])
            weekend = np.array([weekend_lift if day.weekday() >= 5 else 1.0 for day in chunk])
            noise = np.exp(
                self._rng.normal(0.0, units_sigma, size=(cells, n_days)) - units_sigma**2 / 2.0
            )
            level = season * promo * weekend
            mu = self._fmcg_lambda[:, None] * level[None, :] * noise
            mu *= trend[:, None] ** years[None, :]
            units = self._rng.poisson(mu)

            cost_multiplier = np.ones((cells, n_days))
            logistics_multiplier = np.ones((cells, n_days))
            for spec in specs:
                day_mask = np.array([spec.start_day <= day <= spec.end_day for day in chunk])
                if not day_mask.any():
                    continue
                mask = masks[spec.id][:, None] & day_mask[None, :]
                if not mask.any():
                    continue
                if spec.leaf_factor == "unit_cost":
                    cost_multiplier[mask] = spec.multiplier
                elif spec.leaf_factor == "logistics_per_unit":
                    logistics_multiplier[mask] = spec.multiplier
                else:
                    raise CalibrationError(f"快消场景不支持的注入叶子因子：{spec.leaf_factor}")

            drift = (1.0 + cost_drift) ** years
            cost = np.rint(unit_cost[:, None] * drift[None, :] * cost_multiplier).astype(np.int64)
            logistics = np.rint(
                logistics_rate[:, None] * logistics_multiplier * units
            ).astype(np.int64)
            base_logistics = np.rint(logistics_rate[:, None] * units).astype(np.int64)
            base_cost = np.rint(unit_cost[:, None] * drift[None, :]).astype(np.int64)

            revenue = units * price[:, None]
            raw_material = cost * units
            commission = np.rint(revenue * commission_rate[:, None]).astype(np.int64)
            other = np.rint(revenue * other_ratio).astype(np.int64)

            for spec in specs:
                day_mask = np.array([spec.start_day <= day <= spec.end_day for day in chunk])
                if not day_mask.any():
                    continue
                mask = masks[spec.id][:, None] & day_mask[None, :]
                if not mask.any():
                    continue
                if spec.leaf_factor == "unit_cost":
                    accumulators[spec.id]["cost_base"] += float(
                        (base_cost[mask] * units[mask]).sum()
                    )
                    accumulators[spec.id]["cost_inj"] += float(raw_material[mask].sum())
                    # 毛利 = 其它项 − 被注入的这一项；这里只累计"其它项"
                    other_terms = revenue - logistics - commission - other
                else:
                    accumulators[spec.id]["cost_base"] += float(base_logistics[mask].sum())
                    accumulators[spec.id]["cost_inj"] += float(logistics[mask].sum())
                    other_terms = revenue - raw_material - commission - other
                accumulators[spec.id]["non_cost"] += float(
                    other_terms[mask].sum()
                )
                accumulators[spec.id]["units"] += float(units[mask].sum())

            day_strings = [day.isoformat() for day in chunk]
            flat_day = [day_strings[index] for index in np.tile(np.arange(n_days), cells)]
            flat_channels = np.repeat(self._channel_ids[grid["channel"]], n_days)
            flat_skus = np.repeat(self._sku_ids[grid["sku"]], n_days)
            flat_regions = np.repeat(self._region_ids[grid["region"]], n_days)
            self._insert_rows(
                conn,
                "fact_fmcg_daily",
                [
                    "day",
                    "channel_id",
                    "sku_id",
                    "region_id",
                    "units",
                    "revenue_cents",
                    "raw_material_cents",
                    "logistics_cents",
                    "channel_commission_cents",
                    "other_cost_cents",
                ],
                [
                    flat_day,
                    flat_channels,
                    flat_skus,
                    flat_regions,
                    units.reshape(-1),
                    revenue.reshape(-1),
                    raw_material.reshape(-1),
                    logistics.reshape(-1),
                    commission.reshape(-1),
                    other.reshape(-1),
                ],
            )
            rows += cells * n_days
            conn.commit()

        return rows, self._finish_fmcg_truth(specs, accumulators)

    def _finish_fmcg_truth(
        self, specs: list[InjectionSpec], accumulators: dict[str, dict[str, float]]
    ) -> list[dict[str, Any]]:
        """快消场景真值：成本项上涨多少，毛利额就掉多少（差额分析，sign = -1）。"""

        records: list[dict[str, Any]] = []
        for spec in specs:
            totals = accumulators[spec.id]
            base_cost = totals["cost_base"]
            injected_cost = totals["cost_inj"]
            non_cost = totals["non_cost"]
            units = totals["units"]
            if base_cost <= 0 or injected_cost <= 0 or non_cost <= 0 or units <= 0:
                raise CalibrationError(f"注入 {spec.id} 的切片没有成本数据，无法给出真值")
            metric_observed = int(round(non_cost - injected_cost))
            metric_counterfactual = int(round(non_cost - base_cost))
            contribution = metric_observed - metric_counterfactual
            records.append(
                {
                    "scenario": spec.scenario,
                    "day": spec.start_day.isoformat(),
                    "window_end": spec.end_day.isoformat(),
                    "dimension_json": spec.filter_json(),
                    "factor": spec.factor,
                    "leaf_factor": spec.leaf_factor,
                    "injection_pct": spec.injection_pct,
                    "factor_base_value": base_cost / units,
                    "factor_injected_value": injected_cost / units,
                    "metric_counterfactual_cents": metric_counterfactual,
                    "metric_observed_cents": metric_observed,
                    "injected_contribution_cents": contribution,
                    "observed_slice_metric_cents": metric_observed,
                    "verify_sql": self._verify_sql(spec),
                    "note": (
                        f"{spec.note}；成本项变动 {injected_cost - base_cost:.0f} 分，"
                        "毛利额贡献 = −Δ成本（差额分析，sign = -1）"
                    ),
                }
            )
        return records

    # ------------------------------------------------------------------ 真值

    def _injection_cell_mask(self, scenario: str, spec: InjectionSpec) -> np.ndarray:
        """把维度编码过滤条件翻译成组合网格上的布尔掩码（未知编码直接报错，不静默忽略）。"""

        if scenario == "ecom":
            grid = self._ecom_grid
            lookup = {
                "channel": (grid["channel"], self._channel_code_index, len(dims.CHANNELS)),
                "category": (grid["category"], self._category_code_index, len(dims.CATEGORIES)),
                "region": (grid["region"], self._region_code_index, len(dims.REGIONS)),
                "segment": (grid["segment"], self._segment_code_index, len(dims.SEGMENTS)),
            }
        else:
            grid = self._fmcg_grid
            sku_categories = self._sku_category_index(grid["sku"])
            category_index = {c.code: index for index, c in enumerate(dims.CATEGORIES)}
            lookup = {
                "channel": (grid["channel"], self._channel_code_index, len(dims.CHANNELS)),
                "region": (grid["region"], self._region_code_index, len(dims.REGIONS)),
                "sku": (grid["sku"], self._sku_code_index, len(self._sku_rows)),
                "category": (sku_categories, category_index, len(dims.CATEGORIES)),
            }
        mask = np.ones(len(next(iter(lookup.values()))[0]), dtype=bool)
        for key, codes in spec.filters.items():
            if key not in lookup:
                raise CalibrationError(
                    f"注入 {spec.id} 用了 {scenario} 场景不支持的过滤维度：{key}"
                )
            values, index_map, size = lookup[key]
            missing = [code for code in codes if code not in index_map]
            if missing:
                raise CalibrationError(f"注入 {spec.id} 的过滤值不存在：{missing}")
            wanted = np.array([index_map[code] for code in codes])
            mask &= np.isin(values, wanted)
        return mask

    def _sku_category_index(self, sku_index: np.ndarray) -> np.ndarray:
        """SKU 索引 → 品类索引（快消场景按品类聚合增速与过滤）。"""

        category_ids = self._sku_category_id[sku_index]
        return np.searchsorted(self._category_ids, category_ids)

    def _verify_sql(self, spec: InjectionSpec) -> str:
        """给评测留一条独立 SQL：直接从数仓复算该切片的观测口径。"""

        where = [f"day BETWEEN '{spec.start_day}' AND '{spec.end_day}'"]
        if spec.scenario == "ecom":
            metric = "SUM(gmv_cents)"
            table = "fact_ecom_daily"
            columns = {
                "channel": ("channel_id", self._channel_id_of),
                "category": ("category_id", self._category_id_of),
                "region": ("region_id", self._region_id_of),
                "segment": ("segment_id", self._segment_id_of),
            }
            for key, codes in spec.filters.items():
                if key not in columns:
                    raise CalibrationError(f"电商场景的 verify SQL 不支持过滤维度 {key}")
                column, resolver = columns[key]
                where.append(self._in_clause(column, [resolver(code) for code in codes]))
        else:
            metric = (
                "SUM(revenue_cents - raw_material_cents - logistics_cents"
                " - channel_commission_cents - other_cost_cents)"
            )
            table = "fact_fmcg_daily"
            columns = {
                "channel": ("channel_id", self._channel_id_of),
                "region": ("region_id", self._region_id_of),
            }
            for key, codes in spec.filters.items():
                if key in columns:
                    column, resolver = columns[key]
                    where.append(self._in_clause(column, [resolver(code) for code in codes]))
                elif key == "category":
                    ids = self._in_clause(
                        "category_id", [self._category_id_of(code) for code in codes]
                    )
                    where.append(f"sku_id IN (SELECT sku_id FROM dw.dim_sku WHERE {ids})")
                elif key == "sku":
                    where.append(
                        self._in_clause("sku_id", [self._sku_id_of(code) for code in codes])
                    )
                else:
                    raise CalibrationError(f"快消场景的 verify SQL 不支持过滤维度 {key}")
        return f"SELECT {metric} FROM dw.{table} WHERE " + " AND ".join(where)

    @staticmethod
    def _in_clause(column: str, values: list[int]) -> str:
        """拼 ``IN`` 条件（整数 id 由本类解析，不接受外部字符串，避免 SQL 注入面）。"""

        return f"{column} IN (" + ",".join(str(value) for value in values) + ")"

    def _channel_id_of(self, code: str) -> int:
        return dims.CHANNELS[self._channel_code_index[code]].channel_id

    def _category_id_of(self, code: str) -> int:
        return dims.CATEGORIES[self._category_code_index[code]].category_id

    def _region_id_of(self, code: str) -> int:
        return dims.REGIONS[self._region_code_index[code]].region_id

    def _segment_id_of(self, code: str) -> int:
        return dims.SEGMENTS[self._segment_code_index[code]].segment_id

    def _sku_id_of(self, code: str) -> int:
        return self._sku_rows[self._sku_code_index[code]][0]

    def _write_ground_truth(self, conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
        """写入真值清单：这是评测期望值的唯一来源。"""

        columns = [
            "scenario",
            "day",
            "window_end",
            "dimension_json",
            "factor",
            "leaf_factor",
            "injection_pct",
            "factor_base_value",
            "factor_injected_value",
            "metric_counterfactual_cents",
            "metric_observed_cents",
            "injected_contribution_cents",
            "observed_slice_metric_cents",
            "verify_sql",
            "note",
        ]
        conn.executemany(
            f"INSERT INTO ground_truth ({', '.join(columns)})"
            f" VALUES ({', '.join('?' * len(columns))})",
            [
                (
                    record["scenario"],
                    record["day"],
                    record["window_end"],
                    record["dimension_json"],
                    record["factor"],
                    record["leaf_factor"],
                    record["injection_pct"],
                    record["factor_base_value"],
                    record["factor_injected_value"],
                    record["metric_counterfactual_cents"],
                    record["metric_observed_cents"],
                    record["injected_contribution_cents"],
                    record["observed_slice_metric_cents"],
                    record["verify_sql"],
                    record["note"],
                )
                for record in records
            ],
        )

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _insert_rows(
        conn: sqlite3.Connection, table: str, columns: list[str], arrays: list[Any]
    ) -> None:
        """批量插入：``tolist()`` 把 numpy 标量转成 Python 标量，避免驱动绑定报错。"""

        sql = (
            f"INSERT INTO {table} ({', '.join(columns)})"
            f" VALUES ({', '.join(['?'] * len(columns))})"
        )
        payload = [np.asarray(array).tolist() for array in arrays]
        conn.executemany(sql, zip(*payload, strict=True))
