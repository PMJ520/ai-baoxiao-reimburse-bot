# -*- coding: utf-8 -*-
"""按配置选择适配器。"""
from types import SimpleNamespace

from ...config import settings
from .base import LLMClient, LLMError, parse_json  # noqa: F401
from .claude import ClaudeClient
from .cli_bridge import CLIBridgeClient
from .openai_compat import OpenAICompatClient

_PROVIDERS = {"cli": CLIBridgeClient, "cli_bridge": CLIBridgeClient,
              "claude": ClaudeClient, "anthropic": ClaudeClient,
              "openai": OpenAICompatClient, "openai_compat": OpenAICompatClient,
              "qwen": OpenAICompatClient, "deepseek": OpenAICompatClient,
              "zhipu": OpenAICompatClient, "moonshot": OpenAICompatClient}


def current_config():
    """当前生效的模型配置：后台里配的优先，环境变量兜底。

    后台设置写在数据库里，而 settings.llm 只读环境变量。两边不统一的话，
    会出现"后台测试连通正常、真正调用却说没配置"这种自相矛盾的情况。
    """
    try:
        from ...db.base import SessionLocal
        from ..settings_store import llm_config
        with SessionLocal() as s:
            return SimpleNamespace(**llm_config(s))
    except Exception:            # 数据库还没就绪（如启动早期）时退回环境变量
        return settings.llm


def get_client(cfg=None):
    cfg = cfg if cfg is not None else current_config()
    cls = _PROVIDERS.get((cfg.provider or "").lower())
    if not cls:
        raise LLMError(f"不支持的 LLM_PROVIDER：{cfg.provider}，"
                       f"可选 {'、'.join(sorted(set(_PROVIDERS)))}")
    return cls(cfg)
