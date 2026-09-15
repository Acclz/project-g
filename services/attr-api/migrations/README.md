# migrations 目录

业务表（`app.db`）的迁移目录，预留给 Alembic。

**P2 没有启用 Alembic**，原因写清楚不留含糊：P2 的出口条件是"数仓可复现生成 + 指标树可配置"，
业务表结构由 `app/warehouse/app_schema.py` 一次性建出来（唯一来源，见该文件 docstring）。
引入 Alembic 的时机是 **P3**——只要出现第一张需要演进（加字段/改约束）的业务表，就必须接迁移，
而不是靠"删库重建"。

启用步骤（P3 开工时执行一次）：

```powershell
cd services\attr-api
alembic init migrations
# 把 app/warehouse/app_schema.py 的表结构登记为首个版本，再 upgrade head
```
