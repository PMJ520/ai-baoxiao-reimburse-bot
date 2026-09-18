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


def _new_loop():
    """给通道线程一个干净的事件循环。

    必须用默认策略新建：uvicorn 可能装了 uvloop，而两家 SDK 的长连接都不
    保证能在 uvloop 上正常工作。
    """
    loop = asyncio.DefaultEventLoopPolicy().new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def _patch_lark_loop(loop):
    """飞书 SDK 专用补丁，别套到别的通道上。

    lark SDK 在 ws/client.py 里把事件循环存成**模块级全局**，import 时即绑定。
    由于 import 发生在主线程，它抓到的是 uvicorn 正在运行的循环，于是
    start() 调 run_until_complete 必然报 "event loop is already running"。
    仅靠 set_event_loop 无效——必须把那个模块全局替换掉。
    """
    try:
        from lark_oapi.ws import client as _ws_client
        _ws_client.loop = loop
    except Exception:
        log.warning("未能替换飞书 SDK 的事件循环，长连接可能无法建立", exc_info=True)


def _run(channel, on_message, prepare=None):
    """运行长连接。

    不要在外层再套重连——SDK 自身已带自动重连，外层重连会同时存在多个
    客户端；而平台对同一应用只允许一条长连接，多客户端会互相踢下线，
    表现为无限重连。这里只负责起一次，并记录最终状态。
    """
    loop = _new_loop()
    if prepare:
        prepare(loop)
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

    t = threading.Thread(target=_run, args=(channel, on_message, _patch_lark_loop),
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


def start_dingtalk(client_id=None, client_secret=None):
    """启动钉钉通道。凭据优先取库里的（可在后台改），其次环境变量。

    未配置时安静跳过——两个通道各自独立，飞书能用不受钉钉影响，反之亦然。
    """
    if client_id is None or client_secret is None:
        from ..db.base import SessionLocal
        from ..services import settings_store as ST
        with SessionLocal() as s:
            client_id, client_secret = ST.dingtalk_config(s)
    if not (client_id and client_secret):
        _status["dingtalk"] = "disabled"
        log.info("未配置钉钉凭据，该通道未启用")
        return None
    if _threads.get("dingtalk") and _threads["dingtalk"].is_alive():
        return _threads["dingtalk"]

    from ..conversation.handlers import handle
    from .dingtalk.channel import DingTalkChannel

    channel = DingTalkChannel(client_id, client_secret)

    def on_message(msg):
        handle(channel, msg)

    t = threading.Thread(target=_run, args=(channel, on_message),
                         name="dingtalk-ws", daemon=True)
    t.start()
    _threads["dingtalk"] = t
    return t


def restart_dingtalk(client_id, client_secret):
    """凭据变更后重连。旧线程不强杀，理由同飞书：新连接会顶掉旧的。"""
    if not (client_id and client_secret):
        _status["dingtalk"] = "disabled"
        return "已清除钉钉凭据，该通道已停用"
    old = _threads.pop("dingtalk", None)
    if old and old.is_alive():
        log.info("旧钉钉连接将由新连接顶替")
    t = start_dingtalk(client_id=client_id, client_secret=client_secret)
    return "凭据已保存，正在重新连接钉钉" if t else "凭据已保存，但连接未能启动"


def start_all():
    """启动所有已配置的通道。没配的安静跳过。"""
    start_feishu()
    start_dingtalk()
