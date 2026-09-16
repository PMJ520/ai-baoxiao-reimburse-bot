# -*- coding: utf-8 -*-
"""对话模型抽象。

只暴露一个 complete()，适配器负责协议差异。国内模型（通义、DeepSeek、
智谱等）多提供 OpenAI 兼容端点，故两个适配器即可覆盖绝大多数选择。

约束：模型只参与"这笔钱花在什么事上"的判断，金额、时间、凭证关系一律
由确定性代码计算，不经模型的手。
"""
import json
import re
from typing import Protocol


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *,
                 max_tokens: int = 2048, temperature: float = 0.2) -> str:
        ...


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def parse_json(text: str):
    """从模型回复里取出 JSON。模型偶尔会裹代码块或加解释，都要能容错。"""
    t = (text or "").strip()
    m = _JSON_BLOCK.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # 退一步：截取最外层花括号或方括号
    for lo, hi in (("{", "}"), ("[", "]")):
        i, j = t.find(lo), t.rfind(hi)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"模型未返回可解析的 JSON：{t[:200]}")
