# -*- coding: utf-8 -*-
"""组批与核对：按消费时间范围取件、配对发票、分摊汇总票、查漏。

这一层只做确定性计算，不碰"这笔钱花在什么事上"的判断——
费用明细与费用类别由对话层补全后写回 ExpenseItem。
"""
from datetime import datetime

from sqlalchemy import select

from .. import storage
from ..db import models as M
from ..pipeline import matcher as MT
from ..pipeline import rules as R
from ..pipeline.parsers import parse_itinerary


def _doc_dict(d: M.Document):
    return {"id": d.id, "amount": float(d.amount) if d.amount is not None else None,
            "occurred_at": d.occurred_at, "merchant": d.merchant,
            "filename": d.filename, "status": d.status}


def collect(session, start: datetime, end: datetime, include_used=False):
    """取时间范围内的支付与发票。默认排除已用于其它批次的，防止重复报销。"""
    def q(kind):
        stmt = select(M.Document).where(M.Document.kind == kind)
        if kind == M.KIND_PAYMENT:
            stmt = stmt.where(M.Document.occurred_at.between(start, end))
        if not include_used:
            stmt = stmt.where(M.Document.status != M.ST_USED,
                              M.Document.status != M.ST_EXCLUDED)
        return list(session.scalars(stmt.order_by(M.Document.occurred_at)))

    return q(M.KIND_PAYMENT), q(M.KIND_INVOICE), q(M.KIND_ITINERARY)


def _itineraries(docs):
    out = []
    for d in docs:
        it = d.parsed if (d.parsed or {}).get("rows") else None
        if it is None:                 # 本次改动之前入库的行程单没存解析结果
            try:
                it = parse_itinerary(str(storage.abs_path(d.rel_path)))
            except Exception:
                continue
        rows = []
        for r in it.get("rows", []):
            try:
                rows.append({"time_dt": datetime.strptime(r["time"], "%Y-%m-%d %H:%M"),
                             "amt": r["amt"], "route": r.get("route", ""),
                             "car": r.get("car", ""),
                             # 带上来源：归档时要说清本次用了这份行程单的哪几趟，
                             # 剩下的趟次往往属于别的批次
                             "doc_id": d.id, "n": r.get("n")})
            except (ValueError, KeyError):
                continue
        if rows:
            out.append({"doc_id": d.id, "file": d.filename,
                        "total": it["total"], "rows": rows})
    return out


def _attach_trips(pays, itins):
    """把行程单里的起止地点贴到对应的支付记录上。

    这些是单据里白纸黑字写着的事实，不该再去问用户。匹配不唯一时留空，
    让它走"需要你补充"的正常流程——宁可多问一句，不能张冠李戴。
    """
    rows = [r for it in itins for r in it["rows"]]
    for p in pays:
        trip = MT.find_trip(rows, p.get("occurred_at"), p.get("amount"))
        if trip:
            p["route"] = trip.get("route") or ""
            p["car"] = trip.get("car") or ""


def reconcile(session, start: datetime, end: datetime):
    """核对：返回配对结果、分摊方案、待办与金额汇总。不写库，纯计算。"""
    pay_docs, inv_docs, itin_docs = collect(session, start, end)
    pays = [_doc_dict(d) for d in pay_docs]
    invs = [_doc_dict(d) for d in inv_docs]

    pays, _ = MT.match(pays, invs)                       # 先 1:1 等额
    unmatched = [v for v in invs
                 if not any(p["invoice_id"] == v["id"] for p in pays)]

    itins = _itineraries(itin_docs)
    _attach_trips(pays, itins)                           # 起止地点等已知信息回填
    plans, alloc_gaps = MT.plan_allocation(pays, unmatched, itins)
    checks = []
    for pl in plans:                                      # B：单据驱动自动分摊
        pays, chk = MT.allocate_invoice(pays, pl["invoice"], pl["shares"])
        chk["source_doc_id"] = pl["source_doc_id"]
        chk["matched"] = pl["matched"]
        chk["expected"] = pl["expected"]
        checks.append(chk)

    still_unmatched = [v for v in invs
                       if not any(p["invoice_id"] == v["id"] for p in pays)]
    gaps = MT.find_gaps(pays, []) + alloc_gaps            # A：交对话确认
    for fn in R.for_categories([]):                       # 领域规则（按类型可扩展）
        gaps.extend(fn(pays) or [])
    return {
        "period": [start, end],
        "payments": pays, "invoices": invs,
        "unmatched_invoices": still_unmatched,
        "allocations": checks, "gaps": gaps,
        "summary": MT.summarize(pays, invs),
    }


def create_batch(session, start, end, owner=None, title=None, platform="feishu"):
    """按核对结果建批次并生成条目。明细与类别留空，等对话补全。

    需求人、经办人沿用该用户上次填的；出账主体取自发票购买方。
    预填不等于确认——对话里仍会让用户过目，只是从"填空"变成"确认或修改"。
    """
    from . import profile as PF

    rec = reconcile(session, start, end)
    _pay, inv_docs, _itin = collect(session, start, end)
    prof = PF.get(session, platform, owner)
    b = M.Batch(owner=owner, title=title, period_start=start, period_end=end,
                status=M.B_DRAFT,
                requester=prof.requester if prof else None,
                handler=prof.handler if prof else None,
                company=(PF.company_from_invoices(inv_docs)
                         or (prof.company if prof else None)))
    session.add(b)
    session.flush()
    for i, p in enumerate(rec["payments"], 1):
        paid = p.get("amount")
        cov = p.get("invoice_amount")
        b.items.append(M.ExpenseItem(
            seq=i, document_id=p["id"], invoice_document_id=p.get("invoice_id"),
            occurred_at=p.get("occurred_at"), amount=paid, invoice_amount=cov,
            voucher_type=_voucher(paid, cov),
        ))
    session.commit()
    return b, rec


def _voucher(paid, covered):
    if covered is None:
        return "替票"
    if paid is not None and round(float(paid) - float(covered), 2) > 0:
        return "发票+替票"
    return "发票"


def mark_used(session, batch: M.Batch):
    """把批次涉及的文件标记为已使用——防止下次整理重复计入。"""
    ids = {it.document_id for it in batch.items if it.document_id}
    ids |= {it.invoice_document_id for it in batch.items if it.invoice_document_id}
    for d in session.scalars(select(M.Document).where(M.Document.id.in_(ids))):
        d.status = M.ST_USED
    session.commit()


def trip_info(session, batch):
    """批次内每笔支付对应的行程信息，键为条目 seq。

    不落库：行程单本身就是权威来源，按时间金额回查即可，免得同一份事实
    存两处还要考虑迁移。不按周期过滤——匹配靠金额相等加时间窗口，
    且命中不唯一时会放弃，跨周期误配的风险低于漏配。
    """
    docs = list(session.scalars(
        select(M.Document).where(M.Document.kind == M.KIND_ITINERARY)))
    rows = [r for it in _itineraries(docs) for r in it["rows"]]
    if not rows:
        return {}
    out = {}
    for it in batch.items:
        trip = MT.find_trip(rows, it.occurred_at,
                            float(it.amount) if it.amount is not None else None)
        if trip and trip.get("route"):
            out[it.seq] = {"route": trip["route"], "car": trip.get("car", ""),
                           "itinerary_doc_id": trip.get("doc_id"),
                           "trip_n": trip.get("n")}
    return out
