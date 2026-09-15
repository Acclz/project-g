# 归因工作台前端（apps/attr-web）

技术规格 §8 的落地：**Vue 3 + Vite + Vue Router + Pinia + ECharts**，五个页面覆盖
"异动 → 归因 → 交付"的全过程（需求说明书 §4）。

## 跑起来

```powershell
# 1) 后端（仓库根目录）
cd services\attr-api
$env:PYTHONPATH="..\..\services\attr-api;..\..\packages\attribution"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) 前端
cd apps\attr-web
npm install
npm run dev        # http://127.0.0.1:5173（/api 反向代理到 8000）
```

数仓要先有数据：`python services\attr-api\scripts\generate_warehouse.py --profile full --reset`。

## 五个页面（对应需求说明书 §4）

| 页面 | 路由 | 主要能力 |
| --- | --- | --- |
| 指标异动大盘 | `/#/dashboard` | 指标卡片（当前值 / 环比 / 与基线带的位置）、异动清单（Z 分数 + 是否已发起归因）、趋势图 + 基线带、一键发起归因 |
| 归因分析工作台 | `/#/workbench` | 三栏：会话与锁定上下文 / SSE 步骤流与追问、下钻、推演控制 / 瀑布图、透视表、假设证据卡、What-If 曲线、一键出报告 |
| 业务事件日历 | `/#/events` | 事件列表与筛选、新建、软删除、按会话窗口匹配（命中 / 待观察） |
| 指标字典配置 | `/#/metrics` | 场景 → 指标节点（层级 / 结构 / 方法 / 口径）、拆解树、逐节点口径校验 |
| 报告归档与导出 | `/#/reports` | 七段预览、段落级批注、导出 MD / PDF / Excel 并显示同源校验和 |

## 前端只做展示，不做数值加工（红线）

* 贡献额、覆盖率、弹性区间、校验和等数值**全部由后端返回**（数仓 + `packages/attribution`）；
  前端不重算、不四舍五入后再用于判断，需要显示什么就显示接口给的那个字段。
* 图表（ECharts）只负责把后端的贡献额画成瀑布图、把区间画成带子，不做统计推断。
* 报告预览与三种导出指向同一份 `structure_json`，页面直接展示后端给出的内容校验和。

## 界面冒烟（三条链路在界面真跑通）

```powershell
# 前后端都在跑的前提下
npm run smoke
```

脚本用系统已安装的 Edge/Chrome（`playwright-core`，不下载浏览器）走一遍：
大盘 → 发起归因（L1：拆解 / 假设 / 验证 / 事件）→ 下钻（L2）→ 推演（L3）→
七段报告预览 → 三格式导出与校验和一致 → 事件日历 → 指标字典。
每一步都以**后端给出的数字**为判据（覆盖率、弹性区间、校验和），
截图落在 `output/playwright/`（不进版本库）。
