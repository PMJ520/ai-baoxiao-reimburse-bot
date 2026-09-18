# -*- coding: utf-8 -*-
"""模版推断的后台执行。

推断要调模型读样例表头，提示词大、耗时长（CLI 方式几分钟很常见）。
挂在 HTTP 请求上等，浏览器、反向代理任何一环超时都会失败，页面还卡着
干不了别的。所以改成：先落库占位，线程里慢慢跑，页面轮询状态。

状态存在 spec 的 _job 字段里，不额外加表列——加列要迁移，而 SQLite 的
create_all 只建表不改表，老库升级上来会直接崩。
"""
import logging
import threading
import traceback
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from ..db.base import SessionLocal
from ..db import models as M

log = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _set_job(session, tpl, status, detail=""):
    spec = dict(tpl.spec or {})
    spec["_job"] = {"status": status, "detail": detail[:500],
                    "at": datetime.now().isoformat(timespec="seconds")}
    tpl.spec = spec
    session.commit()


def job_of(tpl):
    return (tpl.spec or {}).get("_job") or {}


class Busy(RuntimeError):
    """同名模版正在推断中。"""


def start(name: str, sample_path: str) -> int:
    """建占位记录并在后台开跑，返回模版 id。"""
    with SessionLocal() as s:
        tpl = s.scalar(select(M.Template).where(M.Template.name == name))
        # 同名的还在跑就别覆盖：占位记录一改，前一个线程跑完会把结果写到
        # 新样例的名下，两份样例的结论就混在一起了
        if tpl and job_of(tpl).get("status") == RUNNING:
            raise Busy(name)
        if not tpl:
            tpl = M.Template(name=name, kind="custom")
            s.add(tpl)
        tpl.spec = {"_job": {"status": RUNNING, "detail": "排队中",
                             "at": datetime.now().isoformat(timespec="seconds")}}
        s.commit()
        tid = tpl.id

    t = threading.Thread(target=_run, args=(tid, name, sample_path),
                         name=f"tpl-infer-{tid}", daemon=True)
    t.start()
    return tid


def _run(tpl_id, name, sample_path):
    from . import templates as TPL
    try:
        with SessionLocal() as s:
            tpl = s.get(M.Template, tpl_id)
            if tpl:
                _set_job(s, tpl, RUNNING, "正在让模型分析表头…")
        with SessionLocal() as s:
            tpl, rep = TPL.register(s, sample_path, name)
            detail = ("校验通过，可设为默认" if rep["usable"]
                      else f"校验发现 {rep['total_diffs']} 处问题，请核对后再启用")
            _set_job(s, tpl, DONE, detail)
        log.info("模版「%s」推断完成：%s", name, detail)
    except Exception as e:                       # noqa: BLE001
        log.exception("模版推断失败 %s", name)
        with SessionLocal() as s:
            tpl = s.get(M.Template, tpl_id)
            if tpl:
                _set_job(s, tpl, FAILED, f"{type(e).__name__}: {e}")
    finally:
        # 样例是临时文件，推断线程用完才能删——同步版本在 finally 里删，
        # 异步后如果照搬，线程还没读就被删了
        Path(sample_path).unlink(missing_ok=True)
