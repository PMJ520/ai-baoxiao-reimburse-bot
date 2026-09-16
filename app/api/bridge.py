# -*- coding: utf-8 -*-
"""宿主机 worker 接口。

协议刻意不用 JSON：任务正文就是 body，元信息走 header，结果原样 POST 回来。
理由是 worker 要在各种环境里跑（bash、busybox sh、PowerShell），
一旦要求解析 JSON 就得依赖 jq 之类的东西，那就违背了"零依赖"的初衷。

    GET  /bridge/jobs/next?wait=30&os=&arch=&cli=&ver=
         200 → header X-Job-Id / X-Job-Model / X-Job-Max-Tokens，body 是提示词全文
         204 → 这段时间没活
    POST /bridge/jobs/{id}/result          body 是 CLI 的原样输出
    POST /bridge/jobs/{id}/result?error=1  body 是错误信息
"""
import hmac

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..bridge.queue import QUEUE
from ..db.base import SessionLocal

router = APIRouter(prefix="/bridge", tags=["bridge"])

MAX_WAIT = 60.0
MAX_RESULT = 1 << 20      # 1MB，正常回复远小于此，超了多半是 CLI 吐了异常日志


def _token():
    """桥接令牌复用 LLM 配置里的 key 字段（设置页上标着"桥接令牌"）。"""
    from ..services import settings_store as ST
    with SessionLocal() as s:
        return (ST.llm_config(s).get("api_key") or "").strip()


def _auth(request: Request):
    want = _token()
    if not want:
        raise HTTPException(503, "尚未设置桥接令牌，请先在后台「系统设置 → 对话模型」里配置")
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got, want):
        raise HTTPException(401, "桥接令牌不正确")


@router.get("/jobs/next")
def next_job(request: Request, wait: float = Query(30.0), os: str = "",
             arch: str = "", cli: str = "", ver: str = ""):
    _auth(request)
    env = {"os": os[:40], "arch": arch[:20], "cli": cli[:40], "ver": ver[:80]}
    job = QUEUE.take(min(max(wait, 0.0), MAX_WAIT), env)
    if not job:
        return Response(status_code=204)
    return Response(
        content=job.prompt.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"X-Job-Id": job.id, "X-Job-Model": job.model or "",
                 "X-Job-Max-Tokens": str(job.max_tokens)},
    )


@router.post("/jobs/{job_id}/result")
async def job_result(job_id: str, request: Request, error: int = 0):
    _auth(request)
    raw = await request.body()
    if len(raw) > MAX_RESULT:
        raw = raw[:MAX_RESULT]
    body = raw.decode("utf-8", "replace").strip()
    if error:
        ok = QUEUE.finish(job_id, error=body or "worker 未说明原因")
    else:
        ok = QUEUE.finish(job_id, text=body)
    # 任务不在了通常是调用方已超时撤单，告诉 worker 一声，不算错误
    return {"accepted": ok}


@router.get("/status")
def status(request: Request):
    """worker 在线状态。后台页面用会话鉴权，worker 自己用令牌鉴权。"""
    from ..admin import auth as admin_auth
    if not admin_auth.current_user(request):
        _auth(request)
    return QUEUE.state()
