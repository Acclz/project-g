"""P5 收尾演示：三条链路端到端 + 导出一致性比对（一条命令跑完，数字可复现）。

为什么用 Python 而不是 .ps1：仓库面向 Windows，但 PowerShell 5.1 默认按 GBK 读脚本，
中文提示会直接变成乱码甚至解析失败；Python 脚本统一 UTF-8，跨 shell 都稳。

用法（仓库根目录）：

    python scripts/demo_p5.py            # 三条链路 + 七段报告与导出一致性
    python scripts/demo_p5.py --quick    # 只跑 L2/L3（L1 已在别处验证过时用）

前置：先有完整档数仓

    python services/attr-api/scripts/generate_warehouse.py --profile full --reset

退出码：任一步失败即非 0（可直接接 CI）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STEPS: tuple[tuple[str, str], ...] = (
    ("L1 归因链路（异动→拆解→假设→验证→事件→结论）", "services/attr-api/scripts/demo_attribution.py"),
    ("L2 下钻链路（只收紧切片→维度透视→逐层守恒）", "services/attr-api/scripts/demo_drilldown.py"),
    ("L3 What-If 推演（可干预因子→弹性区间→前提条件）", "services/attr-api/scripts/demo_whatif.py"),
    ("L3 七段报告与三格式导出（E6 字段级一致）", "services/attr-api/scripts/demo_report.py"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P5 三链路端到端演示")
    parser.add_argument("--quick", action="store_true", help="跳过 L1（耗时最长的一段）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures: list[str] = []
    for name, script in STEPS:
        if args.quick and script.endswith("demo_attribution.py"):
            continue
        print("=" * 78)
        print(f"▶ {name}")
        print("=" * 78)
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / script)],
            cwd=REPO_ROOT,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        if completed.returncode != 0:
            failures.append(name)
            print(f"✗ 失败：{name}（退出码 {completed.returncode}）")
    print("=" * 78)
    if failures:
        print("以下步骤失败：" + "、".join(failures))
        return 1
    print("三链路演示全部通过；证据落盘在 services/attr-api/eval/*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
