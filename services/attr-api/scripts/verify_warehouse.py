"""数仓校验脚本：对账、指标 SQL、指标树恒等式、真值复核。

用法：``python services/attr-api/scripts/verify_warehouse.py``
退出码 0 表示全部通过；非 0 表示存在失败项，不允许把数仓用于下游阶段。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "attr-api"
# 纯算法包不安装、按源码路径引用（与 pytest.ini 的 pythonpath 保持一致）
for extra in (SERVICE_ROOT, REPO_ROOT / "packages" / "attribution"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.warehouse.verify import verify_warehouse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校验合成数仓")
    parser.add_argument("--no-digest", action="store_true", help="跳过整库指纹计算（更快）")
    args = parser.parse_args()
    report, fingerprint = verify_warehouse(digest=not args.no_digest)
    print(report.render())
    if fingerprint:
        print(f"整库指纹（sha256，按天汇总）：{fingerprint}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
