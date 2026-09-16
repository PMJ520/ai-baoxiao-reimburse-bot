# -*- coding: utf-8 -*-
"""文件入库：落盘 → 识别类型 → 解析 → 写 Document。

入库即解析，不等到整理时才跑 OCR——这样"整理某段时间的报销"只是一次
数据库查询，秒级返回；也能在收到当下就把识别结果回给用户核对。
"""
import logging
from datetime import datetime

from sqlalchemy import select

from .. import storage
from ..db import models as M
from ..pipeline import parsers as P
from ..pipeline.ocr import engine as ocr

log = logging.getLogger(__name__)

IMG_EXT = (".png", ".jpg", ".jpeg", ".heic", ".webp")


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except ValueError:
            continue
    return None


def detect_kind(filename, abs_path):
    low = filename.lower()
    if low.endswith(".pdf"):
        return M.KIND_INVOICE if P.is_invoice(abs_path) else M.KIND_ITINERARY
    if low.endswith(IMG_EXT):
        return M.KIND_PAYMENT
    return M.KIND_UNKNOWN


def parse_document(doc: M.Document):
    """就地解析并回填字段。失败不抛，转为 needs_review 让人工介入。"""
    path = str(storage.abs_path(doc.rel_path))
    try:
        if doc.kind == M.KIND_INVOICE:
            v = P.parse_invoice(path)
            doc.amount = v.get("total")
            doc.occurred_at = _parse_dt(v.get("date"))
            doc.merchant = v.get("seller") or ""
            doc.parsed = v
        elif doc.kind == M.KIND_PAYMENT:
            blocks = ocr.recognize([path])
            items = blocks.get(doc.filename) or next(iter(blocks.values()), [])
            s = P.read_shot(items)
            doc.amount = s.get("amount")
            doc.occurred_at = _parse_dt(s.get("time"))
            doc.merchant = s.get("merchant") or ""
            doc.parsed = s
        elif doc.kind == M.KIND_ITINERARY:
            # 行程单的起止地点是后面提议费用明细时的关键依据：有了它就不必
            # 再去问用户"这趟从哪到哪"。入库时存下来，省得每次核对重新解析
            v = P.parse_itinerary(path)
            doc.parsed = v
            rows = v.get("rows") or []
            if rows:
                doc.occurred_at = _parse_dt(rows[0].get("time"))
            doc.merchant = v.get("city") or ""
        else:
            doc.parsed = {}
    except ocr.NeedManualOCR:
        doc.status = M.ST_NEEDS_REVIEW
        doc.parse_error = "无可用的文字识别引擎，需人工填写"
        return doc
    except Exception as e:                       # 解析失败不能吞掉
        log.exception("解析失败 %s", doc.filename)
        doc.status = M.ST_NEEDS_REVIEW
        doc.parse_error = f"{type(e).__name__}: {e}"
        return doc

    # 只有支付与发票必须有金额和时间；行程单等辅助单据没有单一金额，不算缺失
    needs_amount = doc.kind in (M.KIND_PAYMENT, M.KIND_INVOICE)
    missing = ([n for n, v in (("金额", doc.amount), ("时间", doc.occurred_at)) if v is None]
               if needs_amount else [])
    if missing:
        doc.status = M.ST_NEEDS_REVIEW
        doc.parse_error = "未识别到" + "、".join(missing)
    else:
        doc.status = M.ST_PARSED
        doc.parse_error = None
    return doc


def ingest_bytes(session, data: bytes, filename: str, *, source="web",
                 external_id=None, uploader=None, parse=True):
    """存入一个文件。内容相同则复用已有记录（IM 里重复发图很常见）。

    返回 (Document, 是否新建)。
    """
    digest, rel, is_new_blob = storage.put_bytes(data, filename)
    existing = session.scalar(select(M.Document).where(M.Document.sha256 == digest))
    if existing:
        return existing, False

    doc = M.Document(
        sha256=digest, filename=filename, rel_path=rel, size=len(data),
        source=source, external_id=external_id, uploader=uploader,
        kind=detect_kind(filename, str(storage.abs_path(rel))),
        status=M.ST_RECEIVED,
    )
    if parse:
        parse_document(doc)
    session.add(doc)
    session.commit()
    return doc, True


def ingest_path(session, path, **kw):
    from pathlib import Path
    p = Path(path)
    return ingest_bytes(session, p.read_bytes(), p.name, **kw)
