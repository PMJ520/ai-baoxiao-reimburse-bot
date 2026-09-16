# -*- coding: utf-8 -*-
"""配置中心。

所有可变项从环境变量读取：Linux/macOS 由 .env 注入，Windows 由管理脚本
写入进程环境，两边共用同一份 docker-compose.yml。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key, default=None):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def _bool(key, default=False):
    v = _env(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMConfig:
    """provider 为 openai 或 claude。默认 openai——国内模型多兼容该协议，覆盖面最广。"""
    provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai"))
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))

    @property
    def ready(self):
        return bool(self.api_key)


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = field(default_factory=lambda: _env("FEISHU_APP_ID", ""))
    app_secret: str = field(default_factory=lambda: _env("FEISHU_APP_SECRET", ""))

    @property
    def ready(self):
        return bool(self.app_id and self.app_secret)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
    db_url: str = field(default_factory=lambda: _env("DB_URL", ""))
    admin_user: str = field(default_factory=lambda: _env("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN", ""))
    timezone: str = field(default_factory=lambda: _env("TZ", "Asia/Shanghai"))
    debug: bool = field(default_factory=lambda: _bool("DEBUG", False))
    llm: LLMConfig = field(default_factory=LLMConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    @property
    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite:///{self.data_dir / 'expense-hub.db'}"


settings = Settings()
