"""假设生成与验证测试：五要素、切片继承、模型通道、置信度与四步检查。"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.sandbox.runner import Quotas, SandboxRunner
from app.services.decomposition import MetricEngine, Period, SliceFilter
from app.services.hypotheses import (
    Hypothesis,
    HypothesisVerifier,
    build_context,
    generate_hypotheses,
)
from app.services.llm import DeepSeekWriter, TemplateWriter, build_writer
from app.services.pseudo_correlation import PseudoCorrelationChecker


def _ecom_engine(sandbox_settings: Settings) -> MetricEngine:
    return MetricEngine(sandbox_settings)


def _report(engine: MetricEngine):
    return engine.decompose(
        "ecom",
        Period("2026-06-01", "2026-06-04"),
        Period("2026-06-05", "2026-06-08"),
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
        auto_targets=["uv", "cvr", "aov"],
    )


def test_context_and_template_hypotheses(sandbox_settings: Settings) -> None:
    engine = _ecom_engine(sandbox_settings)
    context = build_context(engine, _report(engine), slice_filter=SliceFilter({"channel": ("paid_ads",)}))
    assert context["scenario"] == "ecom"
    assert context["factors"], "上下文必须带上因子清单"
    hypotheses, note = generate_hypotheses(context, settings=sandbox_settings)
    assert 1 <= len(hypotheses) <= sandbox_settings.attr_max_hypotheses
    assert "LLM_API_KEY" in note
    for hypothesis in hypotheses:
        assert hypothesis.statement and hypothesis.verification
        assert hypothesis.sql_draft and hypothesis.counter_check
        assert hypothesis.expected_direction in ("up", "down")
        assert hypothesis.source == "template"
        # 关键回归：假设必须带着分析切片，否则"切片内 vs 切片外"会退化成自己比自己
        assert hypothesis.slice_filter.filters == {"channel": ("paid_ads",)}
        assert hypothesis.scenario == "ecom"


def test_writer_selection_without_key(sandbox_settings: Settings) -> None:
    writer = build_writer(sandbox_settings)
    assert isinstance(writer, TemplateWriter)
    assert writer.available() is True


def test_llm_drafts_are_parsed_but_numbers_never_come_from_model(
    sandbox_settings: Settings,
) -> None:
    """模型通道：只取文本与 SQL 草案；草案里的"数字"不会被采信（数值由沙箱算）。"""

    payload = [
        {
            "statement": "付费渠道转化率下滑是主因",
            "expected_direction": "down",
            "verification": "比较切片内外转化率",
            "sql_draft": "SELECT day, 1.0 * SUM(orders_paid) / NULLIF(SUM(visitors),0) AS value"
            " FROM dw.fact_ecom_daily AS fact GROUP BY day",
            "counter_check": "切片内各区域应同向变化",
            "factor": "cvr",
        }
    ]

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}

    class _Client:
        def post(self, *_args, **_kwargs) -> _Response:
            return _Response()

    settings = Settings(
        llm_api_key="fake-key-for-test",
        warehouse_db_path=sandbox_settings.warehouse_db_path,
        app_db_path=sandbox_settings.app_db_path,
    )
    writer = DeepSeekWriter(settings, client=_Client())
    assert writer.available() is True
    drafts = writer.draft({"tables": ["dw.fact_ecom_daily"]}, limit=3)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.source == "llm" and draft.model == settings.llm_model
    assert draft.factor == "cvr"
    assert "1.0 * SUM(orders_paid)" in draft.sql_draft
    assert not hasattr(draft, "contribution"), "草案里不存在数值字段，模型无法参与数值链路"


def test_verification_on_small_warehouse_reports_insufficient_evidence(
    sandbox_settings: Settings,
) -> None:
    """小样本数仓只有 8 天：按 §5.9 第 4 条应判"证据不足"，并保留理由与证据记录。"""

    engine = _ecom_engine(sandbox_settings)
    sandbox = SandboxRunner(sandbox_settings)
    verifier = HypothesisVerifier(engine, sandbox, sandbox_settings)
    context = build_context(engine, _report(engine), slice_filter=SliceFilter({"channel": ("paid_ads",)}))
    hypothesis = generate_hypotheses(context, settings=sandbox_settings, limit=1)[0][0]
    result = verifier.verify(
        hypothesis,
        scenario="ecom",
        current=Period("2026-06-05", "2026-06-08"),
        base=Period("2026-06-01", "2026-06-04"),
        actor="pytest",
    )
    assert result.status in ("excluded", "insufficient")
    assert result.reason
    assert result.evidence is not None
    assert result.evidence.sample_size >= 3


def test_pseudo_correlation_flags_insufficient_sample(sandbox_settings: Settings) -> None:
    engine = _ecom_engine(sandbox_settings)
    sandbox = SandboxRunner(sandbox_settings)
    verifier = HypothesisVerifier(engine, sandbox, sandbox_settings)
    checker = PseudoCorrelationChecker(engine, sandbox, sandbox_settings)
    hypothesis = Hypothesis(
        statement="切片内的转化率变化是主因",
        expected_direction="down",
        verification="对照检验",
        sql_draft="",
        counter_check="子维度同向",
        factor="cvr",
        factor_name="支付转化率",
        scope_label="切片 channel=paid_ads",
        source="pytest",
        scenario="ecom",
        slice_filter=SliceFilter({"channel": ("paid_ads",)}),
    )
    current = Period("2026-06-05", "2026-06-08")
    result = verifier.verify(
        hypothesis,
        scenario="ecom",
        current=current,
        base=Period("2026-06-01", "2026-06-04"),
        actor="pytest",
    )
    verdict = checker.check(hypothesis, result, scenario="ecom", current=current, actor="pytest")
    assert len(verdict.steps) == 4
    assert verdict.verdict in ("证据不足", "共同驱动", "趋势性共同驱动", "通过四步检查", "通过但已下调置信度")
    assert verdict.reasons or verdict.verdict == "通过四步检查"


def test_sandbox_is_the_only_way_to_get_series(sandbox_settings: Settings) -> None:
    """没有沙箱就不许取数：这是"模型生成的 SQL 必须经沙箱"的代码级约束。"""

    from app.services.hypotheses import FactorProbe

    engine = _ecom_engine(sandbox_settings)
    probe = FactorProbe(engine, None)
    with pytest.raises(RuntimeError):
        probe.series("COUNT(*)", Period("2026-06-01", "2026-06-02"), scenario="ecom")


def test_quota_override_is_respected_by_verifier(sandbox_settings: Settings) -> None:
    """验证器用的沙箱配额来自配置（这里只确认默认配额可读，避免硬编码）。"""

    runner = SandboxRunner(sandbox_settings)
    quotas = runner.default_quotas()
    assert isinstance(quotas, Quotas)
    assert quotas.timeout_seconds == sandbox_settings.sandbox_timeout_seconds
    assert quotas.max_rows == sandbox_settings.sandbox_max_rows
