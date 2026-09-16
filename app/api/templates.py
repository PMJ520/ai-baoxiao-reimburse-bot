# -*- coding: utf-8 -*-
"""专用模版接口：上传样例推断、查看校验、确认启用。"""
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..db import models as M
from ..services import templates as TPL
from .deps import Auth, Db

router = APIRouter(prefix="/templates", tags=["templates"])


class ConfirmBody(BaseModel):
    spec: dict | None = None          # 人工修正后的 spec，不传则沿用推断结果
    make_default: bool = True


@router.get("", dependencies=[Auth])
def listing(session=Db):
    return TPL.listing(session)


@router.post("", dependencies=[Auth])
async def register(file: UploadFile = File(...), name: str = Form(...),
                   activate: bool = Form(False), session=Db):
    """上传一份**已填好数据**的模版样例，推断映射并做独立事实校验。

    校验不通过不会自动启用——错误的映射会让交给财务的表出错。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "x.xlsx").suffix,
                                     delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        tpl, report = TPL.register(session, tmp, name, activate=activate)
    except Exception as e:
        raise HTTPException(422, f"推断失败：{e}")
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"id": tpl.id, "name": tpl.name, "is_default": tpl.is_default,
            "report": report, "spec": {k: v for k, v in tpl.spec.items()
                                       if not k.startswith("_")}}


@router.get("/{template_id}", dependencies=[Auth])
def detail(template_id: int, session=Db):
    t = session.get(M.Template, template_id)
    if not t:
        raise HTTPException(404, "模版不存在")
    spec = t.spec or {}
    return {"id": t.id, "name": t.name, "is_default": t.is_default,
            "report": spec.get("_report"),
            "spec": {k: v for k, v in spec.items() if not k.startswith("_")}}


@router.post("/{template_id}/confirm", dependencies=[Auth])
def confirm(template_id: int, body: ConfirmBody, session=Db):
    try:
        t = TPL.confirm(session, template_id, body.spec, body.make_default)
    except Exception as e:
        raise HTTPException(422, str(e))
    return {"id": t.id, "name": t.name, "is_default": t.is_default}
