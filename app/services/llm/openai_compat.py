# -*- coding: utf-8 -*-
"""OpenAI 兼容适配器：通义千问、DeepSeek、智谱、Moonshot 等均可用。"""
import httpx

from .base import LLMError

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICompatClient:
    def __init__(self, cfg):
        if not cfg.api_key:
            raise LLMError("未配置 LLM_API_KEY")
        self.key = cfg.api_key
        self.model = cfg.model or DEFAULT_MODEL
        base = (cfg.base_url or DEFAULT_BASE).rstrip("/")
        self.url = base if base.endswith("/chat/completions") \
            else base + "/chat/completions"

    def complete(self, system, user, *, max_tokens=2048, temperature=0.2):
        r = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "temperature": temperature,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=120,
        )
        if r.status_code >= 400:
            raise LLMError(f"模型接口错误 {r.status_code}: {r.text[:300]}")
        ch = (r.json().get("choices") or [{}])[0]
        return (ch.get("message") or {}).get("content", "")
