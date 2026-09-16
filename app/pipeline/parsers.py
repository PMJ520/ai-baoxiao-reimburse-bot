# -*- coding: utf-8 -*-
"""发票 PDF 与支付截图的解析。"""
import os
import re
import subprocess
from datetime import datetime

import pdfplumber

# 分隔符可缺失：部分识别引擎会把日期与时间之间的空格吞掉，
# 例如 "2026-07-3008:44:40"，不容错就会漏掉这个时间戳。
TS_RE = re.compile(r"20\d{2}-\d{2}-\d{2}[ T]?\d{2}:\d{2}:\d{2}")
DATE_RE = re.compile(r"20\d{2}[-年]\d{1,2}[-月]\d{1,2}")
AMT_NEG_RE = re.compile(r"^[-−–—]\s*(\d[\d,]*\.\d{2})$")
AMT_ANY_RE = re.compile(r"^[-−–—+]?\s*(\d[\d,]*\.\d{2})$")
ORDER_RE = re.compile(r"^\d{18,}$")

DOCS_DIR = "发票"                 # 归档目录名
_AMT_SUFFIX = re.compile(r"-\d+(?:\.\d{1,2})?$")


def _num(s):
    return float(str(s).replace(",", "").strip())


def with_amount(filename, amount):
    """票据文件名追加金额：原文件名-金额.pdf。重复调用不叠加后缀。"""
    if amount is None:
        return filename
    stem, ext = os.path.splitext(filename)
    return f"{_AMT_SUFFIX.sub('', stem)}-{amount:.2f}{ext}"


# ---------- 发票 ----------
def is_invoice(path):
    try:
        with pdfplumber.open(path) as pdf:
            return "发票" in (pdf.pages[0].extract_text() or "")
    except Exception:
        return False


def parse_invoice(path):
    """提取发票关键信息。购买方即报销主体，销售方是开票商家。"""
    with pdfplumber.open(path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    total = None
    m = re.search(r"（小写）\s*[¥￥]?\s*([\d,]+\.\d{2})", text)
    if not m:
        m = re.search(r"价税合计.*?[¥￥]\s*([\d,]+\.\d{2})", text, re.S)
    if m:
        total = _num(m.group(1))
    num = re.search(r"发票号码[:：]\s*(\w+)", text)
    dt = re.search(r"开票日期[:：]\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    # 版式为「购 名称：<购买方> 销 名称：<销售方>」，按出现顺序区分
    names = re.findall(r"名称：\s*([^\s]+?(?:公司|中心|厂|店|社|部|馆|院))", text)
    # 项目名称形如 *交通运输服务*客运服务费、*餐饮服务*餐费
    items = re.findall(r"\*([^*\n]+)\*\s*([^\s\d]{0,20})", text)
    return {
        "file": os.path.basename(path), "total": total,
        "number": num.group(1) if num else "",
        "date": (f"{dt.group(1)}-{int(dt.group(2)):02d}-{int(dt.group(3)):02d}"
                 if dt else None),
        "buyer": names[0] if names else "",
        "seller": names[1] if len(names) > 1 else "",
        "items": ["".join(i).strip() for i in items[:3]],
    }


# ---------- 辅助单据（行程单等）----------
# 汇总开票时，一张发票覆盖多笔支付。辅助单据的明细能给出每笔的开票金额，
# 从而把发票准确分摊下去；没有辅助单据时则走对话确认。
def parse_itinerary(path, year=None):
    """解析行程单。优先用表格结构（能正确还原跨行的起终点），失败则回退到文本正则。

    返回 dict: tag / file / city / rows[{n,time,amt,route,car}] / declared / total
    """
    rows, declared, city = [], None, ""
    with pdfplumber.open(path) as pdf:
        pages = [(p.extract_text() or "", p.extract_tables()) for p in pdf.pages]
    for text, tables in pages:
        d = re.search(r"共\s*(\d+)\s*笔行程，?\s*合计\s*([\d.]+)\s*元", text)
        if d:
            declared = (int(d.group(1)), float(d.group(2)))
        if year is None:
            y = re.search(r"申请日期：(\d{4})-", text)
            if y:
                year = y.group(1)
        for tb in tables:
            for r in tb:
                cells = [(c or "").replace("\n", "").strip() for c in r]
                if len(cells) < 8 or not cells[0].isdigit():
                    continue
                n, car, when, ct, start, end, _km, amt = cells[:8]
                m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}:\d{2})", when)
                if not m:
                    continue
                city = city or ct
                rows.append({"n": int(n), "car": car,
                             "time": f"{year}-{m.group(1)}-{m.group(2)} {m.group(3)}",
                             "amt": _num(amt), "route": f"{start} → {end}"})
    if not rows:                                   # 回退：纯文本解析
        for text, _t in pages:
            for ln in text.split("\n"):
                m = re.match(r"^(\d+)\s+(.*?)(\d{2})-(\d{2})\s+(\d{2}:\d{2})\s+(.*)$", ln)
                if not m:
                    continue
                nums = re.findall(r"(\d+\.\d+)", m.group(6))
                if len(nums) < 2:
                    continue
                rows.append({"n": int(m.group(1)), "car": m.group(2).strip(),
                             "time": f"{year}-{m.group(3)}-{m.group(4)} {m.group(5)}",
                             "amt": _num(nums[-1]), "route": ""})
    rows.sort(key=lambda r: r["n"])
    total = round(sum(r["amt"] for r in rows), 2)
    return {"file": os.path.basename(path), "city": city,
            "rows": rows, "declared": declared, "total": total}


def check_itinerary(it):
    """返回 (是否自洽, 说明)。页眉声明的笔数/金额必须与解析结果一致。"""
    if not it["declared"]:
        return False, "未能读到页眉的『共N笔行程，合计X元』"
    n, amt = it["declared"]
    if len(it["rows"]) != n:
        return False, f"笔数不符：解析 {len(it['rows'])} 笔，页眉声明 {n} 笔"
    if abs(it["total"] - amt) > 0.01:
        return False, f"金额不符：解析 {it['total']:.2f}，页眉声明 {amt:.2f}"
    return True, "与页眉声明一致"

# ---------- 支付截图 ----------
def ocr_images(ocr_bin, paths):
    """批量识别，返回 {文件名: [(文本, x, y, w, h)]}，坐标为 0~1 归一化、原点左上。"""
    if not paths:
        return {}
    out = subprocess.run([ocr_bin] + paths, capture_output=True, text=True).stdout
    blocks, cur = {}, None
    for ln in out.split("\n"):
        if ln.startswith("===FILE\t"):
            cur = os.path.basename(ln.split("\t", 1)[1])
            blocks[cur] = []
        elif cur is not None and ln.strip():
            parts = ln.rstrip("\n").split("\t")
            if len(parts) >= 5:
                try:
                    blocks[cur].append((parts[0], *[float(x) for x in parts[1:5]]))
                except ValueError:
                    pass
    return blocks


LABELS = {
    "merchant": ("商品说明", "商品名称", "交易对方", "收款方", "商户全称", "商户名称"),
    "pay_method": ("付款方式", "支付方式"),
    "pay_time": ("支付时间", "付款时间"),
    # 消费实际发生的时间。它决定这笔归入哪个报销周期，必须与支付时间区分开
    "occur_time": ("乘车时间", "消费时间", "交易时间", "下单时间", "服务时间"),
    "order_no": ("订单号", "商户订单号", "交易单号"),
}


def _pair_by_row(items, label_names, y_tol=0.012):
    """账单页里「标签 值」同处一行：找到标签后，取同一 y 带、位于其右侧的文本。

    Vision 的返回顺序是先一列标签、后一列值，按行序配对会错位，故按坐标配。
    """
    for text, x, y, _w, h in items:
        t = text.strip().rstrip("：:")
        if t not in label_names:
            continue
        cy = y + h / 2
        right = [(vx, vt) for vt, vx, vy, vw, vh in items
                 if vx > x + 0.02 and abs((vy + vh / 2) - cy) <= y_tol]
        if right:
            right.sort()
            return right[0][1].strip().rstrip("＞>").strip()
    return ""


def _norm_ts(v):
    """把识别到的时间串规范成 'YYYY-MM-DD HH:MM'，容忍缺失的分隔符。"""
    if not v:
        return None
    m = TS_RE.search(str(v))
    if not m:
        return None
    t = m.group(0).replace("T", " ")
    if len(t) >= 11 and t[10] != " ":          # 分隔符被吞掉，补回来
        t = t[:10] + " " + t[10:]
    return t[:16]


def read_shot(items):
    """从账单详情截图中提取付款信息。items 为带坐标的识别结果。"""
    lines = [t for t, *_ in items]
    text = "\n".join(lines)
    ts = sorted({t.replace("T", " ") for t in TS_RE.findall(text)})
    amts_neg = [_num(m.group(1)) for m in (AMT_NEG_RE.match(l.strip()) for l in lines) if m]
    amts_any = [_num(m.group(1)) for m in (AMT_ANY_RE.match(l.strip()) for l in lines) if m]
    amount = amts_neg[0] if amts_neg else (max(amts_any) if amts_any else None)

    # 优先按标签定位「乘车/消费时间」——比"取最早时间戳"可靠：
    # 自动扣款等场景下支付时间可能早于或晚于消费时间，纯比大小会取错。
    when = _norm_ts(_pair_by_row(items, LABELS["occur_time"]))
    if not when and ts:
        when = ts[0][:16]
    if not when:
        d = DATE_RE.search(text)
        when = re.sub(r"[年月]", "-", d.group(0)) if d else None

    merchant = _pair_by_row(items, LABELS["merchant"])
    if not merchant:      # 退而求其次：页面顶部的商户名（金额上方那行）
        top = [(y, t.strip().rstrip("＞>")) for t, x, y, _w, _h in items
               if 0.15 < y < 0.25 and len(t.strip()) >= 2]
        merchant = sorted(top)[0][1] if top else ""
    pay = _pair_by_row(items, LABELS["pay_method"])
    order = _pair_by_row(items, LABELS["order_no"])
    if not ORDER_RE.match(order or ""):
        order = next((l.strip() for l in lines if ORDER_RE.match(l.strip())), "")

    warn = []
    if amount is None:
        warn.append("未识别到金额")
    if not when:
        warn.append("未识别到时间")
    if not merchant:
        warn.append("未识别到商户")
    return {"time": when, "amount": amount, "merchant": merchant,
            "pay_method": pay, "order_no": order, "warnings": warn}


def to_dt(s):
    return datetime.strptime(s[:16], "%Y-%m-%d %H:%M") if len(s) > 10 \
        else datetime.strptime(s[:10], "%Y-%m-%d")
