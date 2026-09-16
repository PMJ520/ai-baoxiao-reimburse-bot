# -*- coding: utf-8 -*-
"""出表：把批次条目转成台账 xlsx 与交付清单。

沿用既有管线的纯函数（build_ledger / settle / build_text），
这里只负责把数据库里的条目喂给它们，并落到输出目录。
"""
import logging
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from .. import storage
from ..config import settings
from ..db import models as M
from ..pipeline import datasheet as DS
from ..pipeline import ledger as L
from ..pipeline import renderer as RD
from ..pipeline import manifest as MF

log = logging.getLogger(__name__)

PH_NAME = L.PH_NAME
PH_COMPANY = L.PH_COMPANY


def _rows(batch: M.Batch):
    """把条目转成管线要的结构。金额一律转 float，避免 Decimal 混入格式化。"""
    out = []
    for it in batch.items:
        if it.amount is None:
            continue                      # 缺金额的不进台账，交付清单里会单独列出
        out.append({
            "seq": it.seq,
            "date": (it.occurred_at or datetime.now()).date(),
            "amount": float(it.amount),
            "detail": it.detail or "",
            "category": it.category or "其他",
            "invoice": it.invoice.filename if it.invoice else None,
            "invoice_amount": float(it.invoice_amount)
            if it.invoice_amount is not None else None,
            "note": it.note,
            "shot": it.document.filename if it.document else None,
            "_doc": it.document,
        })
    out.sort(key=lambda r: (r["date"], r["seq"]))
    for i, r in enumerate(out, 1):
        r["seq"] = i
    return out


def _images(rows, cache_dir):
    """为每行准备嵌入图。截图完整不裁剪，凭证不宜改动外观。"""
    imgs = {}
    for r in rows:
        doc = r.get("_doc")
        if not doc or not r.get("shot"):
            continue
        src = storage.abs_path(doc.rel_path)
        if not src.exists():
            continue
        try:
            dst, _w, _h = L.make_thumb(str(src), cache_dir)
            imgs[r["shot"]] = open(dst, "rb").read()
        except Exception:
            log.warning("生成缩略图失败 %s", doc.filename, exc_info=True)
    return imgs


def missing_fields(batch: M.Batch):
    """出表前的必填检查。缺什么如实报出，不放行也不猜。"""
    miss = []
    n = sum(1 for it in batch.items if not (it.detail and it.category))
    if n:
        miss.append(f"{n} 笔还没填费用明细或类别")
    if not batch.requester or batch.requester == PH_NAME:
        miss.append("需求人")
    if not batch.handler or batch.handler == PH_NAME:
        miss.append("经办人")
    return miss


# 文件名里不能出现的字符（跨平台取并集，Windows 最严）
_BAD_CHARS = str.maketrans({c: "_" for c in '\\/:*?"<>|'})


def _safe(name, limit=60):
    return (name or "").translate(_BAD_CHARS).strip()[:limit]


def _archive(session, batch, out_dir):
    """把这一批用到的原始材料复制到交付目录，文件名带台账序号。

    **复制而不是移动**：原件按内容寻址存放且天然去重，同一份文件可能被多个
    批次引用（一张汇总发票覆盖两批支付、一份行程单跨越两个月都很常见），
    移走就会断掉别处的引用。多占一份磁盘换取交付目录自洽，值得。

    返回材料条目列表，供交付清单说明每个文件对应台账哪一行。
    """
    from ..services.batch import trip_info

    mat = out_dir / "材料"
    mat.mkdir(parents=True, exist_ok=True)

    entries = []
    inv_seqs = {}          # 发票可能覆盖多笔，先归拢再命名
    for it in sorted(batch.items, key=lambda x: x.seq):
        d = it.document
        if d:
            when = it.occurred_at.strftime("%Y-%m-%d") if it.occurred_at else "无日期"
            amt = f"{float(it.amount):.2f}" if it.amount is not None else "无金额"
            ext = Path(d.rel_path).suffix or ".bin"
            name = f"{it.seq:02d}_支付_{when}_{amt}{ext}"
            if _copy(d, mat / name):
                entries.append((name, f"第 {it.seq} 行的支付凭证"))
        if it.invoice:
            inv_seqs.setdefault(it.invoice.id, []).append(it.seq)

    seen_inv = {}
    for it in batch.items:
        d = it.invoice
        if not d or d.id in seen_inv:
            continue
        seen_inv[d.id] = True
        seqs = sorted(inv_seqs.get(d.id, []))
        tag = f"{seqs[0]:02d}" if len(seqs) == 1 else f"{seqs[0]:02d}-{seqs[-1]:02d}"
        name = f"{tag}_发票_{_safe(Path(d.filename).stem)}{Path(d.rel_path).suffix}"
        if _copy(d, mat / name):
            which = "、".join(str(x) for x in seqs)
            entries.append((name, f"第 {which} 行的发票"))

    # 行程单：整份复制，但要讲清本次只用到其中哪几趟——剩下的趟次
    # 往往归属别的批次，那边也会各自留一份副本
    trips = trip_info(session, batch)
    used = {}
    for seq, t in trips.items():
        if t.get("itinerary_doc_id"):
            used.setdefault(t["itinerary_doc_id"], []).append((seq, t.get("trip_n")))
    for doc_id, pairs in used.items():
        d = session.get(M.Document, doc_id)
        if not d:
            continue
        name = f"行程单_{_safe(Path(d.filename).stem)}{Path(d.rel_path).suffix}"
        if not _copy(d, mat / name):
            continue
        total = len((d.parsed or {}).get("rows") or [])
        ns = sorted(n for _s, n in pairs if n is not None)
        part = f"第 {'、'.join(str(n) for n in ns)} 趟" if ns else f"{len(pairs)} 趟"
        tail = f"，共 {total} 趟" if total else ""
        entries.append((name, f"本次用到其中{part}{tail}；其余趟次可能属于其它批次"))

    return entries


def _copy(doc, dest: Path):
    src = storage.abs_path(doc.rel_path)
    if not src.exists():
        log.warning("原件缺失，跳过归档：%s", doc.filename)
        return False
    shutil.copy2(src, dest)
    return True


def _prepare(batch):
    """两段出表共用的准备工作：行、抬头、输出目录、缩略图。"""
    rows = _rows(batch)
    if not rows:
        raise ValueError("批次里没有可出表的条目")
    cfg = dict(L.DEFAULTS)
    cfg["需求人"] = batch.requester or PH_NAME
    cfg["经办人"] = batch.handler or PH_NAME
    cfg["报销出账主体"] = batch.company or PH_COMPANY
    out_dir = settings.out_dir / f"batch-{batch.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / ".thumbs"
    return rows, cfg, out_dir, cache


def _invoice_list(batch):
    out, seen = [], set()
    for it in batch.items:
        d = it.invoice
        if d and d.id not in seen:
            seen.add(d.id)
            out.append((d.filename, float(d.amount) if d.amount is not None else 0.0))
    return out


def category_summary(batch):
    """按类别的笔数与金额，外加凭证口径的合计。

    确认环节只需要这些：逐笔明细在表里，对话里刷几十行反而看不清总数
    对不对得上。分类合计与总计能对上，才是核对的重点。
    """
    rows = _rows(batch)
    cats = {}
    for r in rows:
        c = r.get("category") or "未分类"
        n, amt = cats.get(c, (0, 0.0))
        cats[c] = (n + 1, round(amt + (r.get("amount") or 0), 2))
    st = MF.settle(rows, _invoice_list(batch))
    return {"categories": sorted(cats.items(), key=lambda x: -x[1][1]),
            "total": st["total"], "n_total": st["n_total"],
            "inv_ledger": st["inv_ledger"], "sub_total": st["sub_total"],
            "n_sub": st["n_sub"]}


def build_datasheet(session, batch: M.Batch):
    """第一段：只出通用数据表。

    它是两层之间的契约，也是给人复核的底稿。台账要等这份核对无误之后
    才做——先前是一步到底，用户看到明细表时台账早已生成，发现错了只能
    整批重来。
    """
    rows, _cfg, out_dir, cache = _prepare(batch)
    images = _images(rows, str(cache))
    sheet = out_dir / "报销明细表.xlsx"
    st = MF.settle(rows, _invoice_list(batch))
    DS.build(_datasheet_rows(rows), images, st, str(sheet), cache_dir=str(cache))
    batch.datasheet_path = str(sheet)
    session.commit()
    return str(sheet)


def build_ledger(session, batch: M.Batch, out_name="费用报销台账.xlsx"):
    """第二段：出专用台账、归集材料、写交付清单。

    返回 (台账路径, 清单路径, 清单文本)。
    """
    rows, cfg, out_dir, cache = _prepare(batch)
    images = _images(rows, str(cache))
    xlsx = out_dir / out_name
    _render_ledger(session, batch, rows, images, cfg, str(xlsx), str(cache))

    invoices = _invoice_list(batch)
    st = MF.settle(rows, invoices)
    todo = [f for f in ("需求人", "经办人", "报销出账主体")
            if cfg[f] in (PH_NAME, PH_COMPANY)]
    text = MF.build_text(rows, st, cfg, out_name, todo_fields=todo,
                         n_docs=len(invoices))

    entries = _archive(session, batch, out_dir)
    if entries:
        text += "\n\n" + MF.materials_text(entries)
    manifest = out_dir / "交付清单.txt"
    manifest.write_text(text, encoding="utf-8")

    batch.ledger_path = str(xlsx)
    batch.manifest_path = str(manifest)
    batch.status = M.B_DONE
    batch.finalized_at = datetime.now()
    session.commit()
    return str(xlsx), str(manifest), text


def build(session, batch: M.Batch, out_name="费用报销台账.xlsx"):
    """两段一起做。后台里手工触发出表用，对话流程走分开的两段。"""
    sheet = build_datasheet(session, batch)
    xlsx, manifest, text = build_ledger(session, batch, out_name)
    return xlsx, manifest, sheet, text


def _datasheet_rows(rows):
    """把管线行转成通用数据表的行。

    键名必须与 template_spec.SOURCE_FIELDS 一致——那是两层之间的字段契约，
    专用模版的映射全靠它。对不上就会渲染出空表。
    """
    from ..pipeline.template_spec import SOURCE_FIELDS

    out = []
    for i, r in enumerate(rows, 1):
        inv = r.get("invoice_amount")
        diff = round(r["amount"] - inv, 2) if inv is not None else round(r["amount"], 2)
        out.append({
            "seq": r.get("seq", i),
            "date": r["date"],                       # 保留 date 对象，供日期格式化
            "amount": r["amount"],
            "detail": r.get("detail"),
            "category": r.get("category"),
            "invoice": r.get("invoice"),
            "invoice_amount": inv,
            "diff": diff or None,
            "voucher": ("替票" if inv is None
                        else ("发票+替票" if diff > 0 else "发票")),
            "shot": r.get("shot"),
        })
    # 契约自检：漏字段会导致专用模版渲染出空列，早发现早排查
    missing = set(SOURCE_FIELDS) - set(out[0]) - {"requester", "handler",
                                                  "company", "apply_date"} if out else set()
    if missing:
        log.warning("通用数据表缺少契约字段：%s", "、".join(sorted(missing)))
    return out


def _render_ledger(session, batch, rows, images, cfg, out_path, cache_dir):
    """生成专用台账。

    有已启用的自定义模版就按 spec 渲染；没有则回退到内置的备用金台账实现，
    保证没配模版的用户照样能出表。
    """
    from . import templates as TPL

    spec, tpl = TPL.get_spec(session, batch.template_id)
    if not spec:
        L.build_ledger(rows, images, cfg, out_path)
        return "builtin"

    ctx = {"requester": cfg["需求人"], "handler": cfg["经办人"],
           "company": cfg["报销出账主体"],
           "apply_date": cfg.get("发起报销日期") or date.today().strftime("%Y.%m.%d")}
    RD.render(spec, _datasheet_rows(rows), ctx, out_path,
              images=images, cache_dir=cache_dir)
    if tpl and not batch.template_id:
        batch.template_id = tpl.id
    return tpl.name if tpl else "custom"
