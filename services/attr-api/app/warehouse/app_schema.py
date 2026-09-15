"""业务与治理表（``app.db``）的唯一定义处。

对应技术规格 §3.2。P2 只用到 ``ground_truth``（真值清单）；其余表按分期在 P3 之后逐个启用，
但结构先按定稿落下来，避免后面各写一份。

两处值得单独说明：

* ``audit_logs`` 的**不可删改**用触发器实现：PostgreSQL 靠"收回 UPDATE/DELETE 权限"，
  SQLite 没有角色权限层，就用 ``RAISE(ABORT)`` 触发器顶上，并配一条自动化测试。
* ``ground_truth`` 是评测期望值的**唯一来源**：它由生成器的独立解析式写入，
  严禁用被测系统的分解结果回填（需求说明书 §5.12）。
"""

from __future__ import annotations

import sqlite3

TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('analyst', 'viewer', 'admin')),
        display_name  TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE metric_definitions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code       TEXT NOT NULL UNIQUE,
        name       TEXT NOT NULL,
        level      TEXT NOT NULL CHECK (level IN ('atomic', 'derived', 'composite')),
        formula    TEXT,
        unit       TEXT NOT NULL,
        precision  INTEGER NOT NULL DEFAULT 2,
        structure  TEXT NOT NULL CHECK (structure IN
                        ('additive', 'multiplicative', 'unresolvable')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE metric_versions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_id       INTEGER NOT NULL REFERENCES metric_definitions(id),
        version         INTEGER NOT NULL,
        definition_json TEXT NOT NULL,
        effective_from  TEXT NOT NULL,
        UNIQUE (metric_id, version)
    )
    """,
    """
    CREATE TABLE metric_tree_nodes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_id INTEGER NOT NULL REFERENCES metric_definitions(id),
        parent_id INTEGER REFERENCES metric_tree_nodes(id),
        expr      TEXT NOT NULL,
        method    TEXT NOT NULL CHECK (method IN ('diff', 'lmdi')),
        sort      INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        domain          TEXT NOT NULL,
        metric_id       INTEGER NOT NULL REFERENCES metric_definitions(id),
        caliber_version INTEGER NOT NULL,
        base_period     TEXT NOT NULL,
        current_period  TEXT NOT NULL,
        slice_json      TEXT NOT NULL,
        status          TEXT NOT NULL CHECK (status IN
                            ('created', 'analysing', 'awaiting_user', 'completed', 'failed')),
        created_by      INTEGER NOT NULL REFERENCES users(id),
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE session_steps (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES sessions(id),
        seq         INTEGER NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN
                        ('plan', 'query', 'drilldown', 'hypothesis', 'verify',
                         'event', 'whatif', 'report')),
        status      TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        UNIQUE (session_id, seq)
    )
    """,
    """
    CREATE TABLE hypotheses (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id         INTEGER NOT NULL REFERENCES sessions(id),
        statement          TEXT NOT NULL,
        expected_direction TEXT NOT NULL,
        code_draft         TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (status IN ('pending', 'verified', 'excluded')),
        confidence         REAL,
        reason             TEXT,
        evidence_json      TEXT
    )
    """,
    """
    CREATE TABLE evidence (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL REFERENCES sessions(id),
        hypothesis_id INTEGER REFERENCES hypotheses(id),
        kind          TEXT NOT NULL,
        sql_digest    TEXT NOT NULL,
        result_json   TEXT NOT NULL,
        sample_size   INTEGER NOT NULL,
        p_value       REAL,
        effect_size   REAL
    )
    """,
    """
    CREATE TABLE events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        type             TEXT NOT NULL,
        start_day        TEXT NOT NULL,
        end_day          TEXT,
        dimensions_json  TEXT NOT NULL,
        note             TEXT,
        deleted_at       TEXT
    )
    """,
    """
    CREATE TABLE sandbox_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        INTEGER REFERENCES sessions(id),
        step_id           INTEGER REFERENCES session_steps(id),
        language          TEXT NOT NULL CHECK (language IN ('sql', 'python')),
        statement_digest  TEXT NOT NULL,
        rows              INTEGER,
        duration_ms       INTEGER NOT NULL,
        exit_code         INTEGER NOT NULL,
        blocked_reason    TEXT,
        actor             TEXT NOT NULL,
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      INTEGER NOT NULL REFERENCES sessions(id),
        title           TEXT NOT NULL,
        structure_json  TEXT NOT NULL,
        status          TEXT NOT NULL CHECK (status IN ('draft', 'published')),
        caliber_version INTEGER NOT NULL,
        created_by      INTEGER NOT NULL REFERENCES users(id),
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE report_annotations (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL REFERENCES reports(id),
        anchor   TEXT NOT NULL,
        text     TEXT NOT NULL,
        author   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE export_jobs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id  INTEGER NOT NULL REFERENCES reports(id),
        format     TEXT NOT NULL CHECK (format IN ('md', 'pdf', 'xlsx')),
        status     TEXT NOT NULL,
        file_path  TEXT,
        checksum   TEXT
    )
    """,
    """
    CREATE TABLE audit_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        actor       TEXT NOT NULL,
        action      TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id   TEXT,
        detail_json TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE eval_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        label        TEXT NOT NULL,
        dataset      TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT
    )
    """,
    """
    CREATE TABLE ground_truth (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        scenario                    TEXT NOT NULL CHECK (scenario IN ('ecom', 'fmcg')),
        day                         TEXT NOT NULL,
        window_end                  TEXT NOT NULL,
        dimension_json              TEXT NOT NULL,
        factor                      TEXT NOT NULL,
        leaf_factor                 TEXT,
        injection_pct               REAL NOT NULL,
        factor_base_value           REAL,
        factor_injected_value       REAL,
        metric_counterfactual_cents INTEGER NOT NULL,
        metric_observed_cents       INTEGER NOT NULL,
        injected_contribution_cents INTEGER NOT NULL,
        observed_slice_metric_cents INTEGER NOT NULL,
        verify_sql                  TEXT NOT NULL,
        note                        TEXT
    )
    """,
)


# 审计表：结构上禁止修改与删除（技术规格 §3.2）。触发器是 SQLite 侧的等价实现。
TRIGGER_DDL: tuple[str, ...] = (
    """
    CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, '审计日志不可修改');
    END
    """,
    """
    CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, '审计日志不可删除');
    END
    """,
)

INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX idx_steps_session ON session_steps (session_id, seq)",
    "CREATE INDEX idx_sandbox_session ON sandbox_runs (session_id)",
    "CREATE INDEX idx_gt_scenario_day ON ground_truth (scenario, day)",
    "CREATE INDEX idx_events_window ON events (start_day, end_day)",
)


def create_app_schema(conn: sqlite3.Connection) -> None:
    """建业务表、审计触发器与索引；调用方负责提交。"""

    for statement in TABLE_DDL:
        conn.execute(statement)
    for statement in TRIGGER_DDL:
        conn.execute(statement)
    for statement in INDEX_DDL:
        conn.execute(statement)


def table_names() -> list[str]:
    """业务表名清单（沙箱白名单以外的一切表名，都不允许被 SQL 引用）。"""

    names: list[str] = []
    for statement in TABLE_DDL:
        names.append(statement.split("CREATE TABLE", 1)[1].split("(", 1)[0].strip())
    return names
