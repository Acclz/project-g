"""测试夹具：小样本数仓。

单测不跑完整档（546 天），只生成足以覆盖"注入窗口 + 对账 + 指标 SQL"的最小数据集，
完整档由 ``scripts/generate_warehouse.py --profile full`` 在阶段收尾时实测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.warehouse.generator import GenerationStats, WarehouseGenerator


@dataclass(frozen=True)
class SmallWarehouse:
    """一份小样本数仓：路径 + 生成实测结果。"""

    dw_path: Path
    app_path: Path
    stats: GenerationStats


def _generate(root: Path, *, start: date, days: int, seed: int = 20260915) -> SmallWarehouse:
    generator = WarehouseGenerator(seed=seed, days=days, start_day=start, profile="tiny")
    stats = generator.generate(root / "dw.db", root / "app.db")
    return SmallWarehouse(dw_path=root / "dw.db", app_path=root / "app.db", stats=stats)


@pytest.fixture(scope="session")
def ecom_injection_warehouse(tmp_path_factory: pytest.TempPathFactory) -> SmallWarehouse:
    """覆盖电商注入窗口（2026-06-05 ~ 2026-06-11）的小样本。"""

    root = tmp_path_factory.mktemp("dw_ecom")
    return _generate(root, start=date(2026, 6, 1), days=8)


@pytest.fixture(scope="session")
def fmcg_injection_warehouse(tmp_path_factory: pytest.TempPathFactory) -> SmallWarehouse:
    """覆盖快消成本注入窗口（2026-04-06 ~ 2026-04-19）的小样本。"""

    root = tmp_path_factory.mktemp("dw_fmcg")
    return _generate(root, start=date(2026, 4, 1), days=21)


@pytest.fixture(scope="session")
def sandbox_settings(ecom_injection_warehouse: SmallWarehouse) -> Settings:
    """指向临时数仓的配置：沙箱测试绝不允许碰仓库里的 ``.data/``。"""

    root = ecom_injection_warehouse.dw_path.parent
    return Settings(
        warehouse_db_path=ecom_injection_warehouse.dw_path,
        app_db_path=ecom_injection_warehouse.app_path,
        sandbox_tmp_dir=root / "sandbox_tmp",
    )
