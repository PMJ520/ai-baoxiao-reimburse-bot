# -*- coding: utf-8 -*-
"""用户台账默认信息的读写，以及出账主体的自动识别。"""
import logging

from sqlalchemy import select

from ..db import models as M

log = logging.getLogger(__name__)


def get(session, platform, user_id):
    if not user_id:
        return None
    return session.scalar(select(M.UserProfile).where(
        M.UserProfile.platform == platform, M.UserProfile.user_id == user_id))


def save(session, platform, user_id, *, requester=None, handler=None, company=None):
    """记住本次用的信息，下次直接沿用。只覆盖有值的字段。"""
    if not user_id:
        return None
    prof = get(session, platform, user_id)
    if not prof:
        prof = M.UserProfile(platform=platform, user_id=user_id)
        session.add(prof)
    for k, v in (("requester", requester), ("handler", handler), ("company", company)):
        if v:
            setattr(prof, k, v)
    session.commit()
    return prof


def company_from_invoices(invoice_docs):
    """出账主体取自发票购买方——发票上写的就是报销主体。

    多张发票购买方不一致时返回 None，不猜；交由用户指定。
    """
    buyers = set()
    for d in invoice_docs:
        b = (d.parsed or {}).get("buyer")
        if b:
            buyers.add(b)
    return buyers.pop() if len(buyers) == 1 else None
