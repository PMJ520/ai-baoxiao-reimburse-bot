# -*- coding: utf-8 -*-
"""钉钉通道（Stream 模式）。

出站长连接，不需要公网入口，内网部署照样能用——和飞书同样的形态。

三条踩过的坑，都是实测出来的，改代码前先读：

1. **多附件是拆成多条消息推的，一条一个。** 发 5 张图就是 5 条独立消息。
   这跟飞书正相反（飞书打包成一条富文本，附件藏在顶层 files 数组里），
   所以这里不需要"拆包"逻辑，反而要容忍同一批材料分多次到达。

2. **同一种东西在单聊和群聊里类型不同。** 单聊发图是 picture，
   群里发图是 richText。两个分支都得写，少一个就漏一半场景。

3. **图片没有文件名**，而入库是靠扩展名分类的（.pdf → 发票/行程单，
   图片扩展名 → 支付截图）。所以要按真实字节头判断 PNG 还是 JPEG——
   实测两种都会出现，固定写 .png 会张冠李戴。

另有一条平台限制：**群聊收不到文件**。群里机器人只能收到 @ 了它的消息，
而钉钉发文件时无法同时 @ 人。所以发票 PDF 只能走单聊。
"""
import hashlib
import json
import logging
import os

from ..base import Attachment, InboundMessage
from .api import Client, DingTalkError

log = logging.getLogger(__name__)

PLATFORM = "dingtalk"

# 图片字节头 → 扩展名。钉钉不给图片文件名，只能自己认。
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),           # RIFF....WEBP，前四字节足以区分
    (b"BM", ".bmp"),
]


def guess_ext(blob: bytes, default=".png"):
    for magic, ext in MAGIC:
        if blob.startswith(magic):
            return ext
    return default


class DingTalkChannel:
    name = PLATFORM

    def __init__(self, client_id, client_secret):
        self.client_id, self.client_secret = client_id, client_secret
        self.api = Client(client_id, client_secret)
        # chat_id → 发送目标。钉钉发消息要 robotCode 加收件人，而上层只给
        # chat_id，所以每收到一条消息就把路由记下来。
        self._route = {}

    # ---- 发送 ----

    def _target(self, chat_id):
        t = self._route.get(chat_id)
        if not t:
            raise DingTalkError(f"不知道往哪发：{chat_id} 尚未收到过消息")
        return t

    def send_text(self, chat_id, text):
        t = self._target(chat_id)
        return self.api.send(t["robot_code"], "sampleText", {"content": text},
                             user_id=t.get("user_id"),
                             conversation_id=t.get("conversation_id"))

    def send_post(self, chat_id, title, lines):
        """钉钉没有飞书那种富文本卡片，退化成 markdown。"""
        t = self._target(chat_id)
        return self.api.send(
            t["robot_code"], "sampleMarkdown",
            {"title": title, "text": "### " + title + "\n\n" + "\n\n".join(lines)},
            user_id=t.get("user_id"), conversation_id=t.get("conversation_id"))

    def send_file(self, chat_id, path, filename=None):
        t = self._target(chat_id)
        name = filename or os.path.basename(path)
        media_id = self.api.upload_media(path)
        return self.api.send(
            t["robot_code"], "sampleFile",
            {"mediaId": media_id, "fileName": name,
             "fileType": name.rsplit(".", 1)[-1] if "." in name else "file"},
            user_id=t.get("user_id"), conversation_id=t.get("conversation_id"))

    # 钉钉机器人没有表情回应能力，实现成空操作。上层已按"可能没有"来写。
    def react(self, message_id, emoji_type):
        return None

    def unreact(self, message_id, reaction_id):
        return None

    # ---- 接收 ----

    def _attach(self, code, robot_code, filename=None):
        """延迟下载：收到时不拉全量，真要用再取。

        图片没有文件名，而入库是按扩展名分类的，所以下载完再按字节头补上。
        这依赖上层的调用顺序——先 fetch() 拿数据、再读 filename 去入库，
        顺序反过来补名就失效了。
        """
        key = hashlib.sha1(code.encode()).hexdigest()[:16]
        att = Attachment(key, filename or "", "file" if filename else "image", None)

        def fetch():
            blob = self.api.download(code, robot_code)
            if not att.filename:
                att.filename = key + guess_ext(blob)
            return blob

        att.fetch = fetch
        return att

    def _to_inbound(self, raw: dict) -> InboundMessage:
        mtype = raw.get("msgtype") or ""
        robot = raw.get("robotCode") or ""
        is_group = str(raw.get("conversationType")) == "2"
        content = raw.get("content") or {}
        text, atts = "", []

        if mtype == "text":
            text = (raw.get("text") or {}).get("content") or ""
        elif mtype == "picture" and content.get("downloadCode"):
            atts.append(self._attach(content["downloadCode"], robot))
        elif mtype == "file" and content.get("downloadCode"):
            atts.append(self._attach(content["downloadCode"], robot,
                                     content.get("fileName") or "未命名"))
        elif mtype == "richText":
            parts = []
            for node in (content.get("richText") or []):
                if node.get("text"):
                    parts.append(node["text"])
                if node.get("downloadCode"):
                    # 富文本里目前只见过图片；真出现带文件名的节点也能接住
                    atts.append(self._attach(node["downloadCode"], robot,
                                             node.get("fileName")))
            text = "".join(parts)
        else:
            log.warning("未处理的钉钉消息类型 type=%s content=%s",
                        mtype, json.dumps(raw, ensure_ascii=False)[:400])

        chat_id = raw.get("conversationId") or ""
        # 记住怎么回：群聊用 conversationId（实测它就是 openConversationId），
        # 单聊用发送者的 staffId
        self._route[chat_id] = {
            "robot_code": robot,
            "conversation_id": chat_id if is_group else None,
            "user_id": None if is_group else raw.get("senderStaffId"),
        }
        return InboundMessage(
            PLATFORM, chat_id, raw.get("senderStaffId"), raw.get("msgId") or "",
            text.strip(), atts,
            chat_type="group" if is_group else "p2p",
            raw={"message_type": mtype})

    def start(self, on_message):
        """阻塞运行长连接。断线由 SDK 自行重连。"""
        import dingtalk_stream

        channel = self

        class Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):
                raw = callback.data
                try:
                    m = channel._to_inbound(raw)
                    log.info("收到钉钉消息 type=%s chat=%s id=%s",
                             raw.get("msgtype"), m.chat_id, m.message_id)
                    on_message(m)
                except Exception:
                    log.exception("处理钉钉消息失败")
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        cred = dingtalk_stream.Credential(self.client_id, self.client_secret)
        client = dingtalk_stream.DingTalkStreamClient(cred)
        client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC, Handler())
        log.info("钉钉长连接启动中…")
        client.start_forever()
