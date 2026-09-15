"""E4 证据：沙箱对抗测试集拦截率必须 100%，且正控不能被误拦。"""

from __future__ import annotations

from app.config import Settings
from app.sandbox.adversarial import load_cases, run_suite
from app.sandbox.runner import SandboxRunner


def test_case_set_covers_required_categories() -> None:
    """E4 要求覆盖：逃逸 / 越权读表 / 超配额 / 网络访问 / 写业务表。"""

    cases = load_cases()
    categories = {case.category for case in cases}
    assert {"逃逸", "越权读表", "超配额", "网络访问", "写业务表"} <= categories
    blocked = [case for case in cases if case.expect == "blocked"]
    allowed = [case for case in cases if case.expect == "allowed"]
    assert len(blocked) >= 10, "E4 要求至少 10 个对抗用例"
    assert allowed, "必须有正控，否则 100% 拦截率没有意义"
    assert len({case.id for case in cases}) == len(cases), "用例 id 不能重复"


def test_adversarial_suite_blocks_everything_dangerous(sandbox_settings: Settings) -> None:
    """跑完整套对抗：拦截率 100%、无漏拦、无误拦、每条都有审计记录。"""

    runner = SandboxRunner(sandbox_settings)
    report = run_suite(runner)
    assert report.ok, report.render()
    assert report.interception_rate == 1.0
    assert report.escaped == []
    assert report.false_blocks == []
    assert report.audited == report.total
    payload = report.as_dict()
    assert payload["interception_rate"] == 1.0
    assert payload["audited"] == payload["total"]
