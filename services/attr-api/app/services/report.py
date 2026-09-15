"""七段式报告：单一中间态（需求说明书 §9、§5.11；技术规格 §4.6）。

一份报告 = 一个结构化中间态（`structure_json`）。Markdown / PDF / Excel 三种导出与页面预览
**全部由这同一份中间态渲染**，因此"页面、MD、PDF、Excel 字段级一致"不是靠人工核对保证的，
而是结构上就只有一份数据（校验和见 `app/services/export.py`）。

七段与必备字段（§9）：

| 段 | 内容 | 必备字段 |
| --- | --- | --- |
| 1 异动背景与问题定义 | 目标指标、期间、基期/现期值、总差异额与差异率 | 口径版本、对比方式 |
| 2 因子贡献率拆解 | 各因子绝对贡献、贡献率、拉动方向 | 残差显式为 0（或标注未通过） |
| 3 细分维度下钻证据 | TOP 维度组合、透视表、关键变化特征 | TOP N 的 N 值与覆盖率 |
| 4 核心归因结论 | 首要原因、次要原因、内外部诱因划分 | 每条结论绑定贡献额 |
| 5 因果证据链与检验置信度 | 数据验证、沙箱摘要、显著性、伪相关排除 | 检验方法与样本量 |
| 6 What-If 情景模拟结论 | 目标指标预估区间与前提条件 | 区间、假设条件、把握度 |
| 7 可落地策略行动清单 | 业务动作、责任岗位、预估周期、预期收益区间 | 每条行动绑定驱动因子 |

数值来源只有三个：数仓（`MetricEngine`）、算法包（`packages/attribution`）、
以及会话里已经落库的推演记录；模型文本永远不进数值字段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT, Settings
from app.db import connect_app
from app.sandbox.runner import SandboxRunner
from app.services.analysis import DEFAULT_TARGETS
from app.services.anomaly import judge
from app.services.decomposition import (
    DimensionPivot,
    MetricEngine,
    Period,
    SliceFilter,
)
from app.services.drilldown import default_dimensions
from app.services.events import load_events_from_db, match_events, sync_seed_events
from app.services.export import (
    RENDERERS,
    ExportError,
    content_checksum,
    render,
    verify_consistency,
)
from app.services.sessions import SessionService
from attribution import assert_conservation

PLAYBOOK_PATH = REPO_ROOT / "corpus" / "report" / "action_playbook.yaml"

#: 七段顺序**不可变**（§5.11 第 1 条）
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("background", "异动背景与问题定义"),
    ("factor_contributions", "因子贡献率拆解"),
    ("drilldown_evidence", "细分维度下钻证据"),
    ("conclusions", "核心归因结论"),
    ("evidence_chain", "因果证据链与检验置信度"),
    ("whatif", "What-If 情景模拟结论"),
    ("actions", "可落地策略行动清单"),
)


class ReportError(ValueError):
    """报告组装失败（会话不存在、结果过期、数仓缺失等）。"""


@dataclass
class Segment:
    """报告的一段：必备字段 + 人话段落 + 数据来源与统计期间（§5.11）。"""

    index: int
    code: str
    title: str
    source: str
    period: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def add(self, label: str, value: Any, display: str) -> None:
        self.fields.append({"label": label, "value": value, "display": display})

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "code": self.code,
            "title": self.title,
            "source": self.source,
            "period": self.period,
            "fields": self.fields,
            "body": self.body,
            "data": self.data,
        }


def _format_amount(value: float | None, unit: str = "分") -> str:
    """金额类取值带单位；深层节点的口径不是金额时传 ``unit=""``（见 ``_contributions``）。"""

    if value is None:
        return "—"
    return f"{value:,.0f} {unit}".rstrip()


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}"


def load_playbook(path: Path | None = None) -> dict[str, Any]:
    """读取行动项台账（责任岗位 / 周期为项目自定参数，文件顶部已声明）。"""

    raw = yaml.safe_load(Path(path or PLAYBOOK_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "factors" not in raw:
        raise ReportError("行动项台账缺少 factors 段")
    return raw


@dataclass
class ReportService:
    """报告用例层：组装中间态、落库归档、批注与导出（导出的渲染在 export.py）。"""

    settings: Settings | None = None
    engine: MetricEngine | None = None
    sandbox: SandboxRunner | None = None
    sessions: SessionService | None = None

    def __post_init__(self) -> None:
        self.engine = self.engine or MetricEngine(self.settings)
        self.settings = self.settings or self.engine.settings
        self.sandbox = self.sandbox or SandboxRunner(self.settings)
        self.sessions = self.sessions or SessionService(
            self.settings, engine=self.engine, sandbox=self.sandbox
        )

    # ------------------------------------------------------------ 组装中间态

    def build(self, session_id: int, *, actor: str = "analyst") -> dict[str, Any]:
        """把会话里的中间结果组装成七段式报告结构（不落库）。"""

        state = self.sessions.get(session_id)
        if self.sessions.is_stale(session_id):
            raise ReportError(
                "会话的数据版本与当前数仓不一致（结果过期）：必须先重算，才允许进入报告"
                "（需求说明书 §5.9）"
            )
        context = {
            "session_id": state.id,
            "title": state.title,
            "scenario": state.scenario,
            "scenario_name": self.engine.tree(state.scenario).scenario_name,
            "caliber_version": state.caliber_version,
            "base": state.base.as_dict(),
            "current": state.current.as_dict(),
            "slice": json.loads(state.slice_filter.as_json()),
            "data_digest": state.data_digest,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "actor": actor,
            "session_status": state.status,
        }
        decomposition = self.engine.decompose(
            state.scenario,
            state.base,
            state.current,
            slice_filter=state.slice_filter,
            auto_targets=DEFAULT_TARGETS.get(state.scenario, []),
        )
        anomaly = judge(
            self.engine, state.scenario, state.current, decomposition.metric_code,
            slice_filter=state.slice_filter,
        )
        # 交叉校验：异动判定与指标树分解是两个独立来源，总变动必须一致
        assert_conservation(
            [decomposition.root.delta], anomaly.delta, decomposition.tolerance
        )
        hypotheses = self.sessions.hypotheses(session_id)
        evidence = self.sessions.evidence(session_id)
        drilldowns = self.sessions.drilldown_records(session_id)
        whatifs = self.sessions.whatif_records(session_id)
        events = self._matched_events(state.scenario, state.current, state.slice_filter)
        segments = [
            self._background(context, decomposition, anomaly),
            self._contributions(context, decomposition),
            self._drilldown(context, state, decomposition, drilldowns),
            self._conclusions(context, decomposition, hypotheses, events),
            self._evidence_chain(context, hypotheses, evidence),
            self._whatif(context, whatifs),
            self._actions(context, decomposition, whatifs),
        ]
        return {"meta": context, "segments": [segment.as_dict() for segment in segments]}

    def create(self, session_id: int, *, actor: str = "analyst") -> dict[str, Any]:
        """组装并归档：写 ``reports`` 表（status=draft），返回报告详情。"""

        structure = self.build(session_id, actor=actor)
        connection = connect_app(self.settings.app_db)
        try:
            user_id = connection.execute(
                "SELECT id FROM users WHERE role = 'analyst' ORDER BY id LIMIT 1"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO reports (session_id, title, structure_json, status,"
                " caliber_version, created_by) VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    structure["meta"]["title"],
                    json.dumps(structure, ensure_ascii=False),
                    "draft",
                    structure["meta"]["caliber_version"],
                    user_id,
                ),
            )
            report_id = int(cursor.lastrowid or 0)
            connection.commit()
        finally:
            connection.close()
        return self.get(report_id)

    def list_reports(
        self,
        *,
        limit: int = 50,
        domain: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """报告归档列表（按指标域 / 期间 / 状态筛选，见需求说明书 §4.5）。"""

        clauses: list[str] = []
        params: list[Any] = []
        if domain:
            clauses.append("s.domain = ?")
            params.append(domain)
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = connect_app(self.settings.app_db)
        try:
            rows = connection.execute(
                "SELECT r.id, r.session_id, r.title, r.status, r.caliber_version, r.created_at,"
                " s.domain, s.base_period, s.current_period, s.slice_json,"
                " (SELECT COUNT(*) FROM report_annotations AS a WHERE a.report_id = r.id)"
                " AS annotations"
                " FROM reports AS r JOIN sessions AS s ON s.id = r.session_id"
                f"{where} ORDER BY r.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def get(self, report_id: int) -> dict[str, Any]:
        """报告详情：``structure`` 是页面渲染的唯一数据源（§4.6）。"""

        connection = connect_app(self.settings.app_db)
        try:
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ReportError(f"报告 {report_id} 不存在")
        structure = json.loads(row["structure_json"])
        structure["meta"]["report_id"] = int(row["id"])
        structure["meta"]["report_status"] = row["status"]
        structure["meta"]["created_at"] = row["created_at"]
        structure["annotations"] = self.annotations(report_id)
        return structure

    def add_annotation(
        self, report_id: int, *, anchor: str, text: str, author: str = "analyst"
    ) -> dict[str, Any]:
        """段落级批注（单人编辑，保留痕迹：只追加、不覆盖）。"""

        if not anchor or not text:
            raise ReportError("批注必须带段落锚点与正文")
        connection = connect_app(self.settings.app_db)
        try:
            exists = connection.execute(
                "SELECT 1 FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if exists is None:
                raise ReportError(f"报告 {report_id} 不存在")
            cursor = connection.execute(
                "INSERT INTO report_annotations (report_id, anchor, text, author)"
                " VALUES (?,?,?,?)",
                (report_id, anchor, text, author),
            )
            connection.commit()
            annotation_id = int(cursor.lastrowid or 0)
        finally:
            connection.close()
        return {"id": annotation_id, "report_id": report_id, "anchor": anchor, "text": text,
                "author": author}

    def annotations(self, report_id: int) -> list[dict[str, Any]]:
        connection = connect_app(self.settings.app_db)
        try:
            rows = connection.execute(
                "SELECT id, anchor, text, author FROM report_annotations"
                " WHERE report_id = ? ORDER BY id",
                (report_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- 导出

    def export(
        self, report_id: int, fmt: str, *, actor: str = "analyst"
    ) -> dict[str, Any]:
        """导出一种格式：渲染 → 落盘 → 写 ``export_jobs``（含内容校验和）。"""

        structure = self.get(report_id)
        try:
            payload = render(fmt, structure)
        except ExportError as error:
            raise ReportError(str(error)) from error
        checksum = content_checksum(structure)
        export_dir = Path(self.settings.export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        path = export_dir / f"report-{report_id}-{checksum.split(':')[-1][:8]}.{fmt}"
        path.write_bytes(payload)
        connection = connect_app(self.settings.app_db)
        try:
            cursor = connection.execute(
                "INSERT INTO export_jobs (report_id, format, status, file_path, checksum)"
                " VALUES (?,?,?,?,?)",
                (report_id, fmt, "completed", str(path), checksum),
            )
            job_id = int(cursor.lastrowid or 0)
            connection.commit()
        finally:
            connection.close()
        return self.export_status(job_id, actor=actor)

    def export_status(self, job_id: int, *, actor: str = "analyst") -> dict[str, Any]:
        """导出任务状态与下载信息（含内容校验和与文件哈希）。"""

        connection = connect_app(self.settings.app_db)
        try:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ReportError(f"导出任务 {job_id} 不存在")
        path = Path(row["file_path"] or "")
        file_checksum = None
        size = None
        if path.exists():
            data = path.read_bytes()
            file_checksum = hashlib.sha256(data).hexdigest()
            size = len(data)
        _ = actor
        return {
            "id": int(row["id"]),
            "report_id": int(row["report_id"]),
            "format": row["format"],
            "status": row["status"],
            "file_path": row["file_path"],
            "file_name": path.name,
            "file_size": size,
            "content_checksum": row["checksum"],
            "file_sha256": file_checksum,
            "download_url": f"/api/exports/{int(row['id'])}/download",
        }

    def export_bytes(self, job_id: int) -> tuple[str, bytes]:
        """读取导出文件（下载接口用）。"""

        status = self.export_status(job_id)
        path = Path(status["file_path"] or "")
        if not path.exists():
            raise ReportError(f"导出文件不存在或已清理：{status['file_name']}")
        return status["file_name"], path.read_bytes()

    def verify_exports(self, report_id: int) -> dict[str, Any]:
        """E6：把三种格式都渲染一遍，逐字段比对校验和与取值。"""

        structure = self.get(report_id)
        payloads = {fmt: render(fmt, structure) for fmt in RENDERERS}
        return verify_consistency(structure, payloads)

    # ------------------------------------------------------------ 七段组装

    def _matched_events(
        self, scenario: str, current: Period, slice_filter: SliceFilter
    ) -> list[dict[str, Any]]:
        """事件匹配是确定性的：报告组装时按同一口径重跑一次，不依赖上次运行的缓存。"""

        connection = connect_app(self.settings.app_db)
        try:
            sync_seed_events(connection)
            events = load_events_from_db(connection)
        finally:
            connection.close()
        tree = self.engine.tree(scenario)
        matches = match_events(
            events,
            window_start=date.fromisoformat(current.start),
            window_end=date.fromisoformat(current.end),
            slice_dimensions=set(slice_filter.filters),
            scenario_dimensions=set(tree.dimensions),
            window_days=self.settings.attr_event_window_days,
            slice_values={key: list(values) for key, values in slice_filter.filters.items()},
        )
        return [match.as_dict() for match in matches]

    def _background(self, context, decomposition, anomaly) -> Segment:
        segment = Segment(
            index=1,
            code=SEGMENTS[0][0],
            title=SEGMENTS[0][1],
            source="数仓（指标字典 SQL）+ 异动判定（基线带）",
            period=f"{context['base']['start']}~{context['base']['end']}"
            f" → {context['current']['start']}~{context['current']['end']}",
        )
        root = decomposition.root
        delta_rate = None if root.base_total == 0 else root.delta / root.base_total
        segment.add("目标指标", root.code, f"{root.name}（{root.code}）")
        segment.add(
            "口径版本", context["caliber_version"], f"口径版本 v{context['caliber_version']}"
        )
        segment.add("对比方式", "环比", "环比：现期与紧挨着的等长上一期比")
        segment.add("基期值", root.base_total, _format_amount(root.base_total))
        segment.add("现期值", root.current_total, _format_amount(root.current_total))
        segment.add("总差异额", root.delta, _format_amount(root.delta))
        segment.add(
            "差异率",
            delta_rate,
            _format_rate(delta_rate),
        )
        position = (
            "落在正常波动区间内"
            if anomaly.in_normal_band
            else "超出正常波动区间"
        )
        segment.add(
            "基线带位置",
            anomaly.z_score,
            f"{position}（Z 分数 {'—' if anomaly.z_score is None else f'{anomaly.z_score:+.2f}'}，"
            f"幅度阈值 {self.settings.attr_change_threshold:.0%}，"
            f"Z 阈值 {self.settings.attr_z_threshold:g}）",
        )
        segment.body.append(
            f"{context['scenario_name']}·{root.name} 在 "
            f"{context['current']['start']}~{context['current']['end']}"
            f"（对比基期 {context['base']['start']}~{context['base']['end']}）"
            f"为 {_format_amount(root.current_total)}，较基期 "
            f"{_format_amount(root.delta)}（{_format_rate(delta_rate)}）；"
            f"{position}。"
        )
        if anomaly.note:
            segment.body.append(f"异动判定说明：{anomaly.note}")
        segment.body.append(
            "本段数字来自数仓两期取数与基线带判定；口径变更会升版本，历史报告绑定生成时的版本。"
        )
        segment.data = {"anomaly": anomaly.as_dict()}
        return segment

    def _contributions(self, context, decomposition) -> Segment:
        method_label = "差额分析" if decomposition.root.method == "diff" else "LMDI"
        segment = Segment(
            index=2,
            code=SEGMENTS[1][0],
            title=SEGMENTS[1][1],
            source=f"packages/attribution（{method_label}）+ 指标树",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        for item in decomposition.root.as_dict()["contributions"]:
            segment.add(
                f"因子贡献 · {item['factor']}",
                item["contribution"],
                f"{_format_amount(item['contribution'])}（贡献率 {_format_rate(item['rate'])}）",
            )
            segment.body.append(
                f"{item['factor']}：{_format_amount(item['contribution'])}，"
                f"贡献率 {_format_rate(item['rate'])}"
            )
        segment.add(
            "守恒残差（根节点）",
            decomposition.root.residual,
            f"{decomposition.root.residual:.6e}（容差 {decomposition.tolerance:g}）",
        )
        segment.add(
            "守恒判定",
            decomposition.ok,
            (
                f"{'通过' if decomposition.ok else '未通过'}：最大相对误差 "
                f"{decomposition.max_relative_residual:.3e}（判定分母 max(|Δ|, 1)）"
            ),
        )
        for node in decomposition.nodes[1:]:
            for item in node.as_dict()["contributions"]:
                segment.add(
                    f"下钻贡献 · {node.name}/{item['factor']}",
                    item["contribution"],
                    # 深层节点的贡献单位是**该节点自己的口径**（访客数是"人"、比率无量纲），
                    # 不是根指标的金额，所以这里不加"分"，避免单位错配。
                    f"{_format_amount(item['contribution'], unit='')}"
                    f"（贡献率 {_format_rate(item['rate'])}）",
                )
        segment.body.append(
            f"逐层守恒：{len(decomposition.nodes)} 层全部通过 "
            f"（最大相对误差 {decomposition.max_relative_residual:.3e}），"
            "残差为 0 是硬门槛，不是四舍五入的结果。"
        )
        if len(decomposition.nodes) > 1:
            segment.body.append(
                "深层节点的贡献数值单位是该节点自身的口径（例如访客数为人数、比率为无量纲），"
                "只有根指标那一层的贡献才是金额（分）；两者不可直接相加，报告与手册口径一致。"
            )
        if decomposition.skipped_targets:
            segment.body.append(
                "未下钻的节点（零值预案）："
                + "；".join(
                    f"{item['code']}（{item['reason']}）" for item in decomposition.skipped_targets
                )
            )
        segment.data = {"decomposition": decomposition.as_dict()}
        return segment

    def _pivots(self, state, decomposition, drilldowns) -> tuple[list[DimensionPivot], str]:
        """段 3 的数据源：优先用会话里已落库的下钻透视；没有就按场景默认维度补算一次。"""

        pivots: list[DimensionPivot] = []
        for record in drilldowns:
            payload = record["payload"]
            dimensions = payload.get("dimensions") or []
            if not dimensions or payload.get("summary"):
                continue
            pivots.append(
                self.engine.dimension_table(
                    state.scenario,
                    state.base,
                    state.current,
                    dimensions=tuple(str(item) for item in dimensions),
                    slice_filter=state.slice_filter,
                    top_n=int(payload.get("top_n") or 5),
                )
            )
        if pivots:
            return pivots, "会话下钻记录（L2，精确组合计算）"
        plan = default_dimensions(self.engine, state.scenario, state.slice_filter)
        for dimensions in plan:
            pivots.append(
                self.engine.dimension_table(
                    state.scenario,
                    state.base,
                    state.current,
                    dimensions=dimensions,
                    slice_filter=state.slice_filter,
                )
            )
        return pivots, "报告组装时补算（L2 未跑时按场景默认维度透视）"

    def _drilldown(self, context, state, decomposition, drilldowns) -> Segment:
        pivots, source = self._pivots(state, decomposition, drilldowns)
        segment = Segment(
            index=3,
            code=SEGMENTS[2][0],
            title=SEGMENTS[2][1],
            source=source + " + 指标树同切片总变动交叉校验",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        highlights: list[str] = []
        for record in drilldowns:
            payload = record["payload"]
            if payload.get("summary"):
                highlights.extend(str(item) for item in payload.get("highlights") or [])
        for pivot in pivots:
            head = "／".join(pivot.dimensions)
            rows = pivot.table.contributions
            for item in rows[: pivot.table.top_n]:
                scatter = "（占比分摊：启发式）" if pivot.table.heuristic else ""
                segment.add(
                    f"TOP 维度组合 · {head} / {'／'.join(item.key)}",
                    item.contribution,
                    f"{_format_amount(item.contribution)}"
                    f"（贡献率 {_format_rate(item.share_of_change)}）{scatter}",
                )
            segment.add(
                f"覆盖率 · {head}",
                pivot.table.coverage,
                f"{pivot.table.coverage:.1%}（TOP {pivot.table.top_n} 贡献 ÷ 全部组合贡献绝对值）",
            )
            segment.add(
                f"层总变动 · {head}",
                pivot.layer_delta,
                f"{_format_amount(pivot.layer_delta)}（与指标树同切片总变动交叉一致）",
            )
            segment.body.append(pivot.render(limit=pivot.table.top_n))
        if not pivots:
            segment.body.append("本次没有可用的维度下钻数据。")
        for item in highlights:
            segment.body.append(f"关键变化特征：{item}")
        if not highlights:
            segment.body.append("关键变化特征：本段按 TOP 组合与覆盖率给出，未做额外文字包装。")
        segment.data = {
            "n": max((pivot.table.top_n for pivot in pivots), default=5),
            "pivots": [pivot.as_dict() for pivot in pivots],
            "highlights": highlights,
        }
        return segment

    def _conclusions(self, context, decomposition, hypotheses, events) -> Segment:
        segment = Segment(
            index=4,
            code=SEGMENTS[3][0],
            title=SEGMENTS[3][1],
            source="确定性分解（贡献额）+ 沙箱验证通过的假设（佐证）+ 事件匹配（外生变量）",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        ranked = decomposition.top(limit=3)
        for index, item in enumerate(ranked):
            label = "首要原因" if index == 0 else f"次要原因 {index}"
            segment.add(
                label,
                item["contribution"],
                f"{item['factor']}：{_format_amount(item['contribution'])}"
                f"（贡献率 {_format_rate(item['rate'])}）",
            )
        verified = [item for item in hypotheses if item["status"] == "verified"]
        excluded = [item for item in hypotheses if item["status"] != "verified"]
        for item in verified:
            segment.add(
                f"已验证假设 · {item['id']}",
                item["confidence"],
                f"{item['statement']}（置信度 {item['confidence']:.2f}）",
            )
        segment.add(
            "外部诱因",
            [match["event"]["name"] for match in events if match["tier"] == "因果链"],
            (
                "、".join(match["event"]["name"] for match in events if match["tier"] == "因果链")
                or "窗口内未命中事件，不强行关联"
            ),
        )
        internal = [item["factor"] for item in ranked]
        segment.add(
            "内外部诱因划分",
            {"internal": internal, "external": [match["event"]["name"] for match in events]},
            f"内部（可控因子）：{'、'.join(internal) or '—'}；"
            f"外部（事件）："
            + (
                "、".join(match["event"]["name"] for match in events)
                if events
                else "窗口内无事件，不做外部归因"
            ),
        )
        for item in ranked:
            segment.body.append(
                f"{item['factor']} 贡献 {_format_amount(item['contribution'])}"
                f"（贡献率 {_format_rate(item['rate'])}）"
            )
        if excluded:
            segment.body.append(
                "未采信的假设（连同排除理由）："
                + "；".join(f"{item['statement']} → {item['reason']}" for item in excluded)
            )
        segment.body.append(
            "每条结论都绑定贡献额；贡献额来自确定性分解，置信度来自统计检验，两者口径不同、不混用。"
        )
        segment.data = {"primary": ranked[0] if ranked else None, "ranked": ranked}
        return segment

    def _evidence_chain(self, context, hypotheses, evidence) -> Segment:
        segment = Segment(
            index=5,
            code=SEGMENTS[4][0],
            title=SEGMENTS[4][1],
            source="沙箱执行记录（SQL 摘要）+ 置换检验 + 伪相关四步检查",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        by_hypothesis: dict[int, list[dict[str, Any]]] = {}
        for row in evidence:
            by_hypothesis.setdefault(int(row["hypothesis_id"] or 0), []).append(row)
        for item in hypotheses:
            rows = by_hypothesis.get(int(item["id"]), [])
            for row in rows:
                segment.add(
                    f"假设 {item['id']} · 检验方法与样本量",
                    {
                        "kind": row["kind"],
                        "sample_size": row["sample_size"],
                        "p_value": row["p_value"],
                        "effect_size": row["effect_size"],
                    },
                    f"{row['kind']}：样本量 {row['sample_size']}，"
                    f"p 值 {'—' if row['p_value'] is None else f'{row['p_value']:.4f}'}，"
                    f"效应量 {'—' if row['effect_size'] is None else f'{row['effect_size']:+.3f}'}",
                )
                segment.add(
                    f"假设 {item['id']} · 沙箱执行摘要",
                    row["sql_digest"],
                    f"SQL 摘要 {row['sql_digest']}",
                )
            segment.add(
                f"假设 {item['id']} · 结论与置信度",
                item["confidence"],
                f"[{item['status']}] 置信度 "
                f"{'—' if item['confidence'] is None else f'{item['confidence']:.2f}'}"
                f"｜{item['reason'] or ''}",
            )
        if not hypotheses:
            segment.body.append("本次会话没有生成假设（可能尚未执行 L1 链路）。")
        segment.add(
            "伪相关排除记录",
            len(hypotheses),
            f"{len(hypotheses)} 条假设逐条留痕；被排除的假设在段 4 列出理由",
        )
        segment.body.append(
            "每条假设都带证据链：沙箱 SQL 摘要、样本量、p 值、效应量与覆盖天数；"
            "四步伪相关检查的下调幅度记录在假设的证据字段里（evidence_json.penalties）。"
        )
        segment.data = {
            "hypotheses": hypotheses,
            "evidence": evidence,
        }
        return segment

    def _whatif(self, context, whatifs) -> Segment:
        segment = Segment(
            index=6,
            code=SEGMENTS[5][0],
            title=SEGMENTS[5][1],
            source="packages/attribution.elasticity（对数回归 + bootstrap 区间）+ 数仓日序列",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        summaries = [item["payload"] for item in whatifs if item["payload"].get("summary")]
        if not summaries:
            segment.add("推演状态", None, "本次会话未做 What-If 推演（段 6 无数据）")
            segment.body.append(
                "没有推演记录时本段不做任何估计：不编区间、不给「预计提升」这类数字。"
            )
            segment.data = {}
            return segment
        for index, payload in enumerate(summaries, start=1):
            curve = payload["curve"]
            estimate = curve["elasticity"]
            segment.add(
                f"情景 {index} · 可干预因子",
                payload["factor"]["code"],
                f"{payload['factor']['name']}（代理口径：{payload['factor']['proxy_label']}）",
            )
            segment.add(
                f"情景 {index} · 弹性",
                estimate["value"],
                f"{estimate['value']:+.3f}（{estimate['level']:.0%} 区间 "
                f"{estimate['low']:+.3f} ~ {estimate['high']:+.3f}，"
                f"{'对数回归' if not estimate['degraded'] else '有限差分（降级）'}，"
                f"n={estimate['sample_size']}）",
            )
            segment.add(
                f"情景 {index} · 把握度",
                payload["confidence"],
                f"{payload['confidence']:.0%}"
                f"（样本 {estimate['confidence_parts']['sample']:.2f} / "
                f"拟合 {estimate['confidence_parts']['fit']:.2f} / "
                f"区间 {estimate['confidence_parts']['width']:.2f}）",
            )
            for point in curve["points"]:
                segment.add(
                    f"情景 {index} · 调 {point['adjustment']:+.0%}",
                    point["expected"],
                    f"{_format_amount(point['expected'])}"
                    f"（区间 {_format_amount(point['low'])} ~ {_format_amount(point['high'])}"
                    f"{'，⚠外推' if point['out_of_range'] else ''}）",
                )
            for item in payload.get("assumptions", []):
                segment.body.append(f"前提条件：{item}")
            for item in payload.get("warnings", []):
                segment.body.append(f"警示：{item}")
        segment.data = {"scenarios": summaries}
        return segment

    def _actions(self, context, decomposition, whatifs) -> Segment:
        segment = Segment(
            index=7,
            code=SEGMENTS[6][0],
            title=SEGMENTS[6][1],
            source="corpus/report/action_playbook.yaml（岗位/周期为项目自定参数）+ What-If 区间",
            period=f"{context['current']['start']}~{context['current']['end']}",
        )
        playbook = load_playbook()
        plan = playbook["factors"].get(context["scenario"], {})
        curves: dict[str, dict[str, Any]] = {}
        for item in whatifs:
            payload = item["payload"]
            if payload.get("summary"):
                curves[payload["factor"]["code"]] = payload["curve"]
        rank = 0
        for item in decomposition.top(limit=3):
            factor = str(item["factor"])
            entry = plan.get(factor)
            if entry is None:
                segment.add(
                    f"行动 · {factor}",
                    item["contribution"],
                    f"{factor}：贡献 {_format_amount(item['contribution'])}，"
                    "台账里没有对应动作模板 → 按规则不给泛化建议",
                )
                continue
            rank += 1
            segment.add(
                f"行动 {rank} · 驱动因子",
                item["contribution"],
                f"{factor}（贡献 {_format_amount(item['contribution'])}，"
                f"贡献率 {_format_rate(item['rate'])}）",
            )
            segment.add(f"行动 {rank} · 业务动作", entry["action"], entry["action"])
            segment.add(f"行动 {rank} · 责任岗位", entry["owner"], entry["owner"])
            segment.add(f"行动 {rank} · 预估周期", entry["cycle"], entry["cycle"])
            knobs = entry.get("knob")
            curve = curves.get(str(knobs)) if knobs else None
            if curve is None:
                segment.add(
                    f"行动 {rank} · 预期收益区间",
                    None,
                    "不给收益区间：" + (entry.get("note") or "没有对应的推演曲线"),
                )
            else:
                adjustment = float(entry.get("adjustment") or 0.0)
                point = min(
                    curve["points"], key=lambda item: abs(item["adjustment"] - adjustment)
                )
                segment.add(
                    f"行动 {rank} · 预期收益区间",
                    point["expected"],
                    f"调 {point['adjustment']:+.0%}："
                    f"{_format_amount(point['low'])} ~ {_format_amount(point['high'])}"
                    f"（区间，不是承诺值；把握度见段 6）",
                )
                segment.body.append(
                    f"{entry['action']}：{entry['owner']}，{entry['cycle']}，"
                    f"预期收益区间 {_format_amount(point['low'])} ~ "
                    f"{_format_amount(point['high'])}"
                )
                continue
            segment.body.append(
                f"{entry['action']}：{entry['owner']}，{entry['cycle']}（{segment.fields[-1]['display']}）"
            )
        if segment.fields == []:
            segment.body.append("没有可绑定的驱动因子，本段为空（不写泛化建议）。")
        segment.body.append(
            "每条行动都绑定驱动因子与责任岗位；预期收益一律给区间，且只在有推演曲线时给。"
        )
        segment.data = {"playbook_source_note": playbook.get("source_note", "")}
        return segment


__all__ = [
    "PLAYBOOK_PATH",
    "SEGMENTS",
    "ReportError",
    "ReportService",
    "Segment",
    "load_playbook",
]
