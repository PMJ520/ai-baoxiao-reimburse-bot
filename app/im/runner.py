# -*- coding: utf-8 -*-
"""IM 通道的后台运行器。

长连接是阻塞的，必须放在独立线程里，否则会挡住 API 服务。
线程内不复用请求期的数据库会话——handlers 每次自行开启会话。
"""
import asyncio
import logging
import threading

log = logging.getLogger(__name__)

_threads: dict[str, threading.Thread] = {}
_status: dict[str, str] = {}


def status():
    return dict(_status)


def _run(channel, on_message):
    """运行长连接。

    不要在外层再套重连——SDK 自身已带自动重连，外层重连会同时存在多个
    客户端；而飞书对同一 App 只允许一条长连接，多客户端会互相踢下线，
    表现为无限重连。这里只负责起一次，并记录最终状态。
    """
    # lark SDK 在 ws/client.py 里把事件循环存成**模块级全局**，import 时即绑定。
    # 由于 import 发生在主线程，它抓到的是 uvicorn 正在运行的循环，
    # 于是 start() 调 run_until_complete 必然报 "event loop is already running"。
    # 仅靠 set_event_loop 无效——必须把那个模块全局替换掉。
    loop = asyncio.DefaultEventLoopPolicy().new_event_loop()   # 避开 uvloop
    asyncio.set_event_loop(loop)
    try:
        from lark_oapi.ws import client as _ws_client
        _ws_client.loop = loop
    except Exception:
        log.warning("未能替换 SDK 的事件循环，长连接可能无法建立", exc_info=True)
    _status[channel.name] = "running"
    try:
        channel.start(on_message)
        _status[channel.name] = "stopped"
        log.warning("%s 通道已退出", channel.name)
    except Exception as e:
        _status[channel.name] = f"error: {type(e).__name__}"
        log.exception("%s 通道异常退出", channel.name)


def start_feishu(settings=None, app_id=None, app_secret=None):
    """启动飞书通道。凭据优先取库里的（可在后台改），其次环境变量。

    未配置时安静跳过，不影响 API 可用。
    """
    if app_id is None or app_secret is None:
        from ..db.base import SessionLocal
        from ..services import settings_store as ST
        with SessionLocal() as s:
            app_id, app_secret = ST.feishu_config(s)
    if not (app_id and app_secret):
        _status["feishu"] = "disabled"
        log.warning("未配置飞书凭据，IM 通道未启用")
        return None
    if _threads.get("feishu") and _threads["feishu"].is_alive():
        return _threads["feishu"]

    from ..conversation.handlers import handle
    from .feishu.channel import FeishuChannel

    channel = FeishuChannel(app_id, app_secret)

    def on_message(msg):
        handle(channel, msg)

    t = threading.Thread(target=_run, args=(channel, on_message),
                         name="feishu-ws", daemon=True)
    t.start()
    _threads["feishu"] = t
    return t


def restart_feishu(app_id, app_secret):
    """凭据变更后重连。

    SDK 的长连接线程无法安全中断，故不强杀旧线程——飞书对同一 App 只允许
    一条连接，新连接建立后旧的会被服务端踢掉，这里借用该机制完成切换。
    """
    if not (app_id and app_secret):
        _status["feishu"] = "disabled"
        return "已清除飞书凭据，IM 通道已停用"
    old = _threads.pop("feishu", None)
    if old and old.is_alive():
        log.info("旧飞书连接将由新连接顶替")
    t = start_feishu(app_id=app_id, app_secret=app_secret)
    return "凭据已保存，正在重新连接飞书" if t else "凭据已保存，但连接未能启动"
