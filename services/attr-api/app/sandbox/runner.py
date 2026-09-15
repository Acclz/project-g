"""沙箱执行器（父进程侧）：策略判定 → 受限子进程 → 配额监控 → 全量审计。

一次执行的完整链路（技术规格 §6.1/§6.2）：

1. **策略判定**（父进程）：SQL 走 AST 白名单、Python 走导入/内建/属性白名单。被拦下的执行
   不启动子进程，但**照样写审计**——"被拦了多少次"本身就是安全指标。
2. **受限子进程**：只注入白名单环境变量（``SANDBOX_*``），数仓 DSN 用只读串，
   工作目录限定在沙箱临时目录。
3. **配额监控**：硬超时（terminate→kill）、内存轮询、CPU 时间、结果行数与扫描步数，
   任一超限即杀进程并记录原因。
4. **审计留痕**：每次执行（含被拦截的）写一条 ``sandbox_runs``，只存 ``statement_digest`` 不存原文。

隔离等级与残余风险按 §6.3 如实声明：这是**进程级**隔离，不是容器或虚拟机级。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from app.config import Settings, get_settings
from app.db import connect_app, warehouse_readonly_uri
from app.sandbox import policy
from app.sandbox.dw_tables import allowed_table_names
from app.warehouse import app_schema

POLL_INTERVAL_SECONDS = 0.05
STDERR_TAIL = 400
DIGEST_LENGTH = 16

#: 子进程允许继承的环境变量（其余一概不传，尤其是密钥类）
ENV_ALLOWLIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


@dataclass(frozen=True)
class Quotas:
    """资源配额：任一超限都会被硬杀并记录原因。"""

    timeout_seconds: float = 20.0
    max_rows: int = 100_000
    memory_mb: int = 768
    cpu_seconds: float = 15.0
    scan_steps: int = 50_000_000


@dataclass
class SandboxResult:
    """一次沙箱执行的结果（字段与 ``sandbox_runs`` 一一对应，便于回放审计）。"""

    ok: bool
    language: str
    statement_digest: str
    rows: list[list[Any]] | None = None
    columns: list[str] | None = None
    result: str | None = None
    stdout: str = ""
    error: str | None = None
    blocked_reason: str | None = None
    duration_ms: int = 0
    rows_returned: int = 0
    memory_peak_mb: float = 0.0
    exit_code: int | None = None
    audit_id: int | None = None
    scan_steps: int = 0

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "language": self.language,
            "statement_digest": self.statement_digest,
            "blocked_reason": self.blocked_reason,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "rows_returned": self.rows_returned,
            "memory_peak_mb": round(self.memory_peak_mb, 2),
            "exit_code": self.exit_code,
            "audit_id": self.audit_id,
        }
        if self.rows is not None:
            payload["rows"] = self.rows
            payload["columns"] = self.columns or []
        if self.result is not None:
            payload["result"] = self.result
        if self.stdout:
            payload["stdout"] = self.stdout
        return payload


class SandboxRunner:
    """沙箱执行器。服务进程与脚本共用同一个入口，禁止绕过它直接执行外部代码。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        service_root: Path | None = None,
        audit: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.service_root = service_root or Path(__file__).resolve().parents[2]
        self.work_dir = self.settings.resolve_path(self.settings.sandbox_tmp_dir)
        self.audit_enabled = audit
        self._schema_ready = False

    # ------------------------------------------------------------ 公开入口

    def run_sql(
        self,
        sql: str,
        *,
        quotas: Quotas | None = None,
        actor: str = "system",
        session_id: int | None = None,
        step_id: int | None = None,
    ) -> SandboxResult:
        """在沙箱里执行一条 SELECT（其它语句一律拒绝）。"""

        return self.run(
            "sql",
            sql,
            quotas=quotas,
            actor=actor,
            session_id=session_id,
            step_id=step_id,
        )

    def run_python(
        self,
        code: str,
        *,
        quotas: Quotas | None = None,
        actor: str = "system",
        session_id: int | None = None,
        step_id: int | None = None,
    ) -> SandboxResult:
        """在沙箱里执行一段数值计算代码（无网络、无越界文件、受限内建）。"""

        return self.run(
            "python",
            code,
            quotas=quotas,
            actor=actor,
            session_id=session_id,
            step_id=step_id,
        )

    def run(
        self,
        kind: str,
        code: str,
        *,
        quotas: Quotas | None = None,
        actor: str = "system",
        session_id: int | None = None,
        step_id: int | None = None,
    ) -> SandboxResult:
        """统一执行入口：判定 → 执行 → 审计。任何异常都会变成结构化结果，不留裸异常。"""

        active = quotas or self.default_quotas()
        digest = digest_of(code)
        decision = policy.check(kind, code, allowed_table_names())
        if not decision.ok:
            return self._finish(
                SandboxResult(
                    ok=False,
                    language=kind,
                    statement_digest=digest,
                    error=f"策略拒绝：{decision.reason}",
                    blocked_reason=decision.reason,
                    duration_ms=0,
                    exit_code=None,
                ),
                actor=actor,
                session_id=session_id,
                step_id=step_id,
            )

        return self._spawn_and_monitor(
            kind=kind,
            code=code,
            digest=digest,
            quotas=active,
            actor=actor,
            session_id=session_id,
            step_id=step_id,
        )

    def default_quotas(self) -> Quotas:
        """把 ``SANDBOX_*`` 配置翻译成配额对象（配置是唯一来源）。"""

        return Quotas(
            timeout_seconds=float(self.settings.sandbox_timeout_seconds),
            max_rows=int(self.settings.sandbox_max_rows),
            memory_mb=int(self.settings.sandbox_memory_mb),
            cpu_seconds=float(self.settings.sandbox_cpu_seconds),
            scan_steps=int(self.settings.sandbox_max_rows) * 500,
        )

    # -------------------------------------------------------------- 执行体

    def _spawn_and_monitor(
        self,
        *,
        kind: str,
        code: str,
        digest: str,
        quotas: Quotas,
        actor: str,
        session_id: int | None,
        step_id: int | None,
    ) -> SandboxResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "kind": kind,
            "code": code,
            "max_rows": quotas.max_rows,
            "scan_steps": quotas.scan_steps,
            "work_dir": str(self.work_dir),
        }
        started = time.perf_counter()
        process = subprocess.Popen(  # noqa: S603 - 参数固定，无 shell，路径来自配置
            [sys.executable, "-m", "app.sandbox.child"],
            cwd=str(self.service_root),
            env=self._child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        readers = [
            threading.Thread(target=lambda: stdout_chunks.append(process.stdout.read() or "")),
            threading.Thread(target=lambda: stderr_chunks.append(process.stderr.read() or "")),
        ]
        for reader in readers:
            reader.daemon = True
            reader.start()

        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False))
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

        killed_reason: str | None = None
        peak_mb = 0.0
        deadline = started + quotas.timeout_seconds
        while True:
            if process.poll() is not None:
                break
            peak_mb, killed_reason = self._check_quotas(process, peak_mb, quotas)
            if killed_reason:
                _terminate(process)
                break
            if time.perf_counter() > deadline:
                killed_reason = f"超时（>{quotas.timeout_seconds:g}s）"
                _terminate(process)
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        for reader in readers:
            reader.join(timeout=2.0)
        duration_ms = int((time.perf_counter() - started) * 1000)
        exit_code = process.returncode
        stdout_text = "".join(stdout_chunks).strip()
        stderr_text = "".join(stderr_chunks).strip()[-STDERR_TAIL:]

        if killed_reason:
            return self._finish(
                SandboxResult(
                    ok=False,
                    language=kind,
                    statement_digest=digest,
                    error=f"沙箱终止执行：{killed_reason}",
                    blocked_reason=killed_reason,
                    duration_ms=duration_ms,
                    memory_peak_mb=peak_mb,
                    exit_code=exit_code,
                ),
                actor=actor,
                session_id=session_id,
                step_id=step_id,
            )

        payload = _parse_response(stdout_text)
        if payload is None:
            return self._finish(
                SandboxResult(
                    ok=False,
                    language=kind,
                    statement_digest=digest,
                    error=f"子进程未返回合法响应（exit={exit_code}）：{stderr_text}",
                    blocked_reason=None,
                    duration_ms=duration_ms,
                    memory_peak_mb=peak_mb,
                    exit_code=exit_code,
                ),
                actor=actor,
                session_id=session_id,
                step_id=step_id,
            )

        return self._finish(
            SandboxResult(
                ok=bool(payload.get("ok")),
                language=kind,
                statement_digest=digest,
                rows=payload.get("rows"),
                columns=payload.get("columns"),
                result=payload.get("result"),
                stdout=payload.get("stdout") or "",
                error=payload.get("error"),
                blocked_reason=payload.get("blocked_reason"),
                duration_ms=duration_ms,
                rows_returned=int(payload.get("rows_returned") or 0),
                memory_peak_mb=peak_mb,
                exit_code=exit_code,
                scan_steps=int(payload.get("scan_steps") or 0),
            ),
            actor=actor,
            session_id=session_id,
            step_id=step_id,
        )

    def _check_quotas(
        self, process: subprocess.Popen, peak_mb: float, quotas: Quotas
    ) -> tuple[float, str | None]:
        """轮询内存与 CPU：Windows 上没有 rlimit，只能靠这里兜底（§6.3 已声明）。"""

        try:
            handle = psutil.Process(process.pid)
            rss_mb = handle.memory_info().rss / 1024 / 1024
            cpu_seconds = sum(handle.cpu_times()[:2])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return peak_mb, None
        peak_mb = max(peak_mb, rss_mb)
        if rss_mb > quotas.memory_mb:
            return peak_mb, f"内存超限（>{quotas.memory_mb} MB）"
        if cpu_seconds > quotas.cpu_seconds:
            return peak_mb, f"CPU 超限（>{quotas.cpu_seconds:g}s）"
        return peak_mb, None

    def _child_env(self) -> dict[str, str]:
        """只注入白名单环境变量 + 只读 DSN（密钥、密钥库路径一概不传）。"""

        env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
        env.update(
            {
                "SANDBOX_DW_DSN": warehouse_readonly_uri(self.settings.warehouse_db),
                "SANDBOX_TMP_DIR": str(self.work_dir),
                "SANDBOX_TIMEOUT_SECONDS": str(self.settings.sandbox_timeout_seconds),
                "SANDBOX_MAX_ROWS": str(self.settings.sandbox_max_rows),
                "SANDBOX_MEMORY_MB": str(self.settings.sandbox_memory_mb),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    # ---------------------------------------------------------------- 审计

    def _finish(
        self,
        result: SandboxResult,
        *,
        actor: str,
        session_id: int | None,
        step_id: int | None,
    ) -> SandboxResult:
        """写审计并返回结果。审计表是"不可删改"表，写入失败说明治理链路有问题，直接抛出。"""

        if self.audit_enabled:
            result.audit_id = self._audit(
                result=result, actor=actor, session_id=session_id, step_id=step_id
            )
        return result

    def _audit(
        self,
        *,
        result: SandboxResult,
        actor: str,
        session_id: int | None,
        step_id: int | None,
    ) -> int:
        connection = connect_app(self.settings.app_db)
        try:
            if not self._schema_ready:
                self._ensure_schema(connection)
            cursor = connection.execute(
                "INSERT INTO sandbox_runs (session_id, step_id, language, statement_digest,"
                " rows, duration_ms, exit_code, blocked_reason, actor)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    step_id,
                    result.language,
                    result.statement_digest,
                    result.rows_returned,
                    result.duration_ms,
                    result.exit_code if result.exit_code is not None else 0,
                    result.blocked_reason,
                    actor,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)
        finally:
            connection.close()

    def _ensure_schema(self, connection) -> None:
        """首次执行时确保业务表存在（P3 尚未引入 Alembic，见 migrations/README.md）。"""

        found = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sandbox_runs'"
        ).fetchone()
        if not found:
            app_schema.create_app_schema(connection)
            connection.commit()
        self._schema_ready = True


def digest_of(code: str) -> str:
    """语句摘要：审计里只留摘要不留原文（技术规格 §9 的日志脱敏要求）。"""

    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


def _terminate(process: subprocess.Popen) -> None:
    """先 terminate 再 kill：给子进程一个自己退出的机会，超时则强杀。"""

    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _parse_response(stdout_text: str) -> dict[str, Any] | None:
    if not stdout_text:
        return None
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
