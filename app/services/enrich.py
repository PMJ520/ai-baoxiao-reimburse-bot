# -*- coding: utf-8 -*-
"""费用明细与类别的提议。

这是模型唯一介入的环节。金额、时间、凭证关系都由确定性代码算好，
模型只做一件事：根据商户和发票项目，推断"这笔钱花在什么事上"。

硬性约束：**不许编造事实**。人数、宴请对象、出差事由这类信息只有用户
知道，模型必须把它们标成待补占位，而不是替用户想一个看着合理的说法。
"""
import logging

from .llm import get_client, parse_json

log = logging.getLogger(__name__)

CATEGORIES = ["交通费", "差旅费", "招待费", "办公费", "通讯费", "餐饮费", "其他"]

SYSTEM = """你在帮用户整理公司费用报销台账。用户会给你若干笔已经解析好的消费记录，
你要为每一笔提议「费用具体明细」和「费用类别」。

规则：
1. 费用具体明细要写清楚这笔钱花在什么事上，供财务审阅。依据是商户名称和发票项目。
2. **绝对不要编造你不知道的事实**。宴请人数、客户姓名、出差事由等信息，
   你无从得知，必须原样写成占位符，例如「业务招待——海底捞（客户：__，__人）」，
   并在 needs 字段里列出需要用户补充的项。
3. **已经给出的字段不要再问**。记录里若带了"起止地点"等字段，说明单据上写着，
   请直接写进明细，绝不可列进 needs——让用户重复提供已经交给你的信息，
   是在浪费他的时间。
4. 费用类别只能从这个列表里选：{categories}
5. 拿不准的用 confidence 标低，不要硬猜。
6. 只输出 JSON，不要任何解释文字。

输出格式：
{{"items": [{{"id": 1, "detail": "...", "category": "交通费",
             "needs": ["人数", "客户姓名"], "confidence": 0.9}}]}}
"""


def _brief(item):
    """把一条记录压成给模型看的最小信息。只给它判断用得上的字段。

    空字段一律不传：留着会让模型误以为"这项未知、该去问"，而它其实只是
    这类单据本来就没有。
    """
    t = item.get("occurred_at")
    d = {
        "id": item["id"],
        "时间": t.strftime("%Y-%m-%d %H:%M") if t else "未知",
        "金额": item.get("amount"),
        "商户": item.get("merchant") or "未知",
    }
    if item.get("invoice_items"):
        d["发票项目"] = item["invoice_items"]
    if item.get("route"):
        d["起止地点"] = item["route"]        # 行程单上写着，不必再问用户
    if item.get("car"):
        d["车型"] = item["car"]
    return d


def propose(items, *, client=None, hint=""):
    """为一批记录提议明细与类别。

    返回 {id: {detail, category, needs, confidence}}。
    模型不可用或解析失败时返回空字典——上层据此退回纯人工填写，不阻断流程。
    """
    items = [i for i in items if i.get("id") is not None]
    if not items:
        return {}
    try:
        cli = client or get_client()
    except Exception as e:
        log.warning("模型不可用，跳过提议：%s", e)
        return {}

    payload = {"记录": [_brief(i) for i in items]}
    if hint:
        payload["用户补充说明"] = hint
    import json
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        raw = cli.complete(SYSTEM.format(categories="、".join(CATEGORIES)), user)
        data = parse_json(raw)
    except Exception as e:
        log.warning("提议失败：%s", e)
        return {}

    out = {}
    for r in (data.get("items") or []):
        rid = r.get("id")
        cat = r.get("category")
        if rid is None:
            continue
        out[int(rid)] = {
            "detail": (r.get("detail") or "").strip(),
            # 类别必须落在白名单内，模型自创的一律退回「其他」交人工定夺
            "category": cat if cat in CATEGORIES else "其他",
            "category_raw": cat,
            "needs": r.get("needs") or [],
            "confidence": r.get("confidence"),
        }
    return out


def pending_fields(proposals):
    """汇总所有待用户补充的信息，便于在 IM 里一次问清而不是逐笔追问。"""
    agg = {}
    for rid, p in proposals.items():
        for n in p.get("needs") or []:
            agg.setdefault(n, []).append(rid)
    return agg
