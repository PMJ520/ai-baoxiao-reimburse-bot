# -*- coding: utf-8 -*-
"""IM 通道的统一抽象。

上层（会话编排）只依赖这里的接口，换平台不必改业务逻辑。
钉钉、企业微信后续各实现一个适配器即可。
"""
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class InboundMessage:
    """收到的消息。文件与图片统一以 attachments 表达，上层不关心平台差异。"""
    platform: str
    chat_id: str
    user_id: str | None
    message_id: str
    text: str = ""
    attachments: list["Attachment"] = field(default_factory=list)
    chat_type: str = "p2p"          # p2p 私聊 / group 群聊
    raw: dict | None = None

    @property
    def is_p2p(self):
        return self.chat_type == "p2p"

    @property
    def has_files(self):
        return bool(self.attachments)


@dataclass
class Attachment:
    file_key: str
    filename: str
    kind: str            # image / file
    fetch: Callable[[], bytes] | None = None   # 延迟下载，避免收到即拉全量


class IMChannel(Protocol):
    name: str

    # 群里能不能收文件。钉钉不能——群机器人只收 @ 它的消息，而发文件时
    # 无法同时 @。上层据此决定要不要把用户引导到单聊。
    group_file_limited: bool

    def send_text(self, chat_id: str, text: str) -> None: ...

    def send_file(self, chat_id: str, path: str, filename: str | None = None) -> None: ...

    def start(self, on_message: Callable[[InboundMessage], None]) -> None: ...

    # 表情回应用于表达"已看到/处理中/已完成"。平台不支持时实现成空操作即可，
    # 上层不必判断平台差异。
    # 主动给某个人发单聊。平台不支持时返回 False，上层退回纯文案引导。
    def send_direct(self, user_id: str, text: str) -> bool: ...

    def react(self, message_id: str, emoji_type: str) -> str | None: ...

    def unreact(self, message_id: str, reaction_id: str | None) -> None: ...
