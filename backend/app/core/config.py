"""全局配置。

加载优先级（高 → 低）：构造参数 > 环境变量（前缀 SA_）> .env 文件 > config/config.yaml > 默认值。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from app.core.network import DEFAULT_DIRECT_HOSTS

# backend/app/core/config.py → 项目根目录 StockTrading/
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"


def _config_file() -> Path:
    return Path(os.environ.get("SA_CONFIG_FILE", PROJECT_ROOT / "config" / "config.yaml"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SA_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "StockArena"
    version: str = "0.1.0"
    env: str = "dev"

    host: str = "127.0.0.1"
    # 端口选在 15000 以上：本机的 Windows 动态端口范围被改成了 1024~15000，低端口可能被其他程序的出站连接占用
    port: int = 18080
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:15173", "http://localhost:15173"]
    )

    data_dir: Path = PROJECT_ROOT / "data"
    # 为空时使用 data_dir 下的 SQLite 文件
    database_url: str | None = None
    # 启动时自动执行数据库迁移（单用户本地运行时方便；多用户部署时应关闭，改为发布流程中执行）
    auto_migrate: bool = True

    # 部署时由后端托管前端打包目录（frontend/dist），页面与接口同一端口；为空时不托管（开发时由 vite 提供页面）
    frontend_dist: Path | None = None
    # 对外访问地址（如 http://your-server:18080），用于推送消息中的链接；为空时不附链接
    public_url: str | None = None
    # 源代码地址：页面底部显示（AGPL-3.0 第 13 条：通过网络提供服务时要让用户能拿到源代码）。
    # 修改了代码并对外提供服务时，请改成你自己公开修改后代码的地址
    source_url: str = "https://github.com/PandaPanY/stockarena"

    # 访问保护：接口需要登录（密码在首次打开页面时设置）。测试时关闭
    auth_enabled: bool = True

    # 技能（在 Docker 容器里执行第三方代码）。不写时：已安装过技能的旧安装保持开启，新安装关闭（见 app/skills/gate.py）
    skills_enabled: bool | None = None

    log_level: str = "INFO"

    # 自动按收盘时间执行赛道日终流程（测试时关闭）
    auto_run: bool = True

    # 不经过系统代理、直接访问的域名（国内行情源）
    direct_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_DIRECT_HOSTS))

    # 交易规则的全局默认值（三级继承的中间层：系统默认 → 全局配置 → 规则集），键为 RuleParams 的字段名
    rule_defaults: dict[str, Any] = Field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        return PROJECT_ROOT / "config"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'stockarena.db').as_posix()}"

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = YamlConfigSettingsSource(
            settings_cls, yaml_file=_config_file(), yaml_file_encoding="utf-8"
        )
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
