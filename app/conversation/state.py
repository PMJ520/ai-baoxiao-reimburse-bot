# -*- coding: utf-8 -*-
"""会话状态机。

IM 对话是异步且可能长时间中断的——用户今天发一句"整理7月报销"，
明天才回来确认明细。状态与上下文必须落库，不能放内存。
"""
import json
import re
from datetime import datetime

from sqlalchemy import select

from ..db import models as M

IDLE = "idle"               # 空闲，等指令
COLLECTING = "collecting"   # 已组批，正在补齐缺失材料
CONFIRMING = "confirming"   # 逐项确认明细与类别
ASK_PEOPLE = "ask_people"   # 询问需求人/经办人
READY = "ready"             # 待最终确认出表
SHEET_REVIEW = "sheet_review"   # 通用数据表已出，等你核对后再做台账

# 会话一（整理）的状态集合：这几个状态里上下文连贯，历史全量保留
ORGANIZING = (COLLECTING, CONFIRMING, ASK_PEOPLE, READY)


def get_or_create(session, platform, chat_id, user_id=None):
    conv = session.scalar(select(M.Conversation).where(
        M.Conversation.platform == platform, M.Conversation.chat_id == chat_id))
    if conv:
        if user_id and not conv.user_id:
            conv.user_id = user_id
            session.commit()
        return conv
    conv = M.Conversation(platform=platform, chat_id=chat_id, user_id=user_id,
                          state=IDLE, context={})
    session.add(conv)
    session.commit()
    return conv


def transition(session, conv, state, **ctx):
    conv.state = state
    merged = dict(conv.context or {})
    merged.update(ctx)
    conv.context = merged
    session.commit()
    return conv


def reset(session, conv):
    conv.state = IDLE
    conv.context = {}
    conv.batch_id = None
    session.commit()
    return conv


# ---------- 指令解析 ----------
# 只认少数明确的关键词，避免把用户的闲聊误判成指令。
CMD_ORGANIZE = "organize"
CMD_STATUS = "status"
CMD_CANCEL = "cancel"
CMD_HELP = "help"
CMD_CONFIRM = "confirm"      # 确认当前提议
CMD_EXPORT = "export"        # 生成台账

# 流程进行中允许打断的指令。除这几个之外，任何文本都交给当前流程——
# 否则用户一句"只整理3、6两笔"里的"整理"二字就会把整个批次冲掉
INTERRUPTS = {CMD_CANCEL, CMD_HELP, CMD_STATUS, CMD_CONFIRM, CMD_EXPORT}

_MONTH = re.compile(r"(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*月")
_SINGLE_MONTH = re.compile(r"(\d{1,2})\s*月")
_RANGE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*[-~到至]\s*"
                    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def parse_command(text, now=None, in_flow=False):
    """从自然语言里识别指令与时间范围。识别不出范围就返回 None 让上层追问。

    in_flow 表示当前有进行中的流程。此时只认 INTERRUPTS 里那几个明确指令，
    其余一律返回 None 交给流程自己解释——状态必须压过关键词，否则
    "只整理3、6两笔"这种正常说法会被当成"新建一个批次"。
    """
    t = (text or "").strip()
    if not t:
        return None
    if re.search(r"(取消|算了|重来)", t):
        return {"cmd": CMD_CANCEL}
    if re.search(r"(帮助|怎么用|help)", t, re.I):
        return {"cmd": CMD_HELP}
    if re.search(r"(进度|状态|到哪了)", t):
        return {"cmd": CMD_STATUS}
    # 先判确认与出表——「出表」也含"表"字，要排在整理前面，否则会被误判
    if re.search(r"^(确认|对的?|没问题|可以|好的?|ok)$", t, re.I):
        return {"cmd": CMD_CONFIRM}
    # 「出台账」是提示语里让用户说的词，务必在列——提示语和解析器脱节，
    # 用户照着说反而不认，比不提示还糟
    if re.search(r"(出台账|出台帐|出账|出表|生成台账|导出|给我表)", t):
        return {"cmd": CMD_EXPORT}
    if re.search(r"(整理|报销|做台账)", t):
        c = {"cmd": CMD_ORGANIZE, "period": parse_period(t, now)}
        # 流程中说"整理…"：只有给了明确时间范围才当成想开新批次，
        # 且上层还会先问一句是否放弃当前批次，不静默重置
        if in_flow and not c["period"]:
            return None
        return c
    return None


def command_for(text, state, now=None):
    """按当前状态解析指令。流程中只放行可打断的那几个。"""
    in_flow = state != IDLE
    c = parse_command(text, now, in_flow=in_flow)
    if c and in_flow and c["cmd"] not in INTERRUPTS and c["cmd"] != CMD_ORGANIZE:
        return None
    return c


def parse_period(text, now=None):
    """解析时间范围。支持「7-8月」「7月」「2026-07-10 至 2026-08-31」。"""
    now = now or datetime.now()
    m = _RANGE.search(text)
    if m:
        a = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        b = datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)), 23, 59, 59)
        return [a, b]
    m = _MONTH.search(text)
    if m:
        y = now.year
        return [datetime(y, int(m.group(1)), 1), _month_end(y, int(m.group(2)))]
    m = _SINGLE_MONTH.search(text)
    if m:
        mo = int(m.group(1))
        return [datetime(now.year, mo, 1), _month_end(now.year, mo)]
    return None


def _month_end(year, month):
    if month == 12:
        return datetime(year, 12, 31, 23, 59, 59)
    nxt = datetime(year, month + 1, 1)
    return datetime.fromtimestamp(nxt.timestamp() - 1)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------- 会话历史 ----------
# 边界按任务划分而不是按轮数截断：从「开始整理」到「确认通用数据表」是
# 一段完整的语义单元，中间的每一句都可能是后文的前提（"那就只要3、6两笔"
# 里的"那"指向哪一轮，只有看过全程才知道）。所以整理期内不限轮数。
#
# 兜底阈值只防异常——正常一次整理几千字，到不了这里。真触发时会明确
# 告知用户已折叠，而不是让模型在缺失信息的情况下装作全都看见了。
HISTORY_CAP = 80_000


def push_history(session, conv, role, text):
    """记一轮对话。role 为 user 或 bot。"""
    t = (text or "").strip()
    if not t:
        return
    ctx = dict(conv.context or {})
    hist = list(ctx.get("history") or [])
    hist.append({"role": role, "text": t})
    ctx["history"] = hist
    conv.context = ctx
    session.commit()


def history_for_prompt(conv):
    """取出供模型使用的历史，返回 (轮次列表, 是否折叠过)。"""
    hist = list((conv.context or {}).get("history") or [])
    total = sum(len(h["text"]) for h in hist)
    if total <= HISTORY_CAP:
        return hist, False
    kept, size = [], 0
    for h in reversed(hist):                 # 从最近往前保留
        size += len(h["text"])
        if size > HISTORY_CAP:
            break
        kept.append(h)
    return list(reversed(kept)), True


def clear_history(session, conv):
    """任务结束，历史作废。下一段任务重新开始。"""
    ctx = dict(conv.context or {})
    ctx.pop("history", None)
    conv.context = ctx
    session.commit()
