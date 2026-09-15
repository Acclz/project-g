"""假设文本与 SQL 草案的生成通道（云端 DeepSeek，OpenAI 兼容接口）。

红线（技术规格 §1 第 2 条、需求说明书 §5.5）：

* 模型**只**产出假设文本、SQL 草案与报告文字，**任何数值都不允许来自模型**；
* 草案必须过沙箱（P3 的执行器）才算数——模型说的话不作证据；
* 没有可用密钥时降级为**确定性模板**，并在记录里标注 ``source="template"``：
  绝不把模板输出说成模型输出，也绝不用"预录的模型回答"充数（D-07）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings

REQUEST_TIMEOUT_SECONDS = 30.0

SYSTEM_PROMPT = """你是经营归因分析系统里的"假设提出者"。
你的职责只有一个：根据给定的异动拆解结果，提出 3~5 条可被数据检验的候选假设。
硬性要求：
1. 每条假设必须包含五要素：statement（假设陈述）、expected_direction（预期方向：up/down）、
   verification（验证口径，说明比较什么与什么）、sql_draft（一条 SQLite 方言的 SELECT，
   只允许引用 dw 数仓里的表，表名见 given_tables）、counter_check（反例检查：若假设成立，
   哪个子集应呈现一致方向）。
2. 禁止编造任何数值、贡献额、百分比——数值一律由系统计算，你只提假设与取数草案。
3. 只输出 JSON 数组，不要输出解释文字或 markdown 代码块。
"""


@dataclass(frozen=True)
class HypothesisDraft:
    """一条候选假设草案（五要素齐备）。数值字段一律为空——数值不来自模型。"""

    statement: str
    expected_direction: str
    verification: str
    sql_draft: str
    counter_check: str
    factor: str = ""
    source: str = "template"
    model: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "expected_direction": self.expected_direction,
            "verification": self.verification,
            "sql_draft": self.sql_draft,
            "counter_check": self.counter_check,
            "factor": self.factor,
            "source": self.source,
            "model": self.model,
            "note": self.note,
        }


class HypothesisWriter(Protocol):
    """写作者接口：模板与模型两条实现共用同一份调用约定，便于测试时注入替身。"""

    name: str

    def available(self) -> bool: ...

    def draft(self, context: dict[str, Any], limit: int) -> list[HypothesisDraft]: ...


@dataclass
class TemplateWriter:
    """确定性模板：没有模型密钥时的降级路径，输出同样满足五要素要求。"""

    name: str = "template"

    def available(self) -> bool:
        return True

    def draft(self, context: dict[str, Any], limit: int) -> list[HypothesisDraft]:
        metric_name = context.get("metric_name", "目标指标")
        factors = context.get("factors", [])[:limit]
        drafts: list[HypothesisDraft] = []
        for factor in factors:
            direction = "down" if factor["contribution"] < 0 else "up"
            drafts.append(
                HypothesisDraft(
                    statement=(
                        f"{factor['name']}（{factor['code']}）在{factor['scope_label']}上的"
                        f"变化是{metric_name}变动的主要来源之一"
                    ),
                    expected_direction=direction,
                    verification=(
                        f"在沙箱里取{factor['name']}的日序列，比较「{factor['scope_label']}」"
                        "与其他切片同期表现（置换检验，10000 次，固定种子）"
                    ),
                    sql_draft=factor["sql"],
                    counter_check=(
                        f"若该假设成立，{factor['scope_label']}内部各子切片应呈现同方向变化；"
                        "若出现方向相反的明显反例，则下调置信度"
                    ),
                    factor=factor["code"],
                    source="template",
                    note="无 LLM 密钥时的确定性模板草案（数值一律由系统计算）",
                )
            )
        return drafts


@dataclass
class DeepSeekWriter:
    """DeepSeek（OpenAI 兼容）写作者：只产出文本与 SQL 草案。"""

    settings: Settings
    name: str = "deepseek"
    client: httpx.Client | None = None
    last_error: str = ""
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def draft(self, context: dict[str, Any], limit: int) -> list[HypothesisDraft]:
        if not self.available():
            raise RuntimeError("未配置 LLM_API_KEY，不能调用模型")
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": f"针对以下异动提出最多 {limit} 条候选假设",
                            "given_tables": context.get("tables", []),
                            "context": context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        response = self._client.post(
            f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        # 草案是 frozen dataclass：用 replace 打上"来自哪个模型"，而不是原地改字段
        drafts = [
            replace(draft, model=self.settings.llm_model) for draft in _parse_drafts(content)
        ]
        return drafts[:limit]


def _parse_drafts(content: str) -> list[HypothesisDraft]:
    """把模型输出解析成草案；解析失败就抛错，由调用方决定降级（不猜、不兜底编内容）。"""

    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"模型输出不是合法 JSON：{error}") from error
    if isinstance(payload, dict):
        payload = payload.get("hypotheses", [])
    drafts: list[HypothesisDraft] = []
    for item in payload:
        drafts.append(
            HypothesisDraft(
                statement=str(item.get("statement", "")).strip(),
                expected_direction=str(item.get("expected_direction", "")).strip(),
                verification=str(item.get("verification", "")).strip(),
                sql_draft=str(item.get("sql_draft", "")).strip(),
                counter_check=str(item.get("counter_check", "")).strip(),
                factor=str(item.get("factor", "") or ""),
                source="llm",
            )
        )
    return drafts


def build_writer(settings: Settings | None = None) -> HypothesisWriter:
    """按配置选写作者：有密钥走模型，没有则走模板（并在记录里如实标注来源）。"""

    active = settings or get_settings()
    candidate = DeepSeekWriter(active)
    if candidate.available():
        return candidate
    return TemplateWriter()


def draft_with_fallback(
    context: dict[str, Any], limit: int, settings: Settings | None = None
) -> tuple[list[HypothesisDraft], str]:
    """生成草案：模型失败（无密钥、超时、JSON 不合法）时退回模板，并返回降级原因。"""

    writer = build_writer(settings)
    if writer.name == "template":
        return writer.draft(context, limit), "未配置 LLM_API_KEY，使用确定性模板草案"
    try:
        drafts = writer.draft(context, limit)
        if not drafts:
            raise ValueError("模型没有返回任何假设")
        return drafts, ""
    except Exception as error:  # noqa: BLE001 - 模型通道的任何问题都不允许拖垮链路
        fallback = TemplateWriter().draft(context, limit)
        return fallback, f"模型通道失败（{type(error).__name__}: {error}），已降级为模板草案"


__all__ = [
    "DeepSeekWriter",
    "HypothesisDraft",
    "HypothesisWriter",
    "SYSTEM_PROMPT",
    "TemplateWriter",
    "build_writer",
    "draft_with_fallback",
]
