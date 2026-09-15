# infra/sqlite：数仓初始化与只读验证

数据库从 PostgreSQL 改为 SQLite 之后，本目录承担两件事：

1. **初始化**：`services/attr-api/scripts/generate_warehouse.py` 直接建表建索引并生成数据，
   因此这里不再放 `init.sql`（避免出现第二份库结构定义）。
2. **只读验证**：`verify_readonly.py` 证明"沙箱拿到的 dw.db 连接物理上写不了"——
   这是 PostgreSQL 时代"只读角色 attr_ro"的替代证据，必须能当场跑。

```powershell
python infra/sqlite/verify_readonly.py
```

校验项：`mode=ro` 打开成功、INSERT/UPDATE/DELETE/CREATE 全部被拒、`PRAGMA query_only` 生效、
`app.db` 的表在只读连接里不可见（因为根本没 ATTACH）。
