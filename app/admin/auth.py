# -*- coding: utf-8 -*-
"""后台登录。

单用户场景，不做完整 RBAC。会话用签名 Cookie：值是从密码派生的令牌，
服务端不存会话表，重启后仍有效；改密码则所有会话自动失效。
"""
import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..config import settings

COOKIE = "eh_session"
MAX_AGE = 7 * 24 * 3600


def _secret():
    # 用 API_TOKEN 做签名密钥；它本来就是随机生成且保密的
    return (settings.api_token or settings.admin_password or "insecure").encode()


def make_token(user: str):
    exp = int(time.time()) + MAX_AGE
    payload = f"{user}:{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def verify(token: str):
    if not token or token.count(":") != 2:
        return None
    user, exp, sig = token.split(":")
    payload = f"{user}:{exp}"
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    if int(exp) < time.time():
        return None
    return user


def check_login(user: str, password: str):
    """校验登录。密码以库里的哈希为准，未设置过则回退环境变量（首次部署）。"""
    from ..db.base import SessionLocal
    from ..services import settings_store as ST

    if not hmac.compare_digest(user or "", settings.admin_user):
        return False
    with SessionLocal() as s:
        return ST.check_admin_password(s, password)


def current_user(request: Request):
    return verify(request.cookies.get(COOKIE, ""))


def require_login(request: Request):
    """页面用：未登录跳登录页；接口用会抛 401。"""
    user = current_user(request)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录",
                            headers={"Location": "/admin/login"})
    return user


def redirect_login():
    return RedirectResponse("/admin/login", status_code=302)
