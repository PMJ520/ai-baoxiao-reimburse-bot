# -*- coding: utf-8 -*-
"""数据模型。

核心设计：documents（收到的文件，流水账）与 expense_items（确认后的条目，
台账一行）分离。文件入库即解析并落 occurred_at，整理时按消费发生时间
而非上传时间检索——9 月上传 7 月的发票是常态。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

# 文件类型
KIND_PAYMENT = "payment"        # 支付截图
KIND_INVOICE = "invoice"        # 发票
KIND_ITINERARY = "itinerary"    # 行程单等辅助单据
KIND_UNKNOWN = "unknown"

# 文件状态：收到 → 已解析 → 待人工复核 / 已用于某批次 / 已排除
ST_RECEIVED = "received"
ST_PARSED = "parsed"
ST_NEEDS_REVIEW = "needs_review"
ST_USED = "used"
ST_EXCLUDED = "excluded"

# 批次状态
B_DRAFT = "draft"               # 已组批，待确认明细
B_CONFIRMING = "confirming"     # 对话确认中
B_READY = "ready"               # 已确认，待生成
B_DONE = "done"                 # 已出表

Money = Numeric(12, 2)          # 金额一律用定点数，避免浮点累加误差


class Document(Base):
    """收到的每一个文件。原件存文件系统，这里只存元数据与解析结果。"""
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    rel_path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[str] = mapped_column(String(32), default="web")   # im_feishu/web/cli
    external_id: Mapped[str | None] = mapped_column(String(128))     # IM 消息或文件 id
    uploader: Mapped[str | None] = mapped_column(String(128))        # IM 用户 id

    kind: Mapped[str] = mapped_column(String(24), default=KIND_UNKNOWN, index=True)
    status: Mapped[str] = mapped_column(String(24), default=ST_RECEIVED, index=True)

    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    amount: Mapped[float | None] = mapped_column(Money)
    merchant: Mapped[str | None] = mapped_column(String(255))
    parsed: Mapped[dict | None] = mapped_column(JSON)
    parse_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_doc_kind_time", "kind", "occurred_at"),)

    def __repr__(self):
        return f"<Document {self.id} {self.kind} {self.occurred_at} {self.amount}>"


class Template(Base):
    """专用模版。spec 描述列映射、固定值与版面规则，支持多份。"""
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(24), default="builtin")   # builtin/custom
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Batch(Base):
    """一次报销整理。按消费时间范围组批。"""
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    period_start: Mapped[datetime | None] = mapped_column(DateTime)
    period_end: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default=B_DRAFT, index=True)

    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"))
    requester: Mapped[str | None] = mapped_column(String(64))     # 需求人
    handler: Mapped[str | None] = mapped_column(String(64))       # 经办人
    company: Mapped[str | None] = mapped_column(String(255))      # 出账主体

    datasheet_path: Mapped[str | None] = mapped_column(String(512))   # 通用数据表
    ledger_path: Mapped[str | None] = mapped_column(String(512))       # 专用台账
    manifest_path: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)

    items: Mapped[list["ExpenseItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan",
        order_by="ExpenseItem.seq")
    template: Mapped["Template | None"] = relationship()


class ExpenseItem(Base):
    """确认后的报销条目，对应台账一行。

    detail 与 category 是仅有的两个靠对话确认的字段——金额、时间、凭证关系
    全部来自解析结果，不经模型的手。
    """
    __tablename__ = "expense_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)

    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    invoice_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))

    occurred_at: Mapped[datetime | None] = mapped_column(DateTime)
    amount: Mapped[float | None] = mapped_column(Money)          # 实付
    invoice_amount: Mapped[float | None] = mapped_column(Money)  # 被发票覆盖的金额

    detail: Mapped[str | None] = mapped_column(Text)             # 费用具体明细
    category: Mapped[str | None] = mapped_column(String(64))     # 费用类别
    note: Mapped[str | None] = mapped_column(String(255))        # 备注
    voucher_type: Mapped[str | None] = mapped_column(String(24))  # 发票/发票+替票/替票

    batch: Mapped["Batch"] = relationship(back_populates="items")
    document: Mapped["Document | None"] = relationship(foreign_keys=[document_id])
    invoice: Mapped["Document | None"] = relationship(foreign_keys=[invoice_document_id])

    __table_args__ = (UniqueConstraint("batch_id", "document_id",
                                       name="uq_item_batch_doc"),)


class Conversation(Base):
    """IM 会话状态。用户可能隔几天才回，状态必须落库。"""
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(24), default="feishu")
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="idle")
    context: Mapped[dict | None] = mapped_column(JSON)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("platform", "chat_id", name="uq_conv_chat"),)

class ProcessedEvent(Base):
    """已处理的 IM 事件。

    IM 平台在超时未收到确认时会重投同一条消息（实测飞书相隔约 2 分钟重发一次）。
    下载加识别本就耗时，很容易踩到这个窗口。仅靠内容去重只能挡住脏数据，
    用户仍会收到莫名其妙的重复回复，故必须在消息入口做幂等。
    """
    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(24))
    message_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("platform", "message_id",
                                       name="uq_event_msg"),)


class UserProfile(Base):
    """用户的台账默认信息。

    需求人、经办人、出账主体在同一个人的多次报销里基本不变，
    每次从头问一遍很啰嗦。存下来，之后只需确认或修改。
    """
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(24), default="feishu")
    user_id: Mapped[str] = mapped_column(String(128))
    requester: Mapped[str | None] = mapped_column(String(64))
    handler: Mapped[str | None] = mapped_column(String(64))
    company: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("platform", "user_id",
                                       name="uq_profile_user"),)


class Setting(Base):
    """可在后台修改的配置。

    环境变量只作初始值；改密码、配 IM 凭据这些要能在容器内持久化，
    否则每次都得改 .env 重启。库里有值就以库为准。
    """
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())
