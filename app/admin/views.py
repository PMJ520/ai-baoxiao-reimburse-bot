# -*- coding: utf-8 -*-
"""数据后台页面。

服务端渲染，不上前端框架——内部工具，信息密度和可维护性比交互花哨重要。
"""
import json
import os
import platform
import tempfile
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..db import models as M
from ..db.base import SessionLocal
from ..services import templates as TPL
from ..services.enrich import CATEGORIES
from . import auth

router = APIRouter(prefix="/admin", tags=["admin"])
tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

KINDS = [(M.KIND_PAYMENT, "支付截图"), (M.KIND_INVOICE, "发票"),
         (M.KIND_ITINERARY, "行程单"), (M.KIND_UNKNOWN, "未知")]
STATUSES = [(M.ST_PARSED, "已解析"), (M.ST_NEEDS_REVIEW, "待复核"),
            (M.ST_USED, "已用于报销"), (M.ST_EXCLUDED, "已排除"),
            (M.ST_RECEIVED, "已收到")]
_KIND = dict(KINDS)
_STATUS = dict(STATUSES)
_TAGCLS = {M.ST_PARSED: "ok", M.ST_NEEDS_REVIEW: "warn",
           M.ST_USED: "mute", M.ST_EXCLUDED: "bad", M.ST_RECEIVED: "mute"}
_BATCH = {M.B_DRAFT: ("mute", "草稿"), M.B_CONFIRMING: ("warn", "确认中"),
          M.B_READY: ("warn", "待出表"), M.B_DONE: ("ok", "已完成")}


def _tag(cls, text):
    return f'<span class="tag {cls}">{text}</span>'


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def preview_kind(name):
    """能不能在页面里预览：image / pdf / 空（只能下载）。"""
    ext = Path(name or "").suffix.lower()
    return "image" if ext in IMG_EXT else ("pdf" if ext == ".pdf" else "")


tpl.env.globals.update(
    preview_kind=preview_kind,
    kind_name=lambda k: _KIND.get(k, k),
    status_tag=lambda s: _tag(_TAGCLS.get(s, "mute"), _STATUS.get(s, s)),
    batch_tag=lambda s: _tag(*_BATCH.get(s, ("mute", s))),
    voucher_tag=lambda v: _tag({"发票": "ok", "发票+替票": "warn",
                                "替票": "bad"}.get(v, "mute"), v or "—"),
)


def _page(name, request, **ctx):
    ctx.setdefault("me", auth.current_user(request))
    return tpl.TemplateResponse(request, name, ctx)


def _guard(request):
    """页面级鉴权：未登录直接跳登录页，不抛异常。"""
    return auth.current_user(request)


# ---------- 登录 ----------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _page("login.html", request)


@router.post("/login")
def do_login(request: Request, user: str = Form(""), password: str = Form("")):
    if not auth.check_login(user, password):
        return _page("login.html", request, error="用户名或密码不正确")
    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie(auth.COOKIE, auth.make_token(user), max_age=auth.MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------- 概览 ----------
@router.get("", response_class=HTMLResponse)
def home(request: Request):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        def cnt(*conds):
            q = select(func.count(M.Document.id))
            for c in conds:
                q = q.where(c)
            return s.scalar(q) or 0
        unused_q = select(M.Document).where(
            M.Document.kind == M.KIND_PAYMENT,
            M.Document.status.notin_([M.ST_USED, M.ST_EXCLUDED]))
        unused = list(s.scalars(unused_q))
        stats = {
            "docs": cnt(), "payments": cnt(M.Document.kind == M.KIND_PAYMENT),
            "invoices": cnt(M.Document.kind == M.KIND_INVOICE),
            "pending": cnt(M.Document.status == M.ST_NEEDS_REVIEW),
            "unused": len(unused),
            "unused_amount": float(sum(d.amount or 0 for d in unused)),
            "batches": s.scalar(select(func.count(M.Batch.id))) or 0,
        }
        recent = list(s.scalars(select(M.Document)
                                .order_by(M.Document.id.desc()).limit(12)))
    return _page("home.html", request, tab="home", s=stats, recent=recent)


# ---------- 文件 ----------
@router.get("/documents", response_class=HTMLResponse)
def documents(request: Request, kind: str = "", status: str = "", q: str = ""):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        stmt = select(M.Document)
        if kind:
            stmt = stmt.where(M.Document.kind == kind)
        if status:
            stmt = stmt.where(M.Document.status == status)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(M.Document.merchant.like(like)
                              | M.Document.filename.like(like))
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        docs = list(s.scalars(stmt.order_by(
            M.Document.occurred_at.desc().nullslast()).limit(300)))
    return _page("documents.html", request, tab="docs", docs=docs, total=total,
                 kinds=KINDS, statuses=STATUSES,
                 f={"kind": kind, "status": status, "q": q})


# 能内联显示的类型。其余一律当附件下载，避免把 html/svg 之类当页面渲染，
# 那等于在本站域下执行别人上传的内容
INLINE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".pdf": "application/pdf",
}


@router.get("/documents/{doc_id}/raw")
def document_raw(request: Request, doc_id: int, download: int = 0):
    """返回原件本身。看解析对不对，终究要能看到原图。"""
    from fastapi.responses import FileResponse
    from .. import storage

    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        d = s.get(M.Document, doc_id)
        if not d or not d.rel_path:
            return PlainTextResponse("文件不存在", status_code=404)
        rel, name = d.rel_path, d.filename or "file"

    path = storage.abs_path(rel).resolve()
    # rel_path 来自数据库不是用户输入，但仍要确认它落在 blob 目录内——
    # 将来任何一处写入被污染的路径，这里就是最后一道闸
    root = storage.settings.blob_dir.resolve()
    if root not in path.parents or not path.is_file():
        return PlainTextResponse("文件不存在", status_code=404)

    ext = path.suffix.lower()
    ctype = INLINE_TYPES.get(ext)
    disp = "attachment" if (download or not ctype) else "inline"
    return FileResponse(
        path, media_type=ctype or "application/octet-stream",
        headers={"Content-Disposition":
                 f"{disp}; filename*=UTF-8''{quote(name)}"})


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_edit(request: Request, doc_id: int, saved: int = 0):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        d = s.get(M.Document, doc_id)
        if not d:
            return RedirectResponse("/admin/documents", status_code=302)
        pj = json.dumps(d.parsed, ensure_ascii=False, indent=2) if d.parsed else ""
        preview = preview_kind(d.rel_path)
    return _page("document_edit.html", request, tab="docs", d=d, kinds=KINDS,
                 parsed_json=pj, saved=saved, preview=preview)


@router.post("/documents/{doc_id}")
def document_save(request: Request, doc_id: int, occurred_at: str = Form(""),
                  amount: str = Form(""), merchant: str = Form(""),
                  kind: str = Form("")):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        d = s.get(M.Document, doc_id)
        if d:
            d.occurred_at = _parse_dt(occurred_at)
            d.amount = float(amount) if amount.strip() else None
            d.merchant = merchant.strip() or None
            if kind:
                d.kind = kind
            # 人工补齐后自动解除待复核——否则用户改完还得再点一次
            if d.status == M.ST_NEEDS_REVIEW and d.amount is not None \
                    and d.occurred_at is not None:
                d.status = M.ST_PARSED
                d.parse_error = None
            s.commit()
    return RedirectResponse(f"/admin/documents/{doc_id}?saved=1", status_code=302)


def _parse_dt(v):
    v = (v or "").strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, f)
        except ValueError:
            continue
    return None


# ---------- 批次 ----------
@router.get("/batches", response_class=HTMLResponse)
def batches(request: Request):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        rows = []
        for b in s.scalars(select(M.Batch).order_by(M.Batch.id.desc())):
            rows.append({
                "id": b.id, "title": b.title, "status": b.status,
                "requester": b.requester, "company": b.company,
                "ledger_path": b.ledger_path, "n": len(b.items),
                "total": float(sum(i.amount or 0 for i in b.items)),
            })
    return _page("batches.html", request, tab="batches", batches=rows)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int, saved: int = 0):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        b = s.get(M.Batch, batch_id)
        if not b:
            return RedirectResponse("/admin/batches", status_code=302)
        items = list(b.items)
        total = float(sum(i.amount or 0 for i in items))
        inv_total = float(sum(i.invoice_amount or 0 for i in items))
    return _page("batch_detail.html", request, tab="batches", b=b, items=items,
                 total=total, inv_total=inv_total, categories=CATEGORIES,
                 saved=saved)


@router.post("/batches/{batch_id}/people")
async def batch_people(request: Request, batch_id: int):
    if not _guard(request):
        return auth.redirect_login()
    form = await request.form()
    with SessionLocal() as s:
        b = s.get(M.Batch, batch_id)
        if b:
            b.requester = (form.get("requester") or "").strip() or None
            b.handler = (form.get("handler") or "").strip() or None
            b.company = (form.get("company") or "").strip() or None
            s.commit()
    return RedirectResponse(f"/admin/batches/{batch_id}?saved=1", status_code=302)


@router.post("/batches/{batch_id}/items")
async def batch_items(request: Request, batch_id: int):
    if not _guard(request):
        return auth.redirect_login()
    form = await request.form()
    with SessionLocal() as s:
        b = s.get(M.Batch, batch_id)
        if b:
            for it in b.items:
                d = form.get(f"detail_{it.seq}")
                c = form.get(f"category_{it.seq}")
                if d is not None:
                    it.detail = d.strip() or None
                if c is not None:
                    it.category = c.strip() or None
            filled = sum(1 for it in b.items if it.detail and it.category)
            if b.status in (M.B_DRAFT, M.B_CONFIRMING):
                b.status = M.B_READY if filled == len(b.items) else M.B_CONFIRMING
            s.commit()
    return RedirectResponse(f"/admin/batches/{batch_id}?saved=1", status_code=302)


# ---------- 模版 ----------
@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, msg: str = "", ok: int = 0):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        tpls = TPL.listing(s)
    return _page("templates_page.html", request, tab="tpl", tpls=tpls,
                 msg=msg, ok=bool(ok))


@router.get("/templates/status")
def templates_status(request: Request):
    """只返回状态，供页面轮询。

    整页刷新会把用户正在填的表单连同选好的文件一起清掉——推断要跑几分钟，
    刷新必然撞上有人正在上传下一份。
    """
    if not _guard(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    with SessionLocal() as s:
        return JSONResponse({"items": TPL.listing(s)})


@router.post("/templates")
async def template_register(request: Request, name: str = Form(...),
                            file: UploadFile = File(...)):
    """只负责收文件和排队，推断本身丢给后台线程。

    推断要调模型读表头，CLI 方式跑几分钟很常见。挂在 HTTP 请求上等，
    浏览器或反向代理任何一环超时都会失败，页面还卡着干不了别的。
    """
    if not _guard(request):
        return auth.redirect_login()
    from ..services import template_jobs as TJ

    data = await file.read()
    suffix = Path(file.filename or "x.xlsx").suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name          # 临时文件由推断线程用完后删，这里不能删
    try:
        TJ.start(name, path)
    except TJ.Busy:
        Path(path).unlink(missing_ok=True)
        return RedirectResponse(
            f"/admin/templates?msg=「{name}」正在推断中，请等它结束，"
            f"或换一个名称再传&ok=0", status_code=302)
    except Exception as e:      # noqa: BLE001
        Path(path).unlink(missing_ok=True)
        return RedirectResponse(f"/admin/templates?msg=排队失败：{e}&ok=0",
                                status_code=302)
    return RedirectResponse(
        f"/admin/templates?msg=「{name}」已开始推断，完成后这页会自动更新&ok=1",
        status_code=302)


@router.get("/templates/{template_id}", response_class=HTMLResponse)
def template_detail(request: Request, template_id: int):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        t = s.get(M.Template, template_id)
        if not t:
            return RedirectResponse("/admin/templates", status_code=302)
        spec = t.spec or {}
        cols = [_obj(c) for c in spec.get("columns", [])]
        rep = spec.get("_report")
        name, is_default = t.name, t.is_default
    return _page("template_detail.html", request, tab="tpl",
                 t={"name": name, "is_default": is_default}, cols=cols, rep=rep)


def _obj(d):
    class C:
        pass
    o = C()
    for k in ("letter", "header", "source", "const", "template", "when"):
        setattr(o, k, d.get(k))
    return o


@router.get("/templates/{template_id}/activate")
def template_activate(request: Request, template_id: int):
    if not _guard(request):
        return auth.redirect_login()
    with SessionLocal() as s:
        TPL.confirm(s, template_id, make_default=True)
    return RedirectResponse("/admin/templates?msg=已设为默认模版&ok=1",
                            status_code=302)


# ---------- 系统设置 ----------
# OpenAI 兼容端点覆盖面最广（通义、DeepSeek、智谱、Moonshot 等），放首位作默认
# 开源仓库地址，安装命令里要用；部署时可用环境变量覆盖成自己的 fork
REPO = os.environ.get("EH_REPO", "PMJ520/ai-baoxiao-reimburse-bot")
# CNB 的 raw 路径是 /-/git/raw/，不是 /-/raw/——后者返回的是网页外壳，
# 拿到的是一整页 HTML 而不是脚本，且 HTTP 状态是 200，很难察觉
CNB_RAW = os.environ.get(
    "EH_CNB_RAW",
    "https://cnb.cool/hy-team/mj-public/ai-baoxiao-reimburse-bot/-/git/raw/main")


def host_hint():
    """从容器内侧猜宿主机是什么系统。

    容器看不到宿主机，但内核标识会漏底：Docker Desktop 跑在 LinuxKit 虚拟机
    里（只可能是 Mac 或 Windows），WSL2 后端则带 microsoft 字样，两者都没有
    就是原生 Linux Engine。Mac 和 Windows 分不开时交给浏览器 UA 定音——
    页面上四个系统随时能切，猜错了使用者一眼就能发现。
    """
    try:
        ver = Path("/proc/version").read_text().lower()
    except OSError:
        ver = ""
    arch = platform.machine().lower()
    if "microsoft" in ver or "wsl" in ver:
        return {"guess": "windows", "why": "内核标识为 WSL，宿主机是 Windows"}
    if "linuxkit" in ver:
        if arch in ("aarch64", "arm64"):
            return {"guess": "macos", "why": "Docker Desktop + ARM，多为 Apple 芯片 Mac"}
        return {"guess": "desktop", "why": "Docker Desktop，宿主机是 Mac 或 Windows"}
    if ver:
        return {"guess": "linux", "why": "原生 Docker Engine，宿主机是 Linux"}
    return {"guess": "", "why": ""}


PROVIDERS = [("openai", "OpenAI 兼容（通义/DeepSeek/智谱…）"), ("claude", "Claude"),
             ("cli", "本机 CLI（经宿主机桥接）")]

# 配置状态三态：未配置=红、已配置且正常=绿、已配置但异常=黄。
# 区分"没配"和"配了但连不上"，前者是待办，后者是故障，处理方式完全不同。
IM_STATE = {
    "running": ("ok", "已连接", ""),
    "connecting": ("warn", "连接中", "正在建立长连接"),
    "reconnecting": ("warn", "重连中", "连接中断，正在重试"),
    "stopped": ("warn", "已断开", "连接已退出，可重新保存以重连"),
}


def _im_state(raw, configured):
    """把 runner 的原始状态翻译成三态展示。"""
    if not configured:
        return {"cls": "bad", "label": "未配置", "why": "填入凭据后即可接收消息"}
    if raw in IM_STATE:
        cls, label, why = IM_STATE[raw]
        return {"cls": cls, "label": label, "why": why}
    if raw and raw.startswith("error"):
        return {"cls": "warn", "label": "连接异常", "why": raw.split(":", 1)[-1].strip()}
    return {"cls": "warn", "label": raw or "未知", "why": ""}


def _llm_state(session, cfg):
    """模型状态。连通性以最近一次测试结果为准——每次进页面都实测太慢也太费。"""
    from ..services import settings_store as ST
    is_cli = cfg["provider"] in ("cli", "cli_bridge")
    # CLI 方式不需要 API Key，配了桥接令牌就算配置过（worker 靠它鉴权）
    configured = bool(cfg["api_key"])
    if not configured:
        return {"cls": "bad", "label": "未配置",
                "why": "明细提议功能不可用"}
    last = ST.get(session, "llm_last_check")
    if is_cli:
        # 两个信号都要看：worker 在不在线是实时的，但它在线不等于跑得通——
        # CLI 登录过期时 worker 照样在线，只有上次测试结果能反映这件事
        from ..bridge.queue import QUEUE
        st = QUEUE.state()
        if not st["online"]:
            return {"cls": "warn", "label": "已配置但异常",
                    "why": "宿主机 worker 未连接"}
        if last and last.startswith("fail"):
            return {"cls": "warn", "label": "已配置但异常",
                    "why": last.split(":", 1)[-1].strip()[:60]}
        env = st["env"]
        who = " ".join(x for x in (env.get("os"), env.get("cli")) if x)
        tail = f"worker 在线（{who}）" if who else "worker 在线"
        return {"cls": "ok", "label": "已配置",
                "why": tail if last == "ok" else tail + "，尚未验证连通性"}
    if last == "ok":
        return {"cls": "ok", "label": "已配置", "why": "上次测试连通正常"}
    if last and last.startswith("fail"):
        return {"cls": "warn", "label": "已配置但异常",
                "why": last.split(":", 1)[-1].strip()[:60]}
    return {"cls": "warn", "label": "已配置", "why": "尚未验证连通性"}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", ok: int = 0):
    if not _guard(request):
        return auth.redirect_login()
    from ..im import runner as im_runner
    from ..services import settings_store as ST

    with SessionLocal() as s:
        fid, fsec = ST.feishu_config(s)
        did, dsec = ST.dingtalk_config(s)
        llm = ST.llm_config(s)
    st = im_runner.status()
    raw = st.get("feishu", "disabled")
    with SessionLocal() as s:
        llm_state = _llm_state(s, llm)
    from ..bridge.queue import QUEUE
    return _page("settings.html", request, tab="set",
                 feishu_id=fid, feishu_secret_mask=ST.masked(fsec),
                 im=_im_state(raw, bool(fid and fsec)),
                 dingtalk_id=did, dingtalk_secret_mask=ST.masked(dsec),
                 dt=_im_state(st.get("dingtalk", "disabled"), bool(did and dsec)),
                 llm=llm, llm_state=llm_state,
                 llm_key_mask=ST.masked(llm["api_key"]),
                 providers=PROVIDERS, msg=msg, ok=bool(ok),
                 host_hint=host_hint(), worker=QUEUE.state(),
                 repo=REPO, cnb_raw=CNB_RAW,
                 # 安装命令要能直接复制，令牌必须原样给出。页面本就在登录态
                 # 之后，且这个令牌只在本后台与 worker 之间有意义
                 llm_token=(llm["api_key"] if llm["provider"] in ("cli", "cli_bridge") else ""))


@router.post("/settings/feishu")
def settings_feishu(request: Request, app_id: str = Form(""),
                    app_secret: str = Form("")):
    if not _guard(request):
        return auth.redirect_login()
    from ..im import runner as im_runner
    from ..services import settings_store as ST

    with SessionLocal() as s:
        ST.put(s, ST.K_FEISHU_ID, app_id.strip())
        if app_secret.strip():          # 留空表示不修改，避免误清
            ST.put(s, ST.K_FEISHU_SECRET, app_secret.strip())
        fid, fsec = ST.feishu_config(s)
    msg = im_runner.restart_feishu(fid, fsec)
    return RedirectResponse(f"/admin/settings?msg={msg}&ok=1", status_code=302)


@router.post("/settings/dingtalk")
def settings_dingtalk(request: Request, client_id: str = Form(""),
                      client_secret: str = Form("")):
    if not _guard(request):
        return auth.redirect_login()
    from ..im import runner as im_runner
    from ..services import settings_store as ST

    with SessionLocal() as s:
        ST.put(s, ST.K_DINGTALK_ID, client_id.strip())
        if client_secret.strip():       # 留空表示不修改，避免误清
            ST.put(s, ST.K_DINGTALK_SECRET, client_secret.strip())
        cid, csec = ST.dingtalk_config(s)
    msg = im_runner.restart_dingtalk(cid, csec)
    return RedirectResponse(f"/admin/settings?msg={msg}&ok=1", status_code=302)


@router.post("/settings/llm")
def settings_llm(request: Request, provider: str = Form("claude"),
                 api_key: str = Form(""), base_url: str = Form(""),
                 model: str = Form(""), fmt: str = ""):
    if not _guard(request):
        return auth.redirect_login()
    from ..services import settings_store as ST
    with SessionLocal() as s:
        ST.put(s, ST.K_LLM_PROVIDER, provider)
        ST.put(s, ST.K_LLM_BASE, base_url.strip())
        ST.put(s, ST.K_LLM_MODEL, model.strip())
        if api_key.strip():
            ST.put(s, ST.K_LLM_KEY, api_key.strip())
        ST.put(s, "llm_last_check", "")   # 配置变了，旧的连通结论作废
    # fmt=json 时不跳转：整页刷新会把弹窗关掉，用户填完想测一下还得
    # 重新点开卡片，来回一趟毫无必要
    if fmt == "json":
        return JSONResponse({"ok": True, "msg": "已保存，可以点「测试连通性」了"})
    return RedirectResponse("/admin/settings?msg=已保存模型配置，建议点一次「测试连通性」&ok=1",
                            status_code=302)


@router.get("/settings/llm/test")
def settings_llm_test(request: Request, fmt: str = ""):
    """测试连通性。

    fmt=json 时返回结果而不跳转——设置页在弹窗里调它，跳转会把弹窗关掉，
    用户得重新点开才能继续改。
    """
    if not _guard(request):
        return auth.redirect_login()
    from ..services.llm import get_client
    from ..services import settings_store as ST
    try:
        # 不传配置，走和飞书对话里完全一样的取客户端路径——测试若和实际
        # 用的不是同一条路，就会出现"测试通过、真用报错"这种自相矛盾
        cli = get_client()
        out = cli.complete("只回复两个字：正常", "测试", max_tokens=32)
        msg, ok, mark = f"连通正常，模型回复：{(out or '').strip()[:30]}", 1, "ok"
    except Exception as e:
        detail = str(e)[:120]
        msg, ok, mark = f"连接失败：{detail}", 0, f"fail:{detail}"
    with SessionLocal() as s:
        ST.put(s, "llm_last_check", mark)
    if fmt == "json":
        return JSONResponse({"ok": bool(ok), "msg": msg})
    return RedirectResponse(f"/admin/settings?msg={msg}&ok={ok}", status_code=302)


@router.post("/settings/password")
def settings_password(request: Request, old: str = Form(""),
                      new1: str = Form(""), new2: str = Form("")):
    if not _guard(request):
        return auth.redirect_login()
    from ..services import settings_store as ST

    if new1 != new2:
        return RedirectResponse("/admin/settings?msg=两次输入的新密码不一致&ok=0",
                                status_code=302)
    if len(new1) < 8:
        return RedirectResponse("/admin/settings?msg=新密码至少 8 位&ok=0",
                                status_code=302)
    with SessionLocal() as s:
        if not ST.check_admin_password(s, old):
            return RedirectResponse("/admin/settings?msg=当前密码不正确&ok=0",
                                    status_code=302)
        ST.set_admin_password(s, new1)
    # 密码变了，旧会话应失效——重新登录
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie(auth.COOKIE)
    return resp
