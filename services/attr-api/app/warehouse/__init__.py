"""合成数仓：库结构、口径参数、生成器与校验（P2 交付）。

职责边界：

* ``calibration.py``  读 ``corpus/warehouse/calibration.csv``，做参数校验（缺项即失败）。
* ``dimensions.py``   维度表与 SKU 主数据（名称、编码、层级这类"没有数值来源"的静态数据）。
* ``dw_schema.py``    数仓（dw.db）的唯一定义处：建表 SQL 与索引。
* ``app_schema.py``   业务与治理表（app.db）的唯一定义处，含审计表不可删改触发器。
* ``generator.py``    固定种子生成维度表 + 三张事实表 + 真值注入。
* ``groundtruth.py``  真值的独立解析式核算（严禁用被测系统的分解结果反推）。
* ``verify.py``       对账断言：聚合表 vs 明细表、指标 SQL 可执行性、真值期间复核。
"""

from app.warehouse.calibration import Calibration, CalibrationError
from app.warehouse.generator import GenerationStats, WarehouseGenerator, reset_warehouse

__all__ = [
    "Calibration",
    "CalibrationError",
    "GenerationStats",
    "WarehouseGenerator",
    "reset_warehouse",
]
