# -*- coding: utf-8 -*-
"""按费用类型挂载的查漏规则。

通用规则（无发票、孤儿发票、解析失败）在 matcher 里；这里放领域规则——
不同费用类型该补什么资料差别很大，交通看往返结构，餐饮看人数对象，
加油看车牌。新增费用类型时加一个函数并注册，不必改动通用逻辑。
"""
from collections import defaultdict

GAP_ROUND_TRIP = "missing_round_trip"
GAP_FIELD_REQUIRED = "field_required"


def _by_day(payments):
    g = defaultdict(list)
    for p in payments:
        t = p.get("occurred_at")
        if t:
            g[t.date()].append(p)
    return g


def transport_round_trip(payments, **_):
    """交通：出差日通常是「去程—到岗—离岗—返程」的闭环，缺一段就提示。

    依据实践：某天只有单程而当天另有异地行程时，往往是漏传了截图。
    只提示不臆断——真实原因可能是同事顺路捎带，需用户确认。
    """
    gaps = []
    for day, items in sorted(_by_day(payments).items()):
        if len(items) < 2:
            continue
        times = sorted(i["occurred_at"] for i in items)
        span = (times[-1] - times[0]).total_seconds() / 3600
        # 跨度超过 8 小时却只有奇数笔，疑似缺了返程或去程
        if span >= 8 and len(items) % 2 == 1:
            gaps.append({
                "type": GAP_ROUND_TRIP, "date": str(day),
                "message": f"{day} 有 {len(items)} 笔行程、跨度 {span:.0f} 小时，"
                           f"呈单数不成对，是否漏传了某一程的截图？",
            })
    return gaps


REQUIRED_FIELDS = {
    "招待费": ["宴请对象", "人数"],      # 财务通常不认没有对象和人数的招待费
    "差旅费": ["出差事由"],
    "油费": ["车牌号"],
}


def required_fields(payments, items=(), **_):
    """按已定类别检查必填信息是否补全。"""
    gaps = []
    for it in items:
        need = REQUIRED_FIELDS.get(it.get("category") or "")
        if not need:
            continue
        detail = it.get("detail") or ""
        missing = [n for n in need if n not in detail and "__" in detail] or \
                  ([] if detail else need)
        if missing:
            gaps.append({
                "type": GAP_FIELD_REQUIRED, "item_id": it.get("id"),
                "message": f"{it.get('category')}「{detail or '未填明细'}」"
                           f"还缺：{'、'.join(missing)}",
            })
    return gaps


# 规则注册表：按费用类型取用，未命中类型只跑通用规则
REGISTRY = {
    "交通费": [transport_round_trip],
    "差旅费": [transport_round_trip],
}
COMMON = [required_fields]


def for_categories(categories):
    """取这批费用涉及的所有规则，去重后返回。"""
    fns, seen = [], set()
    for c in list(categories) + ["__common__"]:
        for f in (COMMON if c == "__common__" else REGISTRY.get(c, [])):
            if f.__name__ not in seen:
                seen.add(f.__name__)
                fns.append(f)
    return fns
