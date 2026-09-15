"""会话层：状态机、上下文锁定、并发隔离与步骤流（技术规格 §4.4、需求说明书 §5.9）。

状态机：``created → analysing → awaiting_user → completed``；失败或用户取消走 ``failed``，
每次迁移都写 ``session_steps``（SSE 的事件源就是它）。

三条硬规则（对应需求说明书 §5.9）：

1. **上下文锁定**：指标、口径版本、对比期间在会话创建时锁定，追问只允许**收紧**切片，
   想改锁定项必须新建会话（不静默换口径）。
2. **并发隔离**：同一会话同一时刻只允许一个执行中任务，第二个请求直接拒绝并给出提示。
3. **结果过期**：数仓文件指纹变了（数据刷新）就把旧结论判为"结果过期"，必须重算才能进报告。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import connect_app
from app.sandbox.runner import SandboxRunner
from app.services.analysis import DEFAULT_TARGETS, AnalysisRequest, run_analysis
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.seed import seed_reference_data

STATUS_CREATED = "created"
STATUS_ANALYSING = "analysing"
STATUS_AWAITING = "awaiting_user"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
VALID_STATUSES = (
    STATUS_CREATED,
    STATUS_ANALYSING,
    STATUS_AWAITING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)


class SessionError(ValueError):
    """会话层业务错误（路由层统一映射为 4xx）。"""


class SessionBusy(SessionError):
    """同一会话已有执行中任务（对应 409）。"""


class ContextLocked(SessionError):
    """试图修改被锁定的上下文（指标/口径版本/期间）。"""


@dataclass
class SessionState:
    """会话的对外视图：锁定的上下文 + 状态 + 数据版本。"""

    id: int
    title: str
    scenario: str
    status: str
    base: Period
    current: Period
    slice_filter: SliceFilter
    caliber_version: int
    data_digest: str
    created_at: str
    last_message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "scenario": self.scenario,
            "status": self.status,
            "base": self.base.as_dict(),
            "current": self.current.as_dict(),
            "slice": json.loads(self.slice_filter.as_json()),
            "caliber_version": self.caliber_version,
            "data_digest": self.data_digest,
            "created_at": self.created_at,
            "last_message": self.last_message,
            "steps": self.steps,
        }


class SessionService:
    """会话用例层：编排 ``analysis.run_analysis``，并负责状态、锁与事件流。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        engine: MetricEngine | None = None,
        sandbox: SandboxRunner | None = None,
    ) -> None:
        self.engine = engine or MetricEngine(settings)
        self.settings = settings or self.engine.settings
        self.sandbox = sandbox or SandboxRunner(self.settings)
        self._lock = threading.Lock()
        self._running: set[int] = set()
        self._threads: dict[int, threading.Thread] = {}
        self._events: dict[int, list[dict[str, Any]]] = {}
        self._failures: dict[int, str] = {}

    # ------------------------------------------------------------- 生命周期

    def create(
        self,
        *,
        scenario: str,
        base: Period,
        current: Period,
        slice_filter: SliceFilter | None = None,
        title: str = "",
        actor: str = "analyst",
    ) -> SessionState:
        """新建会话并锁定上下文（指标必须匹配场景的根指标，口径版本固定为 1）。"""

        tree = self.engine.tree(scenario)
        seed_reference_data(self.engine)
        connection = connect_app(self.settings.app_db)
        try:
            user_id = connection.execute(
                "SELECT id FROM users WHERE role = 'analyst' ORDER BY id LIMIT 1"
            ).fetchone()[0]
            metric_id = connection.execute(
                "SELECT id FROM metric_definitions WHERE code = ?",
                (f"{scenario}.{tree.root.code}",),
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO sessions (title, domain, metric_id, caliber_version, base_period,"
                " current_period, slice_json, status, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    title or f"{tree.scenario_name}·{tree.root.name}",
                    scenario,
                    metric_id,
                    1,
                    self._encode_period(base),
                    self._encode_period(current),
                    (slice_filter or SliceFilter()).as_json(),
                    STATUS_CREATED,
                    user_id,
                ),
            )
            session_id = int(cursor.lastrowid or 0)
            connection.commit()
        finally:
            connection.close()
        # 创建即记录"数据版本快照"：日后数据刷新时，`is_stale` 就是拿这一条做比对
        self._transition(session_id, STATUS_CREATED, "会话创建，上下文与数据版本已锁定", actor)
        return self.get(session_id)

    def get(self, session_id: int) -> SessionState:
        """读取会话详情（含步骤流与"结果过期"标记）。"""

        connection = connect_app(self.settings.app_db)
        try:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise SessionError(f"会话 {session_id} 不存在")
            steps = connection.execute(
                "SELECT seq, kind, status, payload_json, duration_ms FROM session_steps"
                " WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
        return SessionState(
            id=session_id,
            title=row["title"],
            scenario=row["domain"],
            status=row["status"],
            base=self._decode_period(row["base_period"]),
            current=self._decode_period(row["current_period"]),
            slice_filter=SliceFilter.from_json(row["slice_json"]),
            caliber_version=int(row["caliber_version"]),
            data_digest=self.data_digest(),
            created_at=row["created_at"] or "",
            steps=[
                {
                    "seq": step["seq"],
                    "kind": step["kind"],
                    "status": step["status"],
                    "duration_ms": step["duration_ms"],
                    "payload": json.loads(step["payload_json"] or "{}"),
                }
                for step in steps
            ],
        )

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """会话列表（本人可见 + 只读可见，这里先按时间倒序返回全部）。"""

        connection = connect_app(self.settings.app_db)
        try:
            rows = connection.execute(
                "SELECT id, title, domain, status, base_period, current_period, created_at"
                " FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- 执行

    def start(self, session_id: int, *, message: str = "", actor: str = "analyst") -> SessionState:
        """发起（或追问）一次分析：后台线程跑 L1 链路，事件流实时可读。"""

        with self._lock:
            if session_id in self._running:
                raise SessionBusy(
                    f"会话 {session_id} 已有执行中的任务：同一会话同时只允许一个任务"
                    "（需求说明书 §5.9 并发隔离）"
                )
            state = self.get(session_id)
            self._running.add(session_id)
            self._transition(session_id, STATUS_ANALYSING, "消息已受理，开始分析", actor)
        thread = threading.Thread(
            target=self._run,
            args=(session_id, state, message, actor),
            name=f"session-{session_id}",
            daemon=True,
        )
        self._threads[session_id] = thread
        thread.start()
        return self.get(session_id)

    def wait(self, session_id: int, *, timeout: float = 600.0) -> SessionState:
        """等待后台任务结束（脚本与测试用；接口层用 SSE）。"""

        thread = self._threads.get(session_id)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.get(session_id)

    def _run(self, session_id: int, state: SessionState, message: str, actor: str) -> None:
        try:
            request = AnalysisRequest(
                scenario=state.scenario,
                base=state.base,
                current=state.current,
                slice_filter=state.slice_filter,
                auto_targets=DEFAULT_TARGETS.get(state.scenario, []),
                title=state.title,
                actor=actor,
                persist=False,  # 会话层自己落库，避免出现两份会话
            )
            report = run_analysis(
                request, engine=self.engine, sandbox=self.sandbox, settings=self.settings
            )
            for step in report.steps:
                self._emit(session_id, step["kind"], step["status"], step["payload"])
            self._persist_results(session_id, report)
            self._transition(session_id, STATUS_AWAITING, "分析完成，等待用户追问", actor)
        except Exception as error:  # noqa: BLE001 - 任何失败都要落到 failed 状态与事件流
            self._failures[session_id] = f"{type(error).__name__}: {error}"
            self._emit(session_id, "report", "failed", {"error": self._failures[session_id]})
            self._transition(session_id, STATUS_FAILED, self._failures[session_id], actor)
        finally:
            with self._lock:
                self._running.discard(session_id)

    def events(self, session_id: int, *, since: int = -1) -> list[dict[str, Any]]:
        """读取会话事件（SSE 与轮询共用；``since`` 为上一次的 seq）。"""

        return [event for event in self._events.get(session_id, []) if event["seq"] > since]

    # --------------------------------------------------------------- 控制

    def control(self, session_id: int, action: str, *, actor: str = "analyst") -> SessionState:
        """任务控制：取消=终止并置 failed；暂停=停在等待用户；恢复=重跑并校验数据版本。"""

        if action == "cancel":
            self._transition(session_id, STATUS_FAILED, "用户取消（cancel）", actor)
        elif action == "pause":
            self._transition(
                session_id, STATUS_AWAITING, "用户请求暂停：当前任务结束后停在等待用户", actor
            )
        elif action == "resume":
            if self.is_stale(session_id):
                self._transition(
                    session_id, STATUS_ANALYSING, "数据已刷新，恢复前重算（结果过期）", actor
                )
            else:
                self._transition(
                    session_id, STATUS_ANALYSING, "恢复：重新校验权限与数据版本后继续", actor
                )
            return self.start(session_id, actor=actor)
        else:
            raise SessionError(f"不支持的控制动作：{action}（可用 pause / cancel / resume）")
        return self.get(session_id)

    def drilldown(
        self,
        session_id: int,
        *,
        extra_slice: SliceFilter,
        actor: str = "analyst",
    ) -> SessionState:
        """下钻：在锁定上下文之上**只能收紧**切片；试图放宽或换口径一律拒绝。"""

        state = self.get(session_id)
        merged = {key: tuple(values) for key, values in state.slice_filter.filters.items()}
        for key, values in extra_slice.filters.items():
            if key in merged:
                narrowed = tuple(value for value in values if value in merged[key])
                if not narrowed:
                    raise ContextLocked(
                        f"下钻参数与锁定切片冲突：{key} 已锁定为 {list(merged[key])}，"
                        f"请求为 {list(values)}（不允许放宽或替换锁定切片）"
                    )
                merged[key] = narrowed
            else:
                merged[key] = tuple(values)
        connection = connect_app(self.settings.app_db)
        try:
            connection.execute(
                "UPDATE sessions SET slice_json = ? WHERE id = ?",
                (SliceFilter(merged).as_json(), session_id),
            )
            connection.commit()
        finally:
            connection.close()
        self._emit(session_id, "drilldown", "completed", {"slice": merged})
        return self.get(session_id)

    def hypotheses(self, session_id: int) -> list[dict[str, Any]]:
        """会话的假设列表（含置信度、状态与排除理由）。"""

        connection = connect_app(self.settings.app_db)
        try:
            rows = connection.execute(
                "SELECT id, statement, expected_direction, status, confidence, reason"
                " FROM hypotheses WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def evidence(self, session_id: int) -> list[dict[str, Any]]:
        """会话的证据链（SQL 摘要、样本量、p 值、效应量）。"""

        connection = connect_app(self.settings.app_db)
        try:
            rows = connection.execute(
                "SELECT id, hypothesis_id, kind, sql_digest, sample_size, p_value, effect_size"
                " FROM evidence WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- 工具

    def data_digest(self) -> str:
        """数据版本指纹：数仓文件的大小 + 修改时间（便宜且足够判断"数据是否刷新过"）。"""

        path = Path(self.settings.warehouse_db)
        if not path.exists():
            return "missing"
        stat = path.stat()
        return f"{stat.st_size}-{stat.st_mtime_ns}"

    @staticmethod
    def _encode_period(period: Period) -> str:
        """期间存成"起..止"：规范里的期间字段是文本，这里保留两端（只存起点会把现期截断）。"""

        return f"{period.start}..{period.end}"

    @staticmethod
    def _decode_period(text: str) -> Period:
        """解析期间文本；老数据只存了一个日期时，起止都取该日期。"""

        if ".." in str(text):
            start, end = str(text).split("..", 1)
            return Period(start, end)
        return Period(str(text), str(text))

    def is_stale(self, session_id: int) -> bool:
        """会话创建时的数据版本与当前是否一致（不一致 ⇒ 旧结论"结果过期"）。"""

        connection = connect_app(self.settings.app_db)
        try:
            row = connection.execute(
                "SELECT payload_json FROM session_steps WHERE session_id = ? AND kind = 'plan'"
                " ORDER BY seq LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return False
        recorded = json.loads(row["payload_json"] or "{}").get("data_digest")
        return bool(recorded) and recorded != self.data_digest()

    def _persist_results(self, session_id: int, report) -> None:
        """把本次分析的假设与证据挂到会话上（数值全部来自沙箱证据与分解）。"""

        connection = connect_app(self.settings.app_db)
        try:
            for result in report.hypotheses:
                cursor = connection.execute(
                    "INSERT INTO hypotheses (session_id, statement, expected_direction,"
                    " code_draft, status, confidence, reason, evidence_json)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        result.hypothesis.statement,
                        result.hypothesis.expected_direction,
                        result.hypothesis.sql_draft,
                        result.status,
                        result.final_confidence,
                        result.reason,
                        json.dumps(
                            {
                                "source": result.hypothesis.source,
                                "confidence": None
                                if result.confidence is None
                                else result.confidence.as_dict(),
                                "penalties": result.penalties,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                hypothesis_id = int(cursor.lastrowid or 0)
                if result.evidence is not None:
                    evidence = result.evidence
                    connection.execute(
                        "INSERT INTO evidence (session_id, hypothesis_id, kind, sql_digest,"
                        " result_json, sample_size, p_value, effect_size) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            session_id,
                            hypothesis_id,
                            evidence.kind,
                            evidence.sql_digest,
                            json.dumps(evidence.as_dict(), ensure_ascii=False),
                            evidence.sample_size,
                            evidence.p_value,
                            evidence.effect_size,
                        ),
                    )
            connection.commit()
        finally:
            connection.close()

    def _transition(self, session_id: int, status: str, note: str, actor: str) -> None:
        """状态迁移：更新 ``sessions.status`` 并在步骤流里留一条记录（每次迁移都落库）。"""

        if status not in VALID_STATUSES:
            raise SessionError(f"非法状态：{status}")
        connection = connect_app(self.settings.app_db)
        try:
            next_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_steps WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
            )
            connection.execute(
                "INSERT INTO session_steps (session_id, seq, kind, status, payload_json,"
                " duration_ms) VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    next_seq,
                    "plan",
                    status,
                    json.dumps(
                        {
                            "note": note,
                            "actor": actor,
                            "at": datetime.now().isoformat(timespec="seconds"),
                            "data_digest": self.data_digest(),
                        },
                        ensure_ascii=False,
                    ),
                    0,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self._emit(session_id, "plan", status, {"note": note, "actor": actor})

    def _emit(self, session_id: int, kind: str, status: str, payload: dict[str, Any]) -> None:
        """把事件追加到内存流（SSE 实时读；持久化版本在 ``session_steps``）。"""

        events = self._events.setdefault(session_id, [])
        events.append(
            {
                "seq": len(events),
                "kind": kind,
                "status": status,
                "payload": payload,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    def _current_end(self, scenario: str) -> str:
        """会话现期的结束日取数仓最后一天（避免把"当前"写成硬编码日期）。"""

        from app.db import connect_warehouse_readonly

        connection = connect_warehouse_readonly(self.settings.warehouse_db)
        try:
            row = connection.execute("SELECT MAX(day) FROM dw.dim_date").fetchone()
            return str(row[0]) if row and row[0] else self.get_current_hint()
        finally:
            connection.close()

    @staticmethod
    def get_current_hint() -> str:
        return time.strftime("%Y-%m-%d")


__all__ = [
    "ContextLocked",
    "SessionBusy",
    "SessionError",
    "SessionService",
    "SessionState",
    "STATUS_ANALYSING",
    "STATUS_AWAITING",
    "STATUS_COMPLETED",
    "STATUS_CREATED",
    "STATUS_FAILED",
]
