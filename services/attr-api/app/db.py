"""数据库连接层（SQLite）。

设计要点（对应技术规格 §3 与 §6）：

1. **两个文件、两个身份**：``dw.db`` 是合成数仓（服务与沙箱都只读打开），``app.db`` 是业务与治理表。
2. **只读是打开方式决定的**：沙箱连接串固定 ``file:...?mode=ro&immutable=1``，SQLite 自身拒绝写，
   比"靠角色权限"更容易当场验证（见 ``infra/sqlite/verify_readonly.py``）。
3. **跨库查询用 ATTACH 保住 SQL 里的 ``dw.`` 前缀**：SQLite 没有 schema，用
   ``ATTACH DATABASE 'dw.db' AS dw`` 之后，``dw.fact_ecom_daily`` 这类写法与 PostgreSQL 一致，
   指标字典里已经写好的 SQL 不需要改写。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from app.config import Settings, get_settings

WAREHOUSE_ALIAS = "dw"
APP_ALIAS = "app"


def warehouse_readonly_uri(path: Path) -> str:
    """生成只读打开串：``mode=ro`` 禁止写，``immutable=1`` 声明文件不会被改动以走快路径。

    这是沙箱"数据只读"约束的实现方式，路径必须已存在且为绝对路径。
    """

    return f"file:{Path(path).as_posix()}?mode=ro&immutable=1"


def warehouse_readwrite_uri(path: Path) -> str:
    """生成可写打开串（只给数据生成器与运维脚本用，服务与沙箱都不许用）。"""

    return f"file:{Path(path).as_posix()}?mode=rwc"


def connect_warehouse_readonly(path: Path) -> sqlite3.Connection:
    """以只读方式打开数仓：主库就是 ``dw.db``，同时把它 ATTACH 成 ``dw``。

    这样一份连接里 ``fact_ecom_daily`` 与 ``dw.fact_ecom_daily`` 两种写法都能用：
    指标字典的 SQL 全部带 ``dw.`` 前缀，而运维脚本习惯写不带前缀的表名。
    ``mode=ro`` 保证文件写不进去，``PRAGMA query_only`` 保证连临时表也建不了。
    """

    uri = warehouse_readonly_uri(path)
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON")
    if path.exists():
        conn.execute(f"ATTACH DATABASE '{uri}' AS {WAREHOUSE_ALIAS}")
    conn.row_factory = sqlite3.Row
    return conn


def connect_warehouse_readwrite(path: Path, *, fast: bool = True) -> sqlite3.Connection:
    """以可写方式打开数仓（生成器专用）。``fast=True`` 时关闭 fsync 以压缩批量写入耗时。"""

    conn = sqlite3.connect(warehouse_readwrite_uri(path), uri=True, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    if fast:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -200000")
    return conn


def connect_app(path: Path) -> sqlite3.Connection:
    """打开业务库（可写）。审计表的不可删改由 DDL 里的触发器保证，见 ``app_schema``。"""

    conn = sqlite3.connect(Path(path).as_posix(), timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def attach_warehouse(
    conn: sqlite3.Connection, warehouse_path: Path, *, readonly: bool = True
) -> None:
    """把数仓挂到 ``dw`` 名字下，让 ``dw.fact_*`` 这类限定名继续可用。"""

    uri = (
        warehouse_readonly_uri(warehouse_path)
        if readonly
        else warehouse_readwrite_uri(warehouse_path)
    )
    conn.execute(f"ATTACH DATABASE '{uri}' AS {WAREHOUSE_ALIAS}")


def init_app_engine(settings: Settings | None = None) -> Engine:
    """创建业务库的 SQLAlchemy 引擎（服务进程用；沙箱进程不持有该引擎）。"""

    active = settings or get_settings()
    engine = create_engine(active.database_url, future=True)
    _enable_sqlite_foreign_keys(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _connection_record) -> None:  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


@contextmanager
def readonly_warehouse_scope(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """只读数仓的连接上下文：查询结束即关闭，避免长连接挡住外部文件操作。"""

    active = settings or get_settings()
    conn = connect_warehouse_readonly(active.warehouse_db)
    try:
        yield conn
    finally:
        conn.close()
