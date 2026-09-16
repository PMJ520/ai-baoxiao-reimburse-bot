# -*- coding: utf-8 -*-
"""发票与支付的配对，以及查漏。

纯函数：进出都是普通数据结构，不碰数据库也不碰文件系统，便于单测与复用。

配对依据是金额相等。一张发票也可能汇总多笔支付（如滴滴月度开票），
这种配不上——刻意不猜，留给对话环节向用户确认。
"""
from datetime import timedelta

TOL = 0.01          # 金额比较容差
GAP_UNPAID = "payment_without_invoice"
GAP_ORPHAN_INVOICE = "invoice_without_payment"
GAP_UNPARSED = "unparsed_field"
GAP_NEEDS_DETAIL = "missing_detail"


def _amt(x):
    v = x.get("amount")
    return None if v is None else float(v)


def match(payments, invoices, window_days=None):
    """把发票配到支付上。返回 (配对后的支付列表, 未配对发票列表)。

    payments / invoices 均为 dict，至少含 id、amount，可含 occurred_at。
    不修改入参，返回新对象。
    """
    pays = [dict(p, invoice_id=None, invoice_amount=None) for p in payments]
    invs = [dict(v, matched_payment_id=None) for v in invoices]
    used = set()

    for v in invs:
        total = _amt(v)
        if total is None:
            continue
        cands = [p for p in pays
                 if p["id"] not in used and _amt(p) is not None
                 and abs(_amt(p) - total) < TOL]
        if window_days is not None:
            cands = [p for p in cands if _within(p, v, window_days)]
        if not cands:
            continue
        hit = _closest_in_time(cands, v)
        used.add(hit["id"])
        hit["invoice_id"] = v["id"]
        hit["invoice_amount"] = total
        v["matched_payment_id"] = hit["id"]

    return pays, [v for v in invs if v["matched_payment_id"] is None]


def _within(p, v, days):
    a, b = p.get("occurred_at"), v.get("occurred_at")
    if not a or not b:
        return True                       # 缺时间就不以时间设限，交给金额判断
    return abs(a - b) <= timedelta(days=days)


def _closest_in_time(cands, v):
    """金额相同的多笔里取时间最接近的，时间缺失则取第一笔。"""
    ref = v.get("occurred_at")
    if not ref:
        return cands[0]
    dated = [c for c in cands if c.get("occurred_at")]
    if not dated:
        return cands[0]
    return min(dated, key=lambda c: abs(c["occurred_at"] - ref))


def find_gaps(payments, invoices_unmatched, rules=()):
    """查漏。返回待办列表，每项含 type / 相关对象 / 面向用户的说明。

    rules 为按费用类型挂载的领域规则，形如 f(payments) -> [gap]，
    便于后续扩展餐饮、加油等场景，而不必改动通用逻辑。
    """
    gaps = []
    for p in payments:
        if _amt(p) is None or not p.get("occurred_at"):
            gaps.append({"type": GAP_UNPARSED, "payment": p,
                         "message": f"{p.get('filename') or p['id']} 的金额或时间没能识别，"
                                    f"需要确认"})
        elif not p.get("invoice_id"):
            gaps.append({"type": GAP_UNPAID, "payment": p,
                         "message": f"{_fmt(p)} 没有对应发票，需要替票"})
    for v in invoices_unmatched:
        gaps.append({"type": GAP_ORPHAN_INVOICE, "invoice": v,
                     "message": f"发票 {v.get('filename')} {_amt(v)} 元找不到对应支付记录，"
                                f"是公司卡付的、漏了截图，还是汇总开票？"})
    for rule in rules:
        gaps.extend(rule(payments) or [])
    return gaps


def _fmt(p):
    t = p.get("occurred_at")
    return f"{t:%Y-%m-%d %H:%M} {_amt(p):.2f}元 {p.get('merchant') or ''}".strip() \
        if t else f"{_amt(p):.2f}元"


def summarize(payments, invoices):
    """金额汇总：实付、有发票、需替票。与台账的口径保持一致。"""
    total = round(sum(_amt(p) or 0 for p in payments), 2)
    inv_covered = round(sum(p.get("invoice_amount") or 0 for p in payments), 2)
    actual_inv = round(sum(_amt(v) or 0 for v in invoices), 2)
    return {
        "total": total, "n_total": len(payments),
        "invoice_covered": inv_covered,
        "actual_invoice": actual_inv,
        "missing_invoice": round(inv_covered - actual_inv, 2),
        "substitute": round(total - inv_covered, 2),
        "n_substitute": sum(1 for p in payments
                            if round((_amt(p) or 0) - (p.get("invoice_amount") or 0), 2) > 0),
    }


# ---------- 汇总开票的分摊 ----------
# 一张发票常覆盖多笔支付（如按城市按周期开票）。两条路径：
#   B 单据驱动：有辅助单据（行程单）时，用它的明细精确分摊，自动完成
#   A 对话确认：没有辅助单据时不猜，列出候选交给用户指定
# 刻意不做子集和自动匹配——实测它会凑出金额恰好、构成却完全错误的组合，
# 对财务数据而言这种"看似正确的错误"比匹配不上危险得多。
GAP_NEEDS_ALLOCATION = "invoice_needs_allocation"


def match_by_document(payments, trips, window_min=30):
    """用辅助单据的明细把支付对上条目。

    单据上的时间是上车/消费时间，支付截图上的时间通常略早几分钟，
    故取"支付时间不晚于单据时间、且相差在窗口内"的最近一笔。
    返回 {payment_id: trip}。
    """
    used, out = set(), {}
    for t in trips:
        tt = t.get("time_dt") or t.get("occurred_at")
        if not tt:
            continue
        cands = []
        for p in payments:
            pt = p.get("occurred_at")
            if p["id"] in used or not pt:
                continue
            gap = (tt - pt).total_seconds() / 60
            if 0 <= gap <= window_min:
                cands.append((gap, p))
        if not cands:
            continue
        cands.sort(key=lambda x: x[0])
        hit = cands[0][1]
        used.add(hit["id"])
        out[hit["id"]] = t
    return out


def allocate_invoice(payments, invoice, shares):
    """把一张发票按 {payment_id: 开票金额} 分摊到多笔支付。

    返回 (更新后的支付列表, 校验信息)。分摊合计与发票金额不符时如实报出，
    不做静默修正——差额往往意味着漏了某笔或多算了某笔。
    """
    pays = [dict(p) for p in payments]
    total = 0.0
    for p in pays:
        amt = shares.get(p["id"])
        if amt is None:
            continue
        p["invoice_id"] = invoice["id"]
        p["invoice_amount"] = round(float(amt), 2)
        total += float(amt)
    total = round(total, 2)
    inv_total = _amt(invoice) or 0.0
    check = {
        "invoice_id": invoice["id"], "invoice_total": inv_total,
        "allocated": total, "diff": round(total - inv_total, 2),
        "ok": abs(total - inv_total) < TOL,
        "n_payments": len(shares),
    }
    return pays, check


def plan_allocation(payments, unmatched_invoices, itineraries):
    """为未配对的发票规划分摊方案。

    itineraries 形如 [{"doc_id":..,"total":..,"rows":[{"time_dt":..,"amt":..}]}]。
    发票金额与某份单据合计相等时走 B 自动分摊；否则生成 A 的待确认项。
    返回 (分摊方案列表, 待人工确认的 gap 列表)。
    """
    plans, gaps = [], []
    used_docs = set()
    for inv in unmatched_invoices:
        total = _amt(inv)
        src = next((it for it in itineraries
                    if it["doc_id"] not in used_docs and total is not None
                    and abs(it["total"] - total) < TOL), None)
        if not src:
            gaps.append({
                "type": GAP_NEEDS_ALLOCATION, "invoice": inv,
                "message": f"发票 {inv.get('filename')} {total} 元疑为汇总开票，"
                           f"未找到金额吻合的辅助单据，需要指定它覆盖哪几笔支付",
            })
            continue
        used_docs.add(src["doc_id"])
        hit = match_by_document(payments, src["rows"])
        shares = {pid: t["amt"] for pid, t in hit.items()}
        plans.append({"invoice": inv, "source_doc_id": src["doc_id"],
                      "shares": shares, "matched": len(shares),
                      "expected": len(src["rows"])})
    return plans, gaps


# ---- 行程单 → 支付记录的回填 ----

TRIP_WINDOW_MIN = 30      # 下单与支付之间的时间差，超过这个数不认为是同一趟


def find_trip(rows, when, amount):
    """在行程明细里找出对应这笔支付的那一趟。

    金额必须相等，时间差在窗口内。**命中多条时返回 None**——同金额同时段
    的行程确实存在（比如往返同价），猜错了就是把 A 的起止地点安到 B 头上，
    而这种错误在台账里几乎看不出来。宁可留空去问用户。
    """
    if when is None or amount is None:
        return None
    hits = []
    for r in rows:
        t, a = r.get("time_dt"), r.get("amt")
        if t is None or a is None:
            continue
        if abs(float(a) - float(amount)) > 0.005:
            continue
        if abs((t - when).total_seconds()) > TRIP_WINDOW_MIN * 60:
            continue
        hits.append(r)
    return hits[0] if len(hits) == 1 else None
