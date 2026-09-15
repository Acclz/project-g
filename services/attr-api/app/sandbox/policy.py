"""沙箱策略：SQL 与 Python 的准入判定（技术规格 §6.2 的第 2、4 道约束）。

两侧各判一次，规则只有这一份：

* **父进程先判**——被拦下的执行不启动子进程，最快也最省资源，拦截原因直接进审计；
* **子进程再判一次**——防止有人绕过 runner 直接喂给子进程（纵深防御，不是重复劳动）。

判定原则是**白名单优先**：只放行明确允许的东西。拒绝时给出可读原因，原因原样写进
``sandbox_runs.blocked_reason``，对抗测试就是拿这些原因做断言的。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

SQL_DIALECT = "sqlite"

#: 允许作为顶层语句的类型（sqlglot 的 ``key`` 是小写类名）
ALLOWED_ROOT_KEYS = frozenset({"select", "union", "subquery"})

#: 禁止出现的 AST 节点：DDL / DML / 事务 / 挂库 / 命令
FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Create",
    "Drop",
    "Alter",
    "Attach",
    "Detach",
    "Pragma",
    "Command",
    "Transaction",
    "Commit",
    "Rollback",
    "Grant",
    "Revoke",
    "Copy",
    "Merge",
    "TruncateTable",
    "Set",
    "Use",
    "Analyze",
    "Vacuum",
    "LoadData",
    "Into",
)
FORBIDDEN_NODE_TYPES = tuple(
    node_type
    for name in FORBIDDEN_NODE_NAMES
    if (node_type := getattr(exp, name, None)) is not None
)

#: 禁止调用的 SQL 函数：SQLite 的文件读写与扩展加载
FORBIDDEN_SQL_FUNCTIONS = frozenset(
    {
        "readfile",
        "writefile",
        "load_extension",
        "fts3_tokenizer",
        "edit",
        "sqlite_readfile",
    }
)

#: 数仓之外的 schema 一律不放行（SQLite 里 ``main`` 就是数仓本身，别的一律拒绝）
ALLOWED_SCHEMAS = frozenset({"", "dw"})

#: 单条语句/脚本的长度上限：既防"塞进一整个脚本"，也让子进程的 stdin 写入不会撑爆管道
MAX_CODE_LENGTH = 65_536


@dataclass(frozen=True)
class Decision:
    """准入判定结果：``ok=False`` 时 ``reason`` 就是审计里的拦截原因。"""

    ok: bool
    reason: str = ""
    detail: str = ""


def check_sql(sql: str, allowed_tables: set[str] | frozenset[str]) -> Decision:
    """校验一条 SQL：必须是**单条 SELECT**，且只引用数仓白名单里的表。

    多语句、注释绕过、DDL/DML、挂库命令、文件函数都在这里被拦下——判定基于 AST，
    所以靠大小写改写、加注释、换行拼接都绕不过去。
    """

    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return Decision(False, "SQL 为空")
    if len(text) > MAX_CODE_LENGTH:
        return Decision(False, f"SQL 过长（{len(text)} > {MAX_CODE_LENGTH} 字符）")
    try:
        parsed = [statement for statement in sqlglot.parse(text, read=SQL_DIALECT) if statement]
    except ParseError as error:
        return Decision(False, f"SQL 解析失败：{error}")
    if len(parsed) != 1:
        return Decision(
            False,
            f"只允许单条 SELECT，检测到 {len(parsed)} 条语句（多语句与注释绕过都在此处拦下）",
        )
    root = parsed[0]
    if root.key not in ALLOWED_ROOT_KEYS:
        return Decision(False, f"只允许 SELECT 查询，实际是 {type(root).__name__}")

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            return Decision(False, f"禁止的语句类型：{type(node).__name__}")
        if isinstance(node, exp.Func):
            hits = _function_names(node) & FORBIDDEN_SQL_FUNCTIONS
            if hits:
                return Decision(False, f"禁止调用的函数：{sorted(hits)[0]}()")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    known = {name.lower() for name in allowed_tables}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not name:
            continue
        if name in cte_names:
            continue
        if schema in ALLOWED_SCHEMAS and name in known:
            continue
        qualified = f"{schema}.{name}" if schema else name
        return Decision(False, f"表 {qualified} 不在数仓白名单内")
    return Decision(True)


def _function_names(node: exp.Func) -> set[str]:
    """取函数所有可能的写法名：sqlglot 对内置函数给 ``sql_name()``，对匿名函数给 ``name``。"""

    names = {str(getattr(node, "name", "") or "").lower()}
    try:
        names.add(str(node.sql_name()).lower())
    except AttributeError:
        pass
    return {name for name in names if name}


#: Python 侧允许导入的模块（技术规格 §6.2 的导入白名单 + 少量只读计算库）
ALLOWED_PYTHON_MODULES = frozenset(
    {
        "numpy",
        "pandas",
        "scipy",
        "math",
        "statistics",
        "decimal",
        "fractions",
        "datetime",
        "json",
        "re",
        "random",
        "itertools",
        "functools",
        "collections",
        "operator",
        "string",
        "time",
        "typing",
        "dataclasses",
    }
)

#: 明确禁止的模块：进程、文件、网络、动态加载
FORBIDDEN_PYTHON_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "socketserver",
        "shutil",
        "pathlib",
        "importlib",
        "ctypes",
        "multiprocessing",
        "threading",
        "concurrent",
        "asyncio",
        "signal",
        "pty",
        "fcntl",
        "msvcrt",
        "winreg",
        "builtins",
        "gc",
        "pickle",
        "marshal",
        "shelve",
        "dbm",
        "sqlite3",
        "tempfile",
        "glob",
        "io",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "webbrowser",
        "psutil",
        "platform",
        "getpass",
        "resource",
        "runpy",
        "code",
        "codeop",
        "pdb",
    }
)

#: 禁止直接调用的内建函数（逃逸与动态执行面）
FORBIDDEN_PYTHON_BUILTINS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "memoryview",
        "exit",
        "quit",
    }
)

#: 禁止访问的属性：典型沙箱逃逸链（``().__class__.__bases__[0].__subclasses__()``）
FORBIDDEN_PYTHON_ATTRIBUTES = frozenset(
    {
        "__class__",
        "__bases__",
        "__base__",
        "__subclasses__",
        "__mro__",
        "__globals__",
        "__getattribute__",
        "__builtins__",
        "__code__",
        "__closure__",
        "__loader__",
        "__spec__",
        "__reduce__",
        "__reduce_ex__",
        "__init_subclass__",
        "__subclasshook__",
    }
)


def check_python(code: str) -> Decision:
    """校验一段 Python：只允许导入白名单模块，禁止动态执行与逃逸链。

    判定同样基于 AST（``ast.parse``），因此字符串拼接、``__builtins__`` 取巧、
    ``().__class__`` 这类经典逃逸都会在语法树上被抓到。
    """

    text = (code or "").strip()
    if not text:
        return Decision(False, "Python 代码为空")
    if len(text) > MAX_CODE_LENGTH:
        return Decision(False, f"代码过长（{len(text)} > {MAX_CODE_LENGTH} 字符）")
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return Decision(False, f"Python 解析失败：{error}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                decision = _check_module(alias.name)
                if not decision.ok:
                    return decision
        elif isinstance(node, ast.ImportFrom):
            decision = _check_module(node.module or "")
            if not decision.ok:
                return decision
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_PYTHON_ATTRIBUTES:
                return Decision(False, f"禁止访问属性：{node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_PYTHON_BUILTINS:
                return Decision(False, f"禁止调用内建：{node.id}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_PYTHON_BUILTINS:
                return Decision(False, f"禁止调用内建：{func.id}")
    return Decision(True)


def _check_module(module: str) -> Decision:
    root = (module or "").split(".", 1)[0]
    if not root:
        return Decision(False, "相对导入不允许（沙箱脚本没有所属包）")
    if root in FORBIDDEN_PYTHON_MODULES:
        return Decision(False, f"禁止导入模块：{root}")
    if root not in ALLOWED_PYTHON_MODULES:
        return Decision(False, f"模块 {root} 不在导入白名单内")
    return Decision(True)


def check(kind: str, code: str, allowed_tables: set[str] | frozenset[str]) -> Decision:
    """按 ``kind`` 分发到 SQL 或 Python 判定（两侧共用同一入口）。"""

    if kind == "sql":
        return check_sql(code, allowed_tables)
    if kind == "python":
        return check_python(code)
    return Decision(False, f"不支持的执行类型：{kind}")
