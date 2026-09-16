# -*- coding: utf-8 -*-
"""文件相关接口：上传、查询、人工订正。"""
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from ..db import models as M
from ..services import ingest
from .deps import Auth, Db

router = APIRouter(prefix="/documents", tags=["documents"])


class DocOut(BaseModel):
    id: int
    filename: str
    kind: str
    status: str
    occurred_at: datetime | None = None
    amount: float | None = None
    merchant: str | None = None
    parse_error: str | None = None

    class Config:
        from_attributes = True


class DocPatch(BaseModel):
    """人工订正解析结果。识别不准时由人补，不让错误数据流进台账。"""
    occurred_at: datetime | None = None
    amount: float | None = None
    merchant: str | None = None
    kind: str | None = None
    status: str | None = None


@router.post("", response_model=DocOut, dependencies=[Auth])
async def upload(file: UploadFile = File(...), source: str = Form("web"),
                 uploader: str | None = Form(None), session=Db):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    doc, created = ingest.ingest_bytes(session, data, file.filename or "unnamed",
                                       source=source, uploader=uploader)
    return DocOut.model_validate(doc, from_attributes=True).model_copy(
        update={"parse_error": doc.parse_error if created else "内容重复，已复用既有记录"})


@router.get("", response_model=list[DocOut], dependencies=[Auth])
def list_docs(kind: str | None = None, status: str | None = None,
              start: datetime | None = None, end: datetime | None = None,
              limit: int = Query(200, le=1000), session=Db):
    q = select(M.Document)
    if kind:
        q = q.where(M.Document.kind == kind)
    if status:
        q = q.where(M.Document.status == status)
    if start:
        q = q.where(M.Document.occurred_at >= start)
    if end:
        q = q.where(M.Document.occurred_at <= end)
    q = q.order_by(M.Document.occurred_at.desc().nullslast()).limit(limit)
    return [DocOut.model_validate(d, from_attributes=True) for d in session.scalars(q)]


@router.patch("/{doc_id}", response_model=DocOut, dependencies=[Auth])
def patch_doc(doc_id: int, body: DocPatch, session=Db):
    doc = session.get(M.Document, doc_id)
    if not doc:
        raise HTTPException(404, "文件不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(doc, k, v)
    if doc.amount is not None and doc.occurred_at is not None \
            and doc.status == M.ST_NEEDS_REVIEW:
        doc.status = M.ST_PARSED           # 补齐后自动解除待复核
        doc.parse_error = None
    session.commit()
    return DocOut.model_validate(doc, from_attributes=True)
