"""参考数据播种：把"字典里的定义"变成"库里的行"。

为什么 P4 就需要播种：需求说明书 §5.5/§5.6 要求"置信度与排除理由必须落库"，
而 `hypotheses` / `evidence` 都挂在 `sessions` 上，`sessions` 又要引用
`users`（created_by）与 `metric_definitions`（metric_id）。所以先把这三样播好，
P5 再补会话状态机与登录（密码哈希已经按 argon2 写入，登录接口留到 P5）。

播种是**幂等**的：表里已有数据就跳过，绝不覆盖人工维护的内容。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from app.db import connect_app
from app.services.decomposition import MetricEngine
from app.services.events import sync_seed_events

DEFAULT_PASSWORD_BYTES = 18

#: 默认账号（P5 接登录时用它做冒烟；口令随机生成、只写哈希，不落明文）
SEED_USERS = (
    ("analyst@local", "analyst", "分析员"),
    ("viewer@local", "viewer", "只读访客"),
    ("admin@local", "admin", "系统管理员"),
)


@dataclass
class SeedResult:
    """播种结果：各类新增行数（0 表示原本就有，幂等跳过）。"""

    users: int = 0
    metrics: int = 0
    metric_versions: int = 0
    events: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "users": self.users,
            "metrics": self.metrics,
            "metric_versions": self.metric_versions,
            "events": self.events,
        }


def seed_reference_data(engine: MetricEngine | None = None) -> SeedResult:
    """播种用户、指标定义、指标版本与事件日历（幂等）。"""

    active_engine = engine or MetricEngine()
    result = SeedResult()
    connection = connect_app(active_engine.settings.app_db)
    try:
        result.users = _seed_users(connection)
        metrics, versions = _seed_metrics(connection, active_engine)
        result.metrics = metrics
        result.metric_versions = versions
        result.events = sync_seed_events(connection)
    finally:
        connection.close()
    return result


def _seed_users(connection) -> int:
    existing = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing:
        return 0
    from argon2 import PasswordHasher

    hasher = PasswordHasher()
    rows = []
    for email, role, display_name in SEED_USERS:
        password = secrets.token_urlsafe(DEFAULT_PASSWORD_BYTES)
        rows.append((email, hasher.hash(password), role, display_name))
    connection.executemany(
        "INSERT INTO users (email, password_hash, role, display_name) VALUES (?,?,?,?)", rows
    )
    connection.commit()
    return len(rows)


def _seed_metrics(connection, engine: MetricEngine) -> tuple[int, int]:
    existing = connection.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0]
    if existing:
        return 0, 0
    defined = 0
    versioned = 0
    for scenario in engine.scenarios():
        tree = engine.tree(scenario)
        for node in tree.nodes():
            cursor = connection.execute(
                "INSERT INTO metric_definitions (code, name, level, formula, unit, precision,"
                " structure) VALUES (?,?,?,?,?,?,?)",
                (
                    f"{scenario}.{node.code}",
                    node.name,
                    node.level,
                    node.formula,
                    node.unit or "cent",
                    2,
                    node.structure,
                ),
            )
            metric_id = int(cursor.lastrowid or 0)
            defined += 1
            connection.execute(
                "INSERT INTO metric_versions (metric_id, version, definition_json, effective_from)"
                " VALUES (?,?,?,?)",
                (
                    metric_id,
                    1,
                    json.dumps(
                        {
                            "code": node.code,
                            "scenario": scenario,
                            "level": node.level,
                            "structure": node.structure,
                            "method": node.method,
                            "formula": node.formula,
                            "sql": node.sql,
                            "sign": node.sign,
                            "caliber": node.caliber,
                        },
                        ensure_ascii=False,
                    ),
                    "2026-09-15",
                ),
            )
            versioned += 1
    connection.commit()
    return defined, versioned


def database_overview(engine: MetricEngine | None = None) -> dict[str, Any]:
    """库内行数概览（冒烟与文档用，只读）。"""

    active_engine = engine or MetricEngine()
    connection = connect_app(active_engine.settings.app_db)
    try:
        tables = (
            "users",
            "metric_definitions",
            "metric_versions",
            "events",
            "sessions",
            "session_steps",
            "hypotheses",
            "evidence",
            "sandbox_runs",
            "eval_runs",
            "ground_truth",
        )
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    finally:
        connection.close()


__all__ = ["SEED_USERS", "SeedResult", "database_overview", "seed_reference_data"]
