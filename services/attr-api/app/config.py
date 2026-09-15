"""应用配置：所有可调项集中在这里，缺关键项时启动即失败（技术规格 §9）。

约定：

* 配置只从环境变量与仓库根目录的 ``.env`` 读取，禁止硬编码在业务代码里。
* 非 local 环境若 ``SECRET_KEY`` 仍是占位值，直接在启动时抛错（不允许运行时才发现）。
* 数据库从 PostgreSQL 改为 SQLite 的取舍与理由见 ``docs/04-决策记录.md`` D-22。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# services/attr-api/app/config.py → parents[3] 即仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET_PLACEHOLDER = "change-me-in-real-env"


class Settings(BaseSettings):
    """运行时配置。字段名与 ``.env`` 的键一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用
    app_name: str = "项目zz · 经营归因分析系统"
    app_env: str = "local"
    debug: bool = True
    secret_key: str = SECRET_PLACEHOLDER
    access_token_ttl_minutes: int = 720
    cors_origins: str = "http://localhost:5173"

    # 数据库：dw（合成数仓，只读打开）与 app（业务与治理表）分成两个 SQLite 文件，
    # 这样沙箱用 `?mode=ro&immutable=1` 打开 dw.db 就物理上读不到 app 侧表。
    data_dir: Path = Path(".data")
    warehouse_db_path: Path = Field(default=Path(".data/dw.db"))
    app_db_path: Path = Field(default=Path(".data/app.db"))
    database_url: str = "sqlite+pysqlite:///./.data/app.db"

    # 数据沙箱（受限子进程：无网络、只读打开、资源配额）
    sandbox_enabled: bool = True
    sandbox_timeout_seconds: int = 20
    sandbox_max_rows: int = 200_000
    sandbox_memory_mb: int = 768
    sandbox_cpu_seconds: int = 15
    sandbox_allow_python: bool = True
    sandbox_tmp_dir: Path = Path(".data/sandbox")

    # 分解与守恒
    attr_conservation_tolerance: float = 1e-9
    attr_default_dimensions: str = "channel,category,region,segment"
    attr_max_hypotheses: int = 5
    attr_min_confidence: float = 0.6

    # 合成数仓（参数默认值必须与 corpus/warehouse/calibration.csv 口径一致）
    warehouse_seed: int = 20260915
    warehouse_days: int = 546
    warehouse_start_day: str = "2025-01-01"

    # 导出
    export_dir: Path = Path(".data/exports")

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS 白名单，逗号分隔环境变量转列表。"""

        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def resolve_path(self, path: Path) -> Path:
        """把相对路径按仓库根目录解析成绝对路径（脚本与测试共用同一套解析规则）。"""

        return path if path.is_absolute() else (REPO_ROOT / path)

    @property
    def warehouse_db(self) -> Path:
        return self.resolve_path(self.warehouse_db_path)

    @property
    def app_db(self) -> Path:
        return self.resolve_path(self.app_db_path)

    @model_validator(mode="after")
    def _fail_fast_on_placeholder_secret(self) -> Settings:
        if self.app_env != "local" and self.secret_key == SECRET_PLACEHOLDER:
            raise ValueError(
                f"APP_ENV={self.app_env} 下 SECRET_KEY 仍是占位值，拒绝启动：密钥必须从环境变量注入"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置；测试可用 ``get_settings.cache_clear()`` 复位。"""

    return Settings()
