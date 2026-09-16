# -*- coding: utf-8 -*-
"""批次接口：核对预览、建批、查看。"""
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import models as M
from ..services import batch as B
from .deps import Auth, Db

router = APIRouter(prefix="/batches", tags=["batches"])


class Period(BaseModel):
    start: datetime
    end: datetime
    owner: str | None = None
    title: str | None = None


def _view(rec):
    """把核对结果压成便于阅读与在 IM 里转述的结构。"""
    return {
        "summary": rec["summary"],
        "allocations": rec["allocations"],
        "gaps": [{"type": g["type"], "message": g["message"]} for g in rec["gaps"]],
        "payments": [{"id": p["id"], "occurred_at": p["occurred_at"],
                      "amount": p["amount"], "merchant": p.get("merchant"),
                      "invoice_id": p.get("invoice_id"),
                      "invoice_amount": p.get("invoice_amount")}
                     for p in rec["payments"]],
    }


@router.post("/preview", dependencies=[Auth])
def preview(body: Period, session=Db):
    """只核对不落库——先让用户看清楚再决定要不要建批。"""
    return _view(B.reconcile(session, body.start, body.end))


@router.post("", dependencies=[Auth])
def create(body: Period, session=Db):
    b, rec = B.create_batch(session, body.start, body.end,
                            owner=body.owner, title=body.title)
    return {"batch_id": b.id, "status": b.status, **_view(rec)}


@router.get("/{batch_id}", dependencies=[Auth])
def get_batch(batch_id: int, session=Db):
    b = session.get(M.Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    return {
        "id": b.id, "status": b.status, "title": b.title,
        "period": [b.period_start, b.period_end],
        "requester": b.requester, "handler": b.handler, "company": b.company,
        "items": [{"seq": i.seq, "occurred_at": i.occurred_at,
                   "amount": float(i.amount) if i.amount is not None else None,
                   "invoice_amount": float(i.invoice_amount)
                   if i.invoice_amount is not None else None,
                   "detail": i.detail, "category": i.category,
                   "voucher_type": i.voucher_type} for i in b.items],
    }


class ItemPatch(BaseModel):
    """确认或修改某条目的明细与类别。这两个字段是对话的产物。"""
    seq: int
    detail: str | None = None
    category: str | None = None
    note: str | None = None


class People(BaseModel):
    requester: str | None = None
    handler: str | None = None
    company: str | None = None


@router.post("/{batch_id}/propose", dependencies=[Auth])
def propose(batch_id: int, hint: str = "", session=Db):
    """让模型为尚未填写的条目提议明细与类别，连同待补信息一并返回。

    只提议不落库——必须经用户确认才写入，避免把没问清的内容发给财务。
    """
    from ..services.enrich import pending_fields, propose as do_propose
    b = session.get(M.Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    todo = [it for it in b.items if not it.detail]
    payload = [{"id": it.seq,
                "occurred_at": it.occurred_at,
                "amount": float(it.amount) if it.amount is not None else None,
                "merchant": it.document.merchant if it.document else None}
               for it in todo]
    props = do_propose(payload, hint=hint)
    if not props:
        return {"available": False,
                "reason": "模型不可用或未返回有效结果，请直接人工填写",
                "items": []}
    return {
        "available": True,
        "items": [{"seq": k, **v} for k, v in sorted(props.items())],
        "pending_fields": pending_fields(props),
    }


@router.patch("/{batch_id}/items", dependencies=[Auth])
def patch_items(batch_id: int, body: list[ItemPatch], session=Db):
    b = session.get(M.Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    by_seq = {it.seq: it for it in b.items}
    changed = 0
    for row in body:
        it = by_seq.get(row.seq)
        if not it:
            continue
        for k, v in row.model_dump(exclude_unset=True, exclude={"seq"}).items():
            setattr(it, k, v)
        changed += 1
    filled = sum(1 for it in b.items if it.detail and it.category)
    b.status = M.B_READY if filled == len(b.items) else M.B_CONFIRMING
    session.commit()
    return {"updated": changed, "filled": filled, "total": len(b.items),
            "status": b.status}


@router.patch("/{batch_id}/people", dependencies=[Auth])
def patch_people(batch_id: int, body: People, session=Db):
    """需求人、经办人、出账主体。两人可能不同，不做默认相等的推断。"""
    b = session.get(M.Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    session.commit()
    return {"requester": b.requester, "handler": b.handler, "company": b.company}
