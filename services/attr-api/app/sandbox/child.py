"""沙箱子进程：一次执行一条请求，返回一条响应，执行完即退出。

对应技术规格 §6.1 的进程模型：

* 与主服务**不在同一进程**，由 ``runner`` 用 :mod:`subprocess` 启动独立解释器；
* 只通过 stdin/stdout 的 JSON 协议通信；环境变量只注入白名单（``SANDBOX_*``），
  数仓 DSN 由父进程按 ``app.db.warehouse_readonly_uri`` 生成后注入，子进程不自己拼；
* SQL：只读连接 + ``PRAGMA query_only`` + progress handler 限制扫描工作量 + ``fetchmany`` 限制行数；
* Python：**受限 builtins** 执行（不是普通 exec），并主动封死网络与沙箱目录之外的文件写入。

本文件里的 ``import os`` 是沙箱子进程**自己**要读白名单环境变量用的，不属于用户代码；
用户代码拿到的是下面 ``_safe_builtins()`` 构造的那一套。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from app.db import open_readonly_connection
from app.sandbox import policy
from app.sandbox.dw_tables import allowed_table_names

PROGRESS_INTERVAL = 5_000
DEFAULT_MAX_ROWS = 100_000
DEFAULT_SCAN_STEPS = 50_000_000
STDOUT_TAIL = 2_000


def main() -> int:
    """读 stdin 的请求、执行、把响应写到 stdout。"""

    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        _write(
            {"ok": False, "error": f"请求不是合法 JSON：{error}", "blocked_reason": "bad_request"}
        )
        return 1

    kind = str(request.get("kind", ""))
    code = str(request.get("code", ""))
    # 纵深防御：父进程已判过一次，这里用同一份策略再判一次
    decision = policy.check(kind, code, allowed_table_names())
    if not decision.ok:
        _write(
            {
                "ok": False,
                "error": f"策略拒绝：{decision.reason}",
                "blocked_reason": decision.reason,
            }
        )
        return 0

    if kind == "sql":
        response = _run_sql(request, code)
    elif kind == "python":
        response = _run_python(request, code)
    else:
        response = {"ok": False, "error": f"不支持的类型：{kind}", "blocked_reason": "bad_request"}
    _write(response)
    return 0


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()


# --------------------------------------------------------------------- SQL


def _run_sql(request: dict[str, Any], sql: str) -> dict[str, Any]:
    dsn = os.environ.get("SANDBOX_DW_DSN", "")
    if not dsn:
        return {"ok": False, "error": "沙箱缺少 SANDBOX_DW_DSN", "blocked_reason": "bad_request"}
    max_rows = int(request.get("max_rows") or DEFAULT_MAX_ROWS)
    scan_steps = int(request.get("scan_steps") or DEFAULT_SCAN_STEPS)
    counters = {"steps": 0}

    def _progress() -> int:
        counters["steps"] += PROGRESS_INTERVAL
        return 1 if counters["steps"] > scan_steps else 0

    try:
        connection = open_readonly_connection(dsn)
    except sqlite3.Error as error:
        return {"ok": False, "error": f"数仓打开失败：{error}", "blocked_reason": None}
    try:
        connection.set_progress_handler(_progress, PROGRESS_INTERVAL)
        cursor = connection.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [description[0] for description in (cursor.description or [])]
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            return {
                "ok": False,
                "error": f"扫描工作量超过上限（{scan_steps} 步）",
                "blocked_reason": f"扫描超限（>{scan_steps} 步）",
            }
        return {"ok": False, "error": f"SQL 执行失败：{error}", "blocked_reason": None}
    except sqlite3.Error as error:
        return {"ok": False, "error": f"SQL 执行失败：{error}", "blocked_reason": None}
    finally:
        connection.close()

    if len(rows) > max_rows:
        return {
            "ok": False,
            "error": f"返回行数超过上限（{max_rows} 行）",
            "blocked_reason": f"结果集超限（>{max_rows} 行）",
        }
    return {
        "ok": True,
        "rows": [list(row) for row in rows],
        "columns": columns,
        "rows_returned": len(rows),
        "scan_steps": counters["steps"],
        "error": None,
        "blocked_reason": None,
    }


# ------------------------------------------------------------------ Python


class BlockedError(Exception):
    """沙箱在运行时主动拦截（网络、越界文件写入）。"""


def _run_python(request: dict[str, Any], code: str) -> dict[str, Any]:
    work_dir = Path(request.get("work_dir") or os.environ.get("SANDBOX_TMP_DIR") or ".").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    capture = io.StringIO()
    namespace: dict[str, Any] = {
        "__name__": "__sandbox__",
        "__builtins__": _safe_builtins(work_dir, capture),
    }
    _block_network()

    try:
        exec(compile(code, "<sandbox>", "exec"), namespace)  # noqa: S102 - 沙箱执行是既定动作
    except BlockedError as blocked:
        return {"ok": False, "error": str(blocked), "blocked_reason": str(blocked)}
    except MemoryError:
        return {"ok": False, "error": "内存不足", "blocked_reason": "内存超限"}
    except Exception as error:  # noqa: BLE001 - 沙箱必须把任何异常变成结构化响应
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "blocked_reason": None}
    return {
        "ok": True,
        "result": _stringify(namespace.get("result")),
        "stdout": capture.getvalue()[-STDOUT_TAIL:],
        "error": None,
        "blocked_reason": None,
    }


def _safe_builtins(work_dir: Path, capture: io.StringIO) -> dict[str, Any]:
    """受限 builtins：只给计算型内建，``open`` 限沙箱目录，且没有 eval/exec/getattr。"""

    import builtins

    safe: dict[str, Any] = {}
    for name in (
        "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "divmod",
        "enumerate", "filter", "float", "format", "frozenset", "hash", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next",
        "ord", "pow", "range", "repr", "reversed", "round", "set", "slice", "sorted",
        "str", "sum", "tuple", "type", "zip", "ArithmeticError", "Exception",
        "IndexError", "KeyError", "LookupError", "NameError", "TypeError", "ValueError",
        "ZeroDivisionError",
    ):
        if hasattr(builtins, name):
            safe[name] = getattr(builtins, name)
    safe["print"] = lambda *args, **kwargs: capture.write(
        " ".join(str(arg) for arg in args) + "\n"
    )
    safe["__import__"] = _guarded_import(builtins)
    safe["open"] = _guarded_open(work_dir, builtins)
    return safe


def _guarded_import(builtins_module) -> Any:
    """白名单导入：只放行 ``policy.ALLOWED_PYTHON_MODULES`` 里的模块。"""

    def _import(name: str, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        decision = policy.check_python(f"import {name}")
        if not decision.ok:
            raise BlockedError(f"沙箱拦截导入：{decision.reason}")
        return builtins_module.__import__(name, globals, locals, fromlist, level)

    return _import


def _guarded_open(work_dir: Path, builtins_module) -> Any:
    """文件访问限定在沙箱临时目录：越界直接拦截。"""

    real_open = builtins_module.open

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        try:
            target = Path(file).resolve()
        except (TypeError, ValueError) as error:
            raise BlockedError(f"沙箱拦截文件访问：{file!r}") from error
        if work_dir != target and work_dir not in target.parents:
            raise BlockedError(f"沙箱拦截文件访问：{target} 不在沙箱临时目录内")
        return real_open(target, mode, *args, **kwargs)

    return _open


def _block_network() -> None:
    """把 socket 直接掐死：即便某条路径绕过导入白名单也连不出去。"""

    import socket as socket_module

    def _deny(*_args: Any, **_kwargs: Any):
        raise BlockedError("沙箱拦截网络访问：禁止建立套接字")

    socket_module.socket = _deny  # type: ignore[assignment]
    socket_module.create_connection = _deny  # type: ignore[assignment]
    socket_module.socketpair = _deny  # type: ignore[assignment]


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else repr(value)


if __name__ == "__main__":
    raise SystemExit(main())
