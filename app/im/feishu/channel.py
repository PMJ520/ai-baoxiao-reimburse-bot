# -*- coding: utf-8 -*-
"""飞书通道。

采用 WebSocket 长连接接收事件，而非 webhook 回调——它是**出站**连接，
内网部署无需公网入口、无需开防火墙入站、也不用配域名，这正是内网场景
选它的原因。
"""
import json
import logging
import mimetypes
import os

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                 CreateMessageReactionRequest,
                                 CreateMessageReactionRequestBody,
                                 CreateMessageRequest, CreateMessageRequestBody,
                                 DeleteMessageReactionRequest, Emoji,
                                 GetMessageResourceRequest)

from ..base import Attachment, InboundMessage

log = logging.getLogger(__name__)
PLATFORM = "feishu"


class FeishuChannel:
    name = PLATFORM

    def __init__(self, app_id: str, app_secret: str):
        if not (app_id and app_secret):
            raise ValueError("缺少飞书应用凭据")
        self.app_id, self.app_secret = app_id, app_secret
        self.client = lark.Client.builder() \
            .app_id(app_id).app_secret(app_secret).build()

    # ---------- 发送 ----------
    def send_text(self, chat_id, text):
        self._send(chat_id, "text", {"text": text})

    def send_post(self, chat_id, title, lines):
        """富文本。逐项确认这类长内容用它比纯文本可读。"""
        content = {"zh_cn": {"title": title,
                             "content": [[{"tag": "text", "text": l}] for l in lines]}}
        self._send(chat_id, "post", content)

    def _send(self, chat_id, msg_type, content):
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id).msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False)).build()).build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("飞书发送失败 %s %s", resp.code, resp.msg)
        return resp.success()

    def send_file(self, chat_id, path, filename=None):
        """上传并发送文件。台账 xlsx 就是这样回给用户的。"""
        filename = filename or os.path.basename(path)
        with open(path, "rb") as fh:
            up = self.client.im.v1.file.create(
                CreateFileRequest.builder().request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream").file_name(filename).file(fh).build()).build())
        if not up.success():
            log.error("飞书上传失败 %s %s", up.code, up.msg)
            return False
        return self._send(chat_id, "file", {"file_key": up.data.file_key})

    # ---------- 表情回应 ----------
    # 飞书没有"机器人把用户消息标记为已读"的接口，用表情回应代替：
    # 收到即打 👀 表示已看到，处理完换成 ✅ 或 ❌。既有即时反馈又不刷屏。
    def react(self, message_id, emoji_type):
        """给消息加表情，返回 reaction_id 供后续删除。失败不抛——它只是锦上添花。"""
        try:
            resp = self.client.im.v1.message_reaction.create(
                CreateMessageReactionRequest.builder()
                .message_id(message_id).request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()).build())
            if resp.success():
                return resp.data.reaction_id
            log.debug("加表情失败 %s %s", resp.code, resp.msg)
        except Exception:
            log.debug("加表情异常", exc_info=True)
        return None

    def unreact(self, message_id, reaction_id):
        if not reaction_id:
            return
        try:
            self.client.im.v1.message_reaction.delete(
                DeleteMessageReactionRequest.builder()
                .message_id(message_id).reaction_id(reaction_id).build())
        except Exception:
            log.debug("删表情异常", exc_info=True)

    # ---------- 接收 ----------
    def _download(self, message_id, file_key, kind):
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id).file_key(file_key) \
            .type("image" if kind == "image" else "file").build()
        resp = self.client.im.v1.message_resource.get(req)
        if not resp.success():
            raise RuntimeError(f"下载失败 {resp.code} {resp.msg}")
        return resp.file.read()

    def _to_inbound(self, data) -> InboundMessage:
        msg = data.event.message
        sender = getattr(data.event, "sender", None)
        user_id = None
        if sender and getattr(sender, "sender_id", None):
            user_id = sender.sender_id.open_id
        try:
            body = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
            body = {}

        text, atts = body.get("text", ""), []
        mid = msg.message_id

        def add_image(key, name=None):
            atts.append(Attachment(key, name or f"{key}.png", "image",
                                   lambda k=key: self._download(mid, k, "image")))

        def add_file(key, name=None):
            atts.append(Attachment(key, name or key, "file",
                                   lambda k=key: self._download(mid, k, "file")))

        mtype = msg.message_type
        if mtype == "image" and body.get("image_key"):
            add_image(body["image_key"])
        elif mtype == "file" and body.get("file_key"):
            add_file(body["file_key"], body.get("file_name"))
        elif mtype == "post":
            # 一次拖进来多张图时，飞书打包成一条富文本，图片藏在正文节点里。
            # 只取文字的话这条消息就变成了"没有附件的空话"，会被当成听不懂。
            text = _post_text(body)
            for key, name in _post_media(body):
                (add_image if name is None else add_file)(key, name)
        elif mtype not in ("text", "post"):
            # 视频、语音、合并转发这些取不出单据，不在这里硬下载——
            # 当成文件解析只会产出一个坏文档。类型透给上层，由它明确回绝。
            # 顺带记下原始结构，遇到没见过的形态才有据可查。
            log.warning("不作为单据处理的飞书消息类型 type=%s content=%s",
                        mtype, (msg.content or "")[:400])

        return InboundMessage(PLATFORM, msg.chat_id, user_id, mid, text, atts,
                              chat_type=getattr(msg, "chat_type", "p2p") or "p2p",
                              raw={"message_type": mtype})

    def start(self, on_message):
        """阻塞运行长连接。断线由 SDK 自行重连。"""
        def _handler(data):
            # 先记一条原始事件，便于区分"没收到事件"与"收到但处理失败"
            try:
                m = data.event.message
                log.info("收到飞书消息 type=%s chat=%s id=%s",
                         m.message_type, m.chat_id, m.message_id)
            except Exception:
                log.info("收到飞书事件（结构异常）")
            try:
                on_message(self._to_inbound(data))
            except Exception:
                log.exception("处理飞书消息失败")

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(_handler).build()
        log.info("飞书长连接启动中…")
        lark.ws.Client(self.app_id, self.app_secret,
                       event_handler=handler, log_level=lark.LogLevel.INFO).start()


def _post_text(body):
    """富文本消息取纯文字，忽略排版。"""
    out = []
    for para in (body.get("content") or []):
        for node in para:
            if node.get("tag") == "text":
                out.append(node.get("text", ""))
    return "".join(out)


def _post_media(body):
    """富文本里夹带的图片和文件。

    产出 (key, name)：name 为 None 表示图片，走图片下载接口；
    否则是文件，走文件下载接口——两者的下载参数不同，不能混。

    飞书对批量发送有两种截然不同的摆法，都要认：
      · 多张图片  → 落在 content 的 img 节点里
      · 多个文件  → 落在**顶层 files 数组**里，此时 content 是空的 [[]]
    只看 content 的话，一次拖进来的几个 PDF 会被整个丢掉。
    """
    for para in (body.get("content") or []):
        for node in para:
            tag = node.get("tag")
            if tag == "img" and node.get("image_key"):
                yield node["image_key"], None
            elif tag in ("file", "media") and node.get("file_key"):
                yield node["file_key"], node.get("file_name") or node["file_key"]

    for f in (body.get("files") or []):
        if f.get("is_folder") or not f.get("file_key"):
            continue            # 文件夹取不到内容，跳过
        yield f["file_key"], f.get("file_name") or f["file_key"]

    for im in (body.get("images") or []):
        if im.get("image_key"):
            yield im["image_key"], None


def guess_filename(att: Attachment):
    if "." in att.filename:
        return att.filename
    ext = mimetypes.guess_extension(
        "image/png" if att.kind == "image" else "application/octet-stream") or ""
    return att.filename + ext
