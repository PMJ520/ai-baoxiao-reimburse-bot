# -*- coding: utf-8 -*-
"""消息处理：把 IM 消息路由到入库或指令处理。

设计原则与命令行版一致——**报不报销、花在什么事上，都由用户拍板**。
系统负责识别、匹配、算账和追问，不替用户做判断。
"""
import logging
import os
import re
from datetime import datetime

from ..db.base import SessionLocal
from ..db import models as M
from ..services import batch as B
from ..services import ingest
from . import state as S

log = logging.getLogger(__name__)

# 飞书表情代号（须用官方代号，写错会静默失败——EyesLook 就是无效的）
EMOJI_WORKING = "OnIt"        # 处理中
EMOJI_OK = "DONE"             # 已完成
EMOJI_FAIL = "CrossMark"      # 失败

HELP = [
    "我可以帮你整理费用报销。",
    "",
    "· 随时把发票、支付截图发给我，我会当场识别并记下来",
    "· 需要出表时说一句「整理 7-8 月报销」",
    "· 想看进度说「到哪了」，想重来说「取消」",
    "",
    "发单据的方式：",
    "· 图片和 PDF 都行，一次选中多个一起发也可以",
    "· 合并转发不行——打包后我取不到里面的原件，请直接发原图或原文件",
    "· 视频、语音、名片、位置里取不出单据",
]


# 取不出单据的消息类型。留空建议表示"不必回应"（如表情包），
# 其余都要明确说清：是什么、为什么不行、改怎么做——只报类型名等于没说。
UNSUPPORTED = {
    "merge_forward": ("合并转发",
                      "合并转发是把多条消息打包成一条，我取不到里面的原件。"
                      "请把图片或 PDF 直接发给我——一次选中多个一起发是可以的。"),
    "forward": ("转发的消息",
                "这条转发我取不到原件。请把图片或 PDF 直接发给我。"),
    "media": ("视频", "我只认发票 PDF 和支付截图，视频里的内容取不出来。"),
    "audio": ("语音", "我还不能听语音。要下指令请发文字，例如「整理 7-8 月报销」。"),
    "sticker": ("表情", ""),
    "location": ("位置", "位置信息用不上。需要报销的话请发单据。"),
    "share_chat": ("群名片", "群名片用不上。需要报销的话请发单据。"),
    "share_user": ("个人名片", "名片用不上。需要报销的话请发单据。"),
    "todo": ("待办", "待办卡片里取不到单据，请直接发图片或 PDF。"),
    "vote": ("投票", "投票卡片里取不到单据，请直接发图片或 PDF。"),
    "calendar": ("日程", "日程卡片里取不到单据，请直接发图片或 PDF。"),
    "system": ("系统消息", ""),
}

# _on_text 的返回标记
UNHANDLED = object()   # 没能处理：表情打叉，不是打勾
IGNORED = object()     # 与我无关（表情包、系统消息）：不做任何表情回应


def handle(channel, msg):
    """处理一条消息。文件走入库，文本走指令。

    入口先做幂等：IM 平台会重投未及时确认的消息，重复处理会让用户收到
    重复回复。靠数据库唯一约束判重，并发下也安全。
    """
    with SessionLocal() as session:
        if _seen(session, channel.name, msg.message_id):
            log.info("消息 %s 已处理过，跳过", msg.message_id)
            return
        conv = S.get_or_create(session, channel.name, msg.chat_id, msg.user_id)

        # 飞书没有让机器人标记已读的接口，用表情回应代替回执。
        # 文件要下载加识别、耗时数秒，先打"处理中"再换结果；
        # 文本处理很快，直接给结果，免得表情闪一下。
        if not msg.has_files:
            try:
                r = _on_text(channel, session, conv, msg)
            except Exception:
                _react(channel, msg.message_id, EMOJI_FAIL)
                raise
            # 明确回绝过的消息不该打勾，否则看起来像已经办好了；
            # 与我无关的消息则连表情都不该有，免得像在抢话
            if r is not IGNORED:
                _react(channel, msg.message_id,
                       EMOJI_FAIL if r is UNHANDLED else EMOJI_OK)
            return

        working = _react(channel, msg.message_id, EMOJI_WORKING)
        try:
            n = _on_files(channel, session, conv, msg)
        except Exception:
            _swap(channel, msg.message_id, working, EMOJI_FAIL)
            raise
        _swap(channel, msg.message_id, working, EMOJI_OK if n else EMOJI_FAIL)
        return n


def _react(channel, message_id, emoji):
    """加表情。通道未实现（如未来接入的其它平台）时静默跳过。"""
    fn = getattr(channel, "react", None)
    return fn(message_id, emoji) if callable(fn) else None


def _swap(channel, message_id, old_id, emoji):
    """把处理中的表情换成结果表情。"""
    fn = getattr(channel, "unreact", None)
    if callable(fn):
        fn(message_id, old_id)
    _react(channel, message_id, emoji)


def _seen(session, platform, message_id):
    """记录并判断消息是否已处理过。返回 True 表示是重复投递。"""
    if not message_id:
        return False
    from sqlalchemy.exc import IntegrityError
    ev = M.ProcessedEvent(platform=platform, message_id=message_id)
    session.add(ev)
    try:
        session.commit()          # 唯一约束冲突即说明重复
        return False
    except IntegrityError:
        session.rollback()
        return True


# ---------- 文件 ----------
def _on_files(channel, session, conv, msg):
    lines, added = [], 0
    for att in msg.attachments:
        try:
            data = att.fetch() if att.fetch else None
            if not data:
                lines.append(f"· {att.filename} 下载失败")
                continue
            doc, created = ingest.ingest_bytes(
                session, data, att.filename, source=f"im_{channel.name}",
                external_id=msg.message_id, uploader=msg.user_id)
        except Exception as e:
            log.exception("入库失败 %s", att.filename)
            lines.append(f"· {att.filename} 处理失败：{e}")
            continue
        added += 1
        lines.append(_describe(doc, created))
    if lines:
        head = f"收到 {added} 个文件：" if added else "收到，但没能处理："
        channel.send_text(msg.chat_id, head + "\n" + "\n".join(lines))
    return added


def _describe(doc, created):
    """回执写清识别结果，让用户当场就能发现认错了——而不是月底才发现。"""
    if not created:
        # 用户主动重发时，说清它对应哪一笔，比只说"发过了"有用
        when = doc.occurred_at.strftime("%m-%d %H:%M") if doc.occurred_at else "时间未知"
        amt = f"{float(doc.amount):.2f} 元" if doc.amount is not None else ""
        return f"· 这张之前收过了（{when} {amt}），不会重复计入"
    kind = {"payment": "支付截图", "invoice": "发票",
            "itinerary": "行程单"}.get(doc.kind, "文件")
    if doc.status == M.ST_NEEDS_REVIEW:
        return f"· {doc.filename}（{kind}）没认全：{doc.parse_error}，稍后我会问你"
    if doc.kind == M.KIND_ITINERARY:
        # 行程单本就没有单一金额，回执要说清它的用途，别让人以为识别失败
        return f"· {kind} {doc.filename}，将用于把汇总发票分摊到每一笔"
    when = doc.occurred_at.strftime("%m-%d %H:%M") if doc.occurred_at else "时间未知"
    amt = f"{float(doc.amount):.2f} 元" if doc.amount is not None else "金额未知"
    who = f" {doc.merchant}" if doc.merchant else ""
    return f"· {kind} {when} {amt}{who}"


# ---------- 文本 ----------
def _on_text(channel, session, conv, msg):
    # 状态优先于关键词：流程中只有少数指令能打断，否则用户一句
    # "只整理3、6两笔"里的"整理"二字就会把当前批次冲掉
    cmd = S.command_for(msg.text, conv.state)
    if conv.state in S.ORGANIZING:
        S.push_history(session, conv, "user", msg.text)
    awaiting = (conv.context or {}).get("awaiting")
    if not cmd:
        # 正在等用户回答时，任何文本都要接住
        if conv.state != S.IDLE or awaiting:
            return _continue_flow(channel, session, conv, msg)
        mtype = (msg.raw or {}).get("message_type")
        if mtype in UNSUPPORTED:
            name, advice = UNSUPPORTED[mtype]
            if not advice:              # 表情、系统消息之类，不值得回一句
                return IGNORED
            # 这类消息在群里同样要回绝：单据本来就允许在群里发，
            # 沉默会让人以为收到了、正在处理
            log.warning("收到不支持的消息类型 %s", mtype)
            channel.send_text(msg.chat_id, f"这条是{name}，我处理不了。\n{advice}")
            return UNHANDLED
        # 群聊里不接话，免得抢群成员的话题；私聊里必须回，
        # 否则用户发什么都没反应，会以为服务挂了。
        if not msg.is_p2p:
            return
        if mtype not in ("text", "post", None):
            # 没进上表的新类型：说清收到了但取不出单据，并留日志备查
            log.warning("未知消息类型 %s，未解出附件", mtype)
            channel.send_text(
                msg.chat_id,
                f"这条消息（类型 {mtype}）我收到了，但取不出可用的单据。\n"
                "请把图片或 PDF 直接发给我，一次发多个也可以。")
            return UNHANDLED
        return channel.send_text(
            msg.chat_id,
            "我没听懂这句。你可以：\n"
            "· 直接把发票或支付截图发给我（一次发多个也行）\n"
            "· 说「整理 7-8 月报销」出台账\n"
            "· 说「帮助」看完整用法")

    c = cmd["cmd"]
    if c == S.CMD_HELP:
        return channel.send_post(msg.chat_id, "费用报销助手", HELP)
    if c == S.CMD_CANCEL:
        S.reset(session, conv)
        return channel.send_text(msg.chat_id, "已取消，材料都还在，随时可以重新整理。")
    if c == S.CMD_STATUS:
        return _status(channel, session, conv, msg)
    if c == S.CMD_ORGANIZE:
        # 手上还有没出完的批次时不静默重置——那会让之前的确认全部作废
        if conv.state != S.IDLE and conv.batch_id:
            S.transition(session, conv, conv.state, pending_period=[
                d.isoformat() for d in (cmd.get("period") or [])] or None,
                awaiting="switch_batch")
            return channel.send_text(
                msg.chat_id,
                "当前还有一批没走完。要放弃它、改整理新的时间范围吗？"
                "回「放弃」继续，回「取消」什么都不动。")
        return _organize(channel, session, conv, msg, cmd.get("period"))
    if c == S.CMD_CONFIRM:
        # 数据表已核对、或正在确认抬头，这时的"确认"都是指继续出台账
        if conv.state in (S.SHEET_REVIEW, S.ASK_PEOPLE):
            return _make_ledger(channel, session, conv, msg)
        return _confirm(channel, session, conv, msg)
    if c == S.CMD_EXPORT:
        # 数据表已出且核对通过，这时的"出表"指的是专用台账
        if conv.state in (S.SHEET_REVIEW, S.ASK_PEOPLE):
            return _make_ledger(channel, session, conv, msg)
        return _export(channel, session, conv, msg)


def _organize(channel, session, conv, msg, period):
    # 新的一段任务，上一段的对话作废
    S.clear_history(session, conv)
    if not period:
        S.transition(session, conv, S.IDLE, awaiting="period")
        return channel.send_text(
            msg.chat_id, "要整理哪段时间的？比如「整理 7-8 月报销」，"
                         "或者直接给起止日期。")

    start, end = period
    b, rec = B.create_batch(session, start, end, owner=msg.user_id,
                            title=f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}")
    conv.batch_id = b.id
    su = rec["summary"]
    if not rec["payments"]:
        S.reset(session, conv)
        return channel.send_text(
            msg.chat_id, f"{start:%m月%d日} 到 {end:%m月%d日} 这段时间我这儿没有支付记录，"
                         f"确认一下时间范围？")

    lines = [f"共 {su['n_total']} 笔，实付合计 {su['total']:.2f} 元",
             f"其中有发票 {su['invoice_covered']:.2f} 元，"
             f"需替票 {su['substitute']:.2f} 元（{su['n_substitute']} 笔）"]
    for a in rec["allocations"]:
        ok = "已分摊" if a["ok"] else f"分摊差 {a['diff']:+.2f}"
        lines.append(f"· 汇总发票 {a['invoice_total']:.2f} → "
                     f"{a['matched']}/{a['expected']} 笔，{ok}")
    if rec["gaps"]:
        lines.append("")
        lines.append(f"有 {len(rec['gaps'])} 处需要你确认：")
        lines.extend("· " + g["message"] for g in rec["gaps"][:8])
        if len(rec["gaps"]) > 8:
            lines.append(f"…… 还有 {len(rec['gaps']) - 8} 处")

    lines.append("")
    lines.append("接下来我会逐项提议费用明细和类别，你确认后就能出表。")
    S.transition(session, conv, S.CONFIRMING, batch_id=b.id,
                 period=[str(start), str(end)])
    channel.send_post(msg.chat_id, f"核对结果 · {b.title}", lines)
    _propose(channel, session, conv, msg, b)
    return b.id


def _status(channel, session, conv, msg):
    if conv.state == S.IDLE or not conv.batch_id:
        return channel.send_text(msg.chat_id, "当前没有进行中的整理。")
    b = session.get(M.Batch, conv.batch_id)
    filled = sum(1 for it in b.items if it.detail and it.category)
    return channel.send_text(
        msg.chat_id,
        f"批次 {b.title}：{len(b.items)} 笔，已填明细 {filled} 笔，状态 {b.status}。")


def _continue_flow(channel, session, conv, msg):
    """流程中的非指令文本：按当前在等什么来解释。"""
    awaiting = (conv.context or {}).get("awaiting")

    if awaiting == "period":
        p = S.parse_period(msg.text)
        if p:
            S.transition(session, conv, conv.state, awaiting=None)
            return _organize(channel, session, conv, msg, p)
        return channel.send_text(msg.chat_id, "没看懂时间范围，给个起止日期？")

    if awaiting == "switch_batch":
        if re.search(r"(放弃|是|好|确定|继续)", msg.text or ""):
            per = (conv.context or {}).get("pending_period")
            period = [datetime.fromisoformat(x) for x in per] if per else None
            S.reset(session, conv)
            return _organize(channel, session, conv, msg, period)
        S.transition(session, conv, conv.state, awaiting=None,
                     pending_period=None)
        return channel.send_text(msg.chat_id, "那就继续当前这批。")

    if awaiting == "people":
        req, hnd, com = _parse_people(msg.text)
        if not any((req, hnd, com)):
            return channel.send_text(
                msg.chat_id,
                "没看懂。可以说「需求人 张三，经办人 李四」、「都是张三」，"
                "或只改其中一项，如「需求人改成王五」。")
        b = session.get(M.Batch, conv.batch_id)
        if req:
            b.requester = req
        if hnd:
            b.handler = hnd
        if com:
            b.company = com
        session.commit()
        changed = "、".join(f"{k} {v}" for k, v in
                           (("需求人", req), ("经办人", hnd), ("出账主体", com)) if v)
        channel.send_text(msg.chat_id, f"已更新：{changed}")
        return _make_ledger(channel, session, conv, msg)

    if awaiting in ("confirm", "manual_detail"):
        # 用户在补充明细或纠正某笔。交给模型理解后回填，而不是让用户
        # 按固定格式填表——这正是用对话而非表单的意义。
        return _amend(channel, session, conv, msg)

    if conv.state == S.SHEET_REVIEW:
        # 数据表已出，这时说的话无非两种：让它继续出台账，或者要改。
        # 关键词只能认死词，打错一个字就卡住——交给模型判断意图
        return _amend(channel, session, conv, msg)

    return channel.send_text(
        msg.chat_id,
        "收到。要开始整理就说「整理 4-6 月报销」，说「帮助」看完整用法。")


_AMEND_SYS = """用户在核对一批费用报销条目，现在提出修改或补充。
请把用户的话转成对具体条目的修改。

规则：
1. 只输出 JSON，不要解释。
2. 用户可能指某一笔（"第2笔"）、某一类（"打车的都算交通费"）或全部。
3. 不要编造用户没说的信息。
4. category 只能从这些里选：{categories}

5. 用户可能要求只保留某几笔、或把某几笔挪出本批次（"其他待定"）。
   移出的条目不会丢失，原件仍在待整理池里，下次还能整理。
6. 用户只是提问（"这几笔没有行程单吗"）时，不要改动任何条目，
   把 updates/keep_only/defer 都留空，只在 reply 里回答。

7. 用户表示认可、要继续往下走（"可以""没问题""出台账"，也可能有错别字
   如"出台张"）时，把 proceed 设为 true，其余字段留空。

输出格式（各字段都可省略）：
{{"updates": [{{"seq": 2, "detail": "...", "category": "招待费"}}],
  "keep_only": [3, 6],
  "defer": [5],
  "proceed": false,
  "reply": "一句话告诉用户你做了什么"}}
"""


def _amend(channel, session, conv, msg):
    from ..services.enrich import CATEGORIES
    from ..services.llm import get_client, parse_json
    import json as _json

    b = session.get(M.Batch, conv.batch_id) if conv.batch_id else None
    if not b:
        return channel.send_text(msg.chat_id, "当前没有进行中的批次。")

    items = [{"seq": it.seq, "金额": float(it.amount) if it.amount is not None else None,
              "当前明细": it.detail, "当前类别": it.category,
              "商户": it.document.merchant if it.document else None}
             for it in b.items]
    # 整理期内上下文不设限：这一段从"开始整理"到"确认数据表"是一个完整的
    # 语义单元，前面任何一句都可能是后文的前提（"那就只要3、6两笔"里的"那"
    # 指向哪一轮，只有看过全程才知道）
    hist, folded = S.history_for_prompt(conv)
    payload = {"条目": items, "用户说": msg.text}
    if hist:
        payload["之前的对话"] = [f"{h['role']}: {h['text']}" for h in hist]
    if folded:
        payload["提示"] = "对话过长，前面若干轮已折叠，如需精确请让用户重述"
    try:
        cli = get_client()
        raw = cli.complete(_AMEND_SYS.format(categories="、".join(CATEGORIES)),
                           _json.dumps(payload, ensure_ascii=False))
        data = parse_json(raw)
    except Exception:
        log.warning("理解修改意图失败", exc_info=True)
        return channel.send_text(
            msg.chat_id,
            "我没能理解这句话。可以说得具体些，例如"
            "「第2笔改成招待费」或「都是出差打车」。")

    # 用户只是表示"可以了、继续"：在数据表阶段就直接出台账。
    # 关键词认不出错别字，这一层由模型兜住
    if data.get("proceed") and not any(
            data.get(k) for k in ("updates", "keep_only", "defer")):
        if conv.state == S.SHEET_REVIEW:
            return _make_ledger(channel, session, conv, msg)
        return _confirm(channel, session, conv, msg)

    by_seq = {it.seq: it for it in b.items}
    n = 0
    for u in (data.get("updates") or []):
        it = by_seq.get(u.get("seq"))
        if not it:
            continue
        if u.get("detail"):
            it.detail = u["detail"]
        if u.get("category") in CATEGORIES:
            it.category = u["category"]
        n += 1

    # 移出条目是有副作用的动作，执行后必须回显清楚，不能默默改完
    keep = {int(x) for x in (data.get("keep_only") or []) if str(x).isdigit()}
    drop = {int(x) for x in (data.get("defer") or []) if str(x).isdigit()}
    if keep:
        drop |= {it.seq for it in b.items if it.seq not in keep}
    moved = []
    for seq in sorted(drop):
        it = by_seq.get(seq)
        if it:
            moved.append(seq)
            session.delete(it)      # 文件此时尚未标记已用，原件仍在待整理池
    session.commit()

    if moved:
        # 序号要重排，否则台账行号会跳号
        for i, it in enumerate(sorted(b.items, key=lambda x: x.seq), 1):
            it.seq = i
        session.commit()

    reply = data.get("reply") or (f"已更新 {n} 笔。" if n else "好的。")
    if moved:
        reply += (f"\n已移出第 {'、'.join(map(str, moved))} 笔，共 {len(moved)} 笔；"
                  f"它们的原件仍在待整理池里，下次整理还会出现。"
                  f"本批现有 {len(b.items)} 笔，序号已重排。")
    left = [it.seq for it in b.items if not (it.detail and it.category)]
    if not b.items:
        reply += "\n本批已经没有条目了，说「取消」可以重来。"
    elif left:
        reply += f"\n还有 {len(left)} 笔待填（第 {'、'.join(map(str, left[:8]))} 笔）。"
    else:
        reply += "\n全部填好了，回「确认」即可出表。"
    channel.send_text(msg.chat_id, reply)
    S.push_history(session, conv, "bot", reply)
    # 数据表已经发出去了又改了内容，必须重出——否则用户手上那份和
    # 系统里的对不上，而这种不一致在台账里看不出来
    if conv.state == S.SHEET_REVIEW and (n or moved) and b.items:
        channel.send_text(msg.chat_id, "内容有变，重新生成数据表…")
        return _export(channel, session, conv, msg)
    return reply


# ---------- 逐项确认 → 出表 ----------
def _propose(channel, session, conv, msg, batch):
    """让模型提议费用明细与类别，按类别归组呈现。

    归组是为了让用户一次看清、一次确认——二十笔一笔一问会耗尽耐心。
    模型不可用时退回纯人工填写，不阻断流程。
    """
    from ..services.enrich import pending_fields, propose

    todo = [it for it in batch.items if not (it.detail and it.category)]
    if not todo:
        return _ask_people(channel, session, conv, msg, batch)

    # 单据上已经写明的信息要一并交给模型，否则它只能把这些也列成"请你补充"，
    # 等于让用户重新提供自己刚上传过的东西
    from ..services.batch import trip_info
    trips = trip_info(session, batch)
    payload = []
    for it in todo:
        d = {"id": it.seq, "occurred_at": it.occurred_at,
             "amount": float(it.amount) if it.amount is not None else None,
             "merchant": it.document.merchant if it.document else None}
        inv = it.invoice
        if inv and (inv.parsed or {}).get("items"):
            d["invoice_items"] = "、".join(inv.parsed["items"])
        d.update(trips.get(it.seq) or {})
        payload.append(d)
    props = propose(payload)
    if not props:
        S.transition(session, conv, S.CONFIRMING, awaiting="manual_detail")
        return channel.send_text(
            msg.chat_id,
            "模型暂时不可用，需要你直接告诉我这些费用的用途和类别。\n"
            "例如：「都是出差打车，交通费」")

    by_seq = {it.seq: it for it in batch.items}
    for seq, pr in props.items():
        it = by_seq.get(seq)
        if it:
            it.detail = pr["detail"]
            it.category = pr["category"]
    session.commit()

    groups = {}
    for seq, pr in props.items():
        groups.setdefault(pr["category"], []).append((seq, pr["detail"]))

    lines = ["我按商户和发票项目做了如下判断，请你过目："]
    for cat, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        lines.append("")
        lines.append(f"【{cat}】{len(items)} 笔")
        for seq, detail in items[:6]:
            lines.append(f"  {seq}. {detail}")
        if len(items) > 6:
            lines.append(f"  …… 另有 {len(items) - 6} 笔同类")

    need = pending_fields(props)
    if need:
        lines.append("")
        lines.append("以下信息只有你知道，需要补充：")
        for field, seqs in need.items():
            lines.append(f"· {field}（第 {'、'.join(map(str, seqs))} 笔）")
        lines.append("")
        lines.append("直接告诉我即可，例如「第2笔是招待张总，3人」。")
    lines.append("")
    lines.append("没问题就回「确认」，要改就直接说。")

    S.transition(session, conv, S.CONFIRMING, batch_id=batch.id, awaiting="confirm")
    channel.send_post(msg.chat_id, "费用明细待确认", lines)


def _confirm(channel, session, conv, msg):
    b = session.get(M.Batch, conv.batch_id) if conv.batch_id else None
    if not b:
        return channel.send_text(msg.chat_id, "当前没有待确认的批次。")
    miss = [it.seq for it in b.items if not (it.detail and it.category)]
    if miss:
        return channel.send_text(
            msg.chat_id,
            f"还有 {len(miss)} 笔没填明细（第 {'、'.join(map(str, miss[:8]))} 笔），"
            f"补齐后再确认。")
    # 直接出通用数据表。需求人/经办人是台账的列，数据表里没有这些字段，
    # 这时候问等于提前打断核对节奏
    return _export(channel, session, conv, msg)


def _ask_people(channel, session, conv, msg, batch):
    """确认台账抬头信息。

    已知的直接列出来让用户过目——从"填空"变成"确认或修改"，
    多数情况一句「确认」就过了。两者可能不是同一人，不做默认相等的推断。
    """
    from ..services.export import PH_COMPANY, PH_NAME

    def val(v, ph):
        return v if v and v != ph else None

    req = val(batch.requester, PH_NAME)
    hnd = val(batch.handler, PH_NAME)
    com = val(batch.company, PH_COMPANY)

    S.transition(session, conv, S.ASK_PEOPLE, batch_id=batch.id, awaiting="people")
    lines = ["数据表核对通过。台账抬头信息如下：", ""]
    lines.append(f"· 需求人    {req or '（待填）'}")
    lines.append(f"· 经办人    {hnd or '（待填）'}")
    if com:
        lines.append(f"· 出账主体  {com}（取自发票购买方）")
    else:
        lines.append("· 出账主体  （待填）")
    lines.append("")
    if req and hnd:
        lines.append("没问题回「确认」即出台账；要改就说，")
        lines.append("例如「需求人改成张三」或「都是李四」。")
    else:
        lines.append("请补齐，例如「需求人 张三，经办人 李四」，")
        lines.append("同一人可直接说「都是张三」。")
    return channel.send_post(msg.chat_id, "确认台账抬头", lines)


_PEOPLE_BOTH = re.compile(r"需求人\s*(?:改成?|为|是)?\s*[:：]?\s*([^\s，,；;]+)"
                          r".*?经办人\s*(?:改成?|为|是)?\s*[:：]?\s*([^\s，,；;]+)")
_PEOPLE_SAME = re.compile(r"(?:都是|都改成|均为|同一人?[:：]?)\s*([^\s，,；;]+)")
_ONE_REQ = re.compile(r"需求人\s*(?:改成?|为|是)?\s*[:：]?\s*([^\s，,；;]+)")
_ONE_HND = re.compile(r"经办人\s*(?:改成?|为|是)?\s*[:：]?\s*([^\s，,；;]+)")
_ONE_COM = re.compile(r"(?:出账主体|公司|主体)\s*(?:改成?|为|是)?\s*[:：]?\s*([^\s，,；;]+)")


def _parse_people(text):
    """解析抬头信息。支持一次给两项、都相同、或只改其中一项。

    返回 (需求人, 经办人, 出账主体)，未提及的为 None——调用方据此只覆盖提到的项。
    """
    t = text or ""
    com = _ONE_COM.search(t)
    com = com.group(1) if com else None
    m = _PEOPLE_BOTH.search(t)
    if m:
        return m.group(1), m.group(2), com
    m = _PEOPLE_SAME.search(t)
    if m:
        return m.group(1), m.group(1), com
    r = _ONE_REQ.search(t)
    h = _ONE_HND.search(t)
    return (r.group(1) if r else None), (h.group(1) if h else None), com


def _export(channel, session, conv, msg):
    """第一段收尾：出通用数据表，交给用户核对。

    台账不在这里做——先前是一步到底，用户看到明细表时台账早已生成、
    材料也已归集，发现明细错了只能整批重来。
    """
    from ..services import export as EX
    b = session.get(M.Batch, conv.batch_id) if conv.batch_id else None
    if not b:
        return channel.send_text(msg.chat_id, "当前没有可出表的批次。")
    channel.send_text(msg.chat_id, "正在生成通用数据表…")
    try:
        sheet = EX.build_datasheet(session, b)
        summary = EX.category_summary(b)
    except Exception as e:
        log.exception("出数据表失败")
        return channel.send_text(msg.chat_id, f"生成数据表时出错：{e}")

    channel.send_file(msg.chat_id, sheet, "报销明细表.xlsx")
    channel.send_post(msg.chat_id, "通用数据表已生成，请核对",
                      _summary_lines(summary))
    # 整理这段任务到此为止，历史作废；出台账是另一件事，上下文重新开始
    S.clear_history(session, conv)
    S.transition(session, conv, S.SHEET_REVIEW, awaiting=None)
    return sheet


def _summary_lines(s):
    """确认用的汇总。只给分类合计与总计——逐笔明细在表里。"""
    out = []
    for cat, (n, amt) in s["categories"]:
        out.append(f"【{cat}】{n} 笔　{amt:,.2f} 元")
    out.append("─" * 18)
    out.append(f"合计 {s['n_total']} 笔　{s['total']:,.2f} 元")
    if s["inv_ledger"]:
        out.append(f"其中有发票 {s['inv_ledger']:,.2f} 元")
    if s["sub_total"]:
        out.append(f"需替票 {s['sub_total']:,.2f} 元（{s['n_sub']} 笔）")
    out.append("")
    out.append("核对无误回「出台账」，要改就直接说。")
    return out


def _make_ledger(channel, session, conv, msg):
    """第二段：出专用台账、归集材料。确认数据表之后才走到这里。"""
    from ..services import export as EX
    from ..services import export as EX2
    b = session.get(M.Batch, conv.batch_id) if conv.batch_id else None
    if not b:
        return channel.send_text(msg.chat_id, "当前没有待出台账的批次。")

    # 抬头是台账的列，到这一步才需要
    miss = EX2.missing_fields(b)
    if miss:
        if "需求人" in miss or "经办人" in miss:
            return _ask_people(channel, session, conv, msg, b)
        return channel.send_text(msg.chat_id, "还差：" + "；".join(miss))

    channel.send_text(msg.chat_id, "正在生成台账…")
    try:
        xlsx, manifest, text = EX.build_ledger(session, b)
    except Exception as e:
        log.exception("出台账失败")
        return channel.send_text(msg.chat_id, f"生成台账时出错：{e}")

    from ..services import profile as PF
    from ..services.batch import mark_used
    # 标记已用要等到台账真的产出之后：先前在确认抬头时就标记，用户看完
    # 数据表说"这批不对"的话，那些材料已经被当成消耗掉了
    mark_used(session, b)
    PF.save(session, channel.name, b.owner or msg.user_id,
            requester=b.requester, handler=b.handler, company=b.company)

    channel.send_file(msg.chat_id, xlsx, os.path.basename(xlsx))
    channel.send_file(msg.chat_id, manifest, "交付清单.txt")
    channel.send_post(msg.chat_id, "台账已生成", text.splitlines()[:40])
    S.reset(session, conv)
    return xlsx
