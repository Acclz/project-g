"""沙箱表名白名单：从数仓库结构的唯一定义处取，不在这里另抄一份。"""

from __future__ import annotations

from app.warehouse.dw_schema import table_names


def allowed_table_names() -> frozenset[str]:
    """允许在沙箱 SQL 里出现的表名（仅 ``dw`` 侧，业务表永远不在此列）。"""

    return frozenset(table_names())
