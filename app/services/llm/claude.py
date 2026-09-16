# -*- coding: utf-8 -*-
"""Claude API 适配器。"""
import httpx

from .base import LLMError

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"


class ClaudeClient:
    def __init__(self, cfg):
        if not cfg.api_key:
            raise LLMError("未配置 LLM_API_KEY")
        self.key = cfg.api_key
        self.model = cfg.model or DEFAULT_MODEL
        self.base = (cfg.base_url or API).rstrip("/")
        if not self.base.endswith("/messages"):
            self.base = self.base + "/v1/messages"

    def complete(self, system, user, *, max_tokens=2048, temperature=0.2):
        r = httpx.post(
            self.base,
            headers={"x-api-key": self.key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "temperature": temperature, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=120,
        )
        if r.status_code >= 400:
            raise LLMError(f"Claude 接口错误 {r.status_code}: {r.text[:300]}")
        blocks = r.json().get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
