"""沙箱对抗测试集：把"安全沙箱"这句名词变成可复算的拦截率（验收标准 E4）。

用例写在 ``corpus/eval/sandbox_adversarial.yaml``，分两类：

* ``expect: blocked``——必须被拦下，且要有审计记录；
* ``expect: allowed``——**正控**。没有正控的 100% 拦截率毫无意义：把一切都拒绝也能得到 100%，
  所以必须同时证明"正常的查数请求是能跑的"。

每跑一次，用例结果与拦截率都会写进 ``app.eval_runs``（``/api/eval/run`` 调的就是这里）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT
from app.sandbox.runner import Quotas, SandboxResult, SandboxRunner

DEFAULT_CASES_PATH = REPO_ROOT / "corpus" / "eval" / "sandbox_adversarial.yaml"
DATASET_NAME = "sandbox_adversarial"


@dataclass(frozen=True)
class AdversarialCase:
    """一个对抗用例：类别、语言、代码、期望结果。"""

    id: str
    category: str
    kind: str
    code: str
    expect: str
    note: str = ""
    quotas: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseOutcome:
    """单个用例的执行结果与判定。"""

    case: AdversarialCase
    result: SandboxResult
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "category": self.case.category,
            "kind": self.case.kind,
            "expect": self.case.expect,
            "passed": self.passed,
            "blocked_reason": self.result.blocked_reason,
            "error": self.result.error,
            "duration_ms": self.result.duration_ms,
            "audit_id": self.result.audit_id,
            "note": self.case.note,
        }


@dataclass
class AdversarialReport:
    """整份对抗报告：拦截率、误拦、漏拦都摆出来。"""

    outcomes: list[CaseOutcome]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def blocked_cases(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.case.expect == "blocked"]

    @property
    def allowed_cases(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.case.expect == "allowed"]

    @property
    def interception_rate(self) -> float:
        """必须被拦下的用例里，真正被拦下的比例（E4 的核心数字）。"""

        cases = self.blocked_cases
        if not cases:
            return 0.0
        return sum(1 for item in cases if item.result.blocked) / len(cases)

    @property
    def false_blocks(self) -> list[CaseOutcome]:
        """误拦：本该允许却被拦下（会砸掉正常功能，同样算缺陷）。"""

        return [item for item in self.allowed_cases if item.result.blocked]

    @property
    def escaped(self) -> list[CaseOutcome]:
        """漏拦：本该拦下却跑通了（安全缺陷）。"""

        return [item for item in self.blocked_cases if not item.result.blocked]

    @property
    def audited(self) -> int:
        """有审计记录的用例数（被拦截的也要留痕）。"""

        return sum(1 for item in self.outcomes if item.result.audit_id)

    @property
    def ok(self) -> bool:
        return (
            self.interception_rate >= 1.0
            and not self.false_blocks
            and self.audited == self.total
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": DATASET_NAME,
            "total": self.total,
            "blocked_cases": len(self.blocked_cases),
            "allowed_cases": len(self.allowed_cases),
            "interception_rate": round(self.interception_rate, 4),
            "escaped": [item.case.id for item in self.escaped],
            "false_blocks": [item.case.id for item in self.false_blocks],
            "audited": self.audited,
            "cases": [item.as_dict() for item in self.outcomes],
        }

    def render(self) -> str:
        lines = [
            f"对抗用例 {self.total} 个：应拦截 {len(self.blocked_cases)}"
            f"、正控 {len(self.allowed_cases)}",
            f"拦截率 {self.interception_rate:.1%}　漏拦 {len(self.escaped)}"
            f"　误拦 {len(self.false_blocks)}　有审计记录 {self.audited}/{self.total}",
        ]
        for item in self.outcomes:
            mark = "PASS" if item.passed else "FAIL"
            reason = item.result.blocked_reason or item.result.error or "未拦截"
            lines.append(f"[{mark}] {item.case.id}（{item.case.category}）→ {reason[:80]}")
        return "\n".join(lines)


def load_cases(path: Path | None = None) -> list[AdversarialCase]:
    """读取对抗用例清单。"""

    target = Path(path) if path is not None else DEFAULT_CASES_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases: list[AdversarialCase] = []
    for item in payload.get("cases", []):
        cases.append(
            AdversarialCase(
                id=str(item["id"]),
                category=str(item["category"]),
                kind=str(item["kind"]),
                code=str(item["code"]),
                expect=str(item.get("expect", "blocked")),
                note=str(item.get("note") or ""),
                quotas=dict(item.get("quotas") or {}),
            )
        )
    if not cases:
        raise ValueError(f"对抗用例清单为空：{target}")
    return cases


def run_suite(
    runner: SandboxRunner, cases: list[AdversarialCase] | None = None, *, actor: str = "eval"
) -> AdversarialReport:
    """跑完整套对抗用例并给出判定（默认配额来自配置，个别用例可覆盖以加快失败速度）。"""

    outcomes: list[CaseOutcome] = []
    for case in cases or load_cases():
        quotas = Quotas(**case.quotas) if case.quotas else None
        if case.kind == "sql":
            result = runner.run_sql(case.code, quotas=quotas, actor=actor)
        else:
            result = runner.run_python(case.code, quotas=quotas, actor=actor)
        expected_block = case.expect == "blocked"
        passed = result.blocked if expected_block else (result.ok and not result.blocked)
        outcomes.append(CaseOutcome(case=case, result=result, passed=passed))
    return AdversarialReport(outcomes=outcomes)
