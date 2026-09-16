# -*- coding: utf-8 -*-
"""API 依赖：会话与鉴权。

服务可能部署在公网，而发票含公司信息，因此除健康检查外一律要求 token。
未配置 token 时直接拒绝启动式的放行——不给"默认无密码"留口子。
"""
import hmac

from fastapi import Depends, Header, HTTPException, status

from ..config import settings
from ..db.base import SessionLocal


def get_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def require_token(authorization: str = Header(default="")):
    expected = settings.api_token
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "服务未配置 API_TOKEN，拒绝提供接口访问")
    got = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not hmac.compare_digest(got, expected):      # 定时比较，避免侧信道
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "凭据无效")
    return True


Auth = Depends(require_token)
Db = Depends(get_session)
