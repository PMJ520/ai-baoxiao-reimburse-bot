# -*- coding: utf-8 -*-
"""配置读写：库优先，环境变量兜底。

密码一律存哈希，不存明文——后台能改密码就意味着它会经过数据库，
明文落库等于给自己埋雷。
"""
import hashlib
import hmac
import logging
import os
import secrets

from sqlalchemy import select

from ..config import settings
from ..db import models as M

log = logging.getLogger(__name__)

K_ADMIN_PASSWORD = "admin_password_hash"
K_FEISHU_ID = "feishu_app_id"
K_FEISHU_SECRET = "feishu_app_secret"
K_DINGTALK_ID = "dingtalk_client_id"
K_DINGTALK_SECRET = "dingtalk_client_secret"
K_LLM_PROVIDER = "llm_provider"
K_LLM_KEY = "llm_api_key"
K_LLM_BASE = "llm_base_url"
K_LLM_MODEL = "llm_model"

_ITER = 120_000


def hash_password(pw: str, salt: str | None = None):
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITER)
    return f"{salt}${dk.hex()}"


def verify_password(pw: str, stored: str):
    if not stored or "$" not in stored:
        return False
    salt, want = stored.split("$", 1)
    got = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), _ITER)
    return hmac.compare_digest(got.hex(), want)


def get(session, key, default=None):
    row = session.get(M.Setting, key)
    return row.value if row and row.value is not None else default


def put(session, key, value):
    row = session.get(M.Setting, key)
    if row:
        row.value = value
    else:
        session.add(M.Setting(key=key, value=value))
    session.commit()


def get_many(session, keys):
    rows = session.scalars(select(M.Setting).where(M.Setting.key.in_(keys)))
    return {r.key: r.value for r in rows}


def check_admin_password(session, pw):
    """库里有哈希就用哈希校验；没有则回退到环境变量里的明文（首次部署）。"""
    stored = get(session, K_ADMIN_PASSWORD)
    if stored:
        return verify_password(pw, stored)
    env_pw = settings.admin_password
    return bool(env_pw) and hmac.compare_digest(pw or "", env_pw)


def set_admin_password(session, pw):
    put(session, K_ADMIN_PASSWORD, hash_password(pw))


def feishu_config(session):
    d = get_many(session, [K_FEISHU_ID, K_FEISHU_SECRET])
    return (d.get(K_FEISHU_ID) or settings.feishu.app_id,
            d.get(K_FEISHU_SECRET) or settings.feishu.app_secret)


def dingtalk_config(session):
    d = get_many(session, [K_DINGTALK_ID, K_DINGTALK_SECRET])
    return (d.get(K_DINGTALK_ID) or settings.dingtalk.client_id,
            d.get(K_DINGTALK_SECRET) or settings.dingtalk.client_secret)


def llm_config(session):
    d = get_many(session, [K_LLM_PROVIDER, K_LLM_KEY, K_LLM_BASE, K_LLM_MODEL])
    return {
        "provider": d.get(K_LLM_PROVIDER) or settings.llm.provider,
        "api_key": d.get(K_LLM_KEY) or settings.llm.api_key,
        "base_url": d.get(K_LLM_BASE) or settings.llm.base_url,
        "model": d.get(K_LLM_MODEL) or settings.llm.model,
    }


def masked(v, keep=4):
    """密钥只回显尾部，避免在页面上完整暴露。"""
    if not v:
        return ""
    return "•" * 8 + v[-keep:] if len(v) > keep else "•" * len(v)
