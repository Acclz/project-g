"""跑沙箱对抗测试集并打印报告（演示与留档用）。

用法：``python services/attr-api/scripts/run_adversarial.py [--json <路径>]``
退出码：0 = 拦截率 100% 且无误拦；非 0 = 存在漏拦或误拦。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
for extra in (SERVICE_ROOT, REPO_ROOT / "packages" / "attribution"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.sandbox.adversarial import run_suite  # noqa: E402
from app.sandbox.runner import SandboxRunner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="沙箱对抗测试集")
    parser.add_argument(
        "--json",
        type=Path,
        default=SERVICE_ROOT / "eval" / "sandbox_adversarial.json",
        help="结果落盘路径",
    )
    args = parser.parse_args()
    report = run_suite(SandboxRunner(), actor="cli")
    print(report.render())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"结果已写入：{args.json}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
