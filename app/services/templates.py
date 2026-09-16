# -*- coding: utf-8 -*-
"""专用模版的注册与取用。

注册时调一次模型推断映射，经独立事实校验与人工确认后固化成 spec；
之后出表纯代码套用，不再碰模型。
"""
import logging

from sqlalchemy import select

from ..db import models as M
from ..pipeline.template_spec import TemplateSpec
from . import template_infer as TI

log = logging.getLogger(__name__)

BUILTIN = "备用金台账"


def register(session, sample_path, name, *, client=None, activate=False):
    """从样例推断并落库。返回 (Template, 校验报告)。

    校验不通过也会存下来（状态为草稿），便于人工修正后再启用——
    直接丢弃会让用户白等一次推断。
    """
    spec, raw = TI.infer(sample_path, name=name, client=client)
    report = TI.check(sample_path, spec)

    tpl = session.scalar(select(M.Template).where(M.Template.name == name))
    if not tpl:
        tpl = M.Template(name=name, kind="custom")
        session.add(tpl)
    tpl.spec = spec.to_dict()
    tpl.spec["_report"] = report
    tpl.spec["_raw_reply"] = (raw or "")[:2000]
    if activate and report["usable"]:
        _set_default(session, tpl)
    session.commit()
    return tpl, report


def confirm(session, template_id, spec_dict=None, make_default=True):
    """人工确认（可顺带修正 spec）后启用。"""
    tpl = session.get(M.Template, template_id)
    if not tpl:
        raise ValueError("模版不存在")
    if spec_dict:
        spec_dict.setdefault("name", tpl.name)
        TemplateSpec.from_dict(spec_dict)      # 结构不合法会在此抛错
        tpl.spec = spec_dict
    if make_default:
        _set_default(session, tpl)
    session.commit()
    return tpl


def _set_default(session, tpl):
    for other in session.scalars(select(M.Template).where(
            M.Template.is_default.is_(True))):
        other.is_default = False
    tpl.is_default = True


def get_spec(session, template_id=None):
    """取要用的 spec。没有自定义模版时返回 None，由调用方回退到内置实现。"""
    tpl = None
    if template_id:
        tpl = session.get(M.Template, template_id)
    if not tpl:
        tpl = session.scalar(select(M.Template).where(
            M.Template.is_default.is_(True)))
    if not tpl or not tpl.spec:
        return None, None
    d = {k: v for k, v in tpl.spec.items() if not k.startswith("_")}
    return TemplateSpec.from_dict(d), tpl


def listing(session):
    out = []
    for t in session.scalars(select(M.Template).order_by(M.Template.id)):
        rep = (t.spec or {}).get("_report") or {}
        out.append({"id": t.id, "name": t.name, "kind": t.kind,
                    "is_default": t.is_default,
                    "usable": rep.get("usable"),
                    "columns": len((t.spec or {}).get("columns") or []),
                    "diffs": rep.get("total_diffs")})
    return out
