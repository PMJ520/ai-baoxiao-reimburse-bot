# -*- coding: utf-8 -*-
"""结算汇总与《交付清单.txt》。纯文本、面向普通阅读者。"""
import os
import unicodedata
from collections import defaultdict
from datetime import date

from .parsers import DOCS_DIR  # noqa: F401

LINE = "=" * 64
SUB = "-" * 64
LBL_W, AMT_W = 34, 12


def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, w):
    return str(s) + " " * max(0, w - _w(s))


def rpad(s, w):
    return " " * max(0, w - _w(s)) + str(s)


def money(v):
    return f"{v:,.2f}"


def _row(label, amount, count=None):
    line = pad(label, LBL_W) + rpad(money(amount), AMT_W) + " 元"
    if count is not None:
        line += f"     共 {count:>2} 笔"
    return line


def settle(rows, invoices):
    """按凭证性质拆分。替票为纸质凭证、不在材料内，只报台账应有金额。"""
    total = round(sum(r["amount"] for r in rows), 2)
    inv_ledger = round(sum(r.get("invoice_amount") or 0 for r in rows), 2)
    actual_inv = round(sum(a for _f, a in invoices), 2)
    sub_total = round(total - inv_ledger, 2)

    need = defaultdict(int)
    for r in rows:
        gap = round(r["amount"] - (r.get("invoice_amount") or 0), 2)
        if gap > 0:
            need[(gap, "无发票" if not r.get("invoice") else "发票差额")] += 1
    return {
        "total": total, "n_total": len(rows),
        "inv_ledger": inv_ledger, "actual_inv": actual_inv,
        "missing_inv": round(inv_ledger - actual_inv, 2),
        "n_inv": sum(1 for r in rows if r.get("invoice")),
        "sub_total": sub_total,
        "n_sub": sum(1 for r in rows
                     if round(r["amount"] - (r.get("invoice_amount") or 0), 2) > 0),
        "need": sorted(need.items(), key=lambda x: -x[0][0]),
        "invoices": invoices,
        "by_cat": sorted(_by_cat(rows).items(), key=lambda x: -x[1][1]),
    }


def _by_cat(rows):
    g = defaultdict(lambda: [0, 0.0])
    for r in rows:
        g[r["category"]][0] += 1
        g[r["category"]][1] = round(g[r["category"]][1] + r["amount"], 2)
    return g


def build_text(rows, s, cfg, ledger_name, excluded=(), todo_fields=(), n_docs=0):
    dates = sorted(r["date"] for r in rows)

    L = [LINE, "                    费用报销 · 交付清单", LINE, "",
         f"  批次        {dates[0]} ~ {dates[-1]}",
         f"  生成日期    {date.today()}",
         f"  出账主体    {cfg['报销出账主体']}",
         f"  需求人      {cfg['需求人']}",
         f"  经办人      {cfg['经办人']}", "", "",
         "一、报销金额", SUB,
         _row("  报销总额", s["total"], s["n_total"]), "",
         _row("    其中 · 有发票部分", s["inv_ledger"], s["n_inv"]),
         _row("         · 需替票部分", s["sub_total"], s["n_sub"]),
         "", "  按费用类别"]
    for cat, (n, amt) in s["by_cat"]:
        L.append(_row(f"    {cat}", amt, n))

    L += ["", "", "二、发票情况", SUB,
          _row("  台账应开发票金额", s["inv_ledger"]),
          _row("  实际发票金额", s["actual_inv"]),
          _row("  缺少发票金额", max(s["missing_inv"], 0))
          + ("    无缺口" if s["missing_inv"] <= 0.01 else "    需补开发票")]
    if s["invoices"]:
        L += ["", "  发票明细"]
        for f, a in s["invoices"]:
            L.append(pad("    " + f, LBL_W) + rpad(money(a), AMT_W) + " 元")

    L += ["", "", "三、待办事项", SUB]
    if todo_fields:
        L.append("  [ ] 补填 " + "、".join(todo_fields))
        L.append("        台账中当前为占位文字，请改成实际内容后再交财务")
        L.append("")
    if s["sub_total"] > 0:
        L.append(f"  [ ] 需补替票 {money(s['sub_total'])} 元")
        for (amt, kind), n in s["need"]:
            L.append("      " + rpad(money(amt), 10) + f" 元 x {n} 张   {kind}")
        L.append("")
    if s["invoices"]:
        L.append("  [ ] 发票 PDF 入表")
        L.append(f"        把下面 {len(s['invoices'])} 个文件拖入台账的「发票」工作表")
        for f, _a in s["invoices"]:
            L.append(f"          data/{DOCS_DIR}/{f}")
        L.append("")
    if s["missing_inv"] > 0.01:
        L.append(f"  [ ] 补开发票 {money(s['missing_inv'])} 元")
    else:
        L.append("  [完成] 发票金额无缺口")
    if excluded:
        L.append(f"  [已排除] {len(excluded)} 笔未纳入本次报销")
        for e in excluded:
            L.append(f"        {e.get('shot','')}  {e.get('reason','')}")

    L += ["", "", "四、交付文件", SUB,
          f"  台账        data/{ledger_name}",
          f"  发票        data/{DOCS_DIR}/    {n_docs} 个 PDF",
          f"  支付截图    data/支付截图/      {s['n_total']} 张", "", LINE, ""]
    return "\n".join(L)


def materials_text(entries):
    """交付目录里的材料清单。

    每份材料都注明对应台账哪一行，财务核对时不用在文件名和表格之间来回猜。
    """
    out = ["材料（见 材料/ 目录，共 %d 份）" % len(entries), "-" * 32]
    for name, why in entries:
        out.append(f"· {name}")
        out.append(f"    {why}")
    out.append("")
    out.append("原件同时保留在系统内，此处为副本；重复出表会覆盖本目录。")
    return "\n".join(out)
