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
    #: 贡献占比门禁：切片对总变动的贡献占比低于该值时，不允许把该切片称为"主因"
    #: （依据：需求说明书 §5.11「每条结论必须绑定量化贡献」）
    attr_min_contribution_share: float = 0.05
    # 统计检验与置信度：效应量分档阈值（小/大），以及证据所需的最少覆盖天数
    attr_effect_small: float = 0.2
    attr_effect_large: float = 0.8
    attr_required_coverage_days: int = 21
    # 观察窗口 42 天：检验用「切片内 vs 切片外」两组日序列（42+42=84 个观测），
    # 满足 §5.9 第 4 条"样本量 ≥ 60"；贡献额比对仍用注入窗口本身，两者不混用
    attr_observation_days: int = 42
    attr_permutations: int = 10_000
    attr_event_window_days: int = 3
    # 异动判定阈值（需求说明书 §5.4：阈值调整要留痕）
    attr_change_threshold: float = 0.05
    attr_z_threshold: float = 2.0
    # What-If 推演（技术规格 §5.6、需求说明书 §5.8）
    #: 弹性估计窗口天数：按天序列要 ≥60 个观测点才走对数回归（主路径）
    attr_whatif_window_days: int = 120
    #: 调整档位默认上限：超出这个幅度只算外推，必须在输出里显式警告
    attr_whatif_max_adjustment: float = 0.30
    #: bootstrap 次数与固定种子：同一输入必须得到完全一致的输出
    attr_whatif_bootstrap_iterations: int = 1000
    attr_whatif_seed: int = 20260915

    # 大模型（只产出假设文本、SQL 草案与报告文字；数值一律不进模型）
    llm_provider: str = "openai_compat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048

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
