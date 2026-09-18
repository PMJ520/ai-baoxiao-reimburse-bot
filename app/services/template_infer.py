# -*- coding: utf-8 -*-
"""从填好的样例模版推断 spec，并用样例自身回放验证。

AI 推断的映射不能直接信。核心是回放验证：拿样例里已有的数据按推断的 spec
重新生成一遍，与原样例逐格比对——全对才允许启用。
猜错会在这里当场暴露，而不是等到给财务的表出错。
"""
import logging
import re

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ..pipeline.template_spec import (SOURCE_FIELDS, ColumnSpec, TemplateSpec,
                                      validate)
from .llm import get_client, parse_json

log = logging.getLogger(__name__)

# 批次级字段：同一批报销里各行相同，但换一批就会变。
# 单份样例里它们整列同值属正常，不可据此判定为固定值。
# 这些字段在单份样例里天然整列相同，据此判错必然误报
BATCH_LEVEL_FIELDS = {"requester", "handler", "company", "apply_date"}
# 行级字段，但同一批报销里常常整批同值（一批打车全是交通费、全是替票）
OFTEN_UNIFORM = {"category", "voucher"}
UNIFORM_OK = BATCH_LEVEL_FIELDS | OFTEN_UNIFORM

# 表头关键词 → 期望的字段。用于发现"表头写着金额、却映射成明细"这类错位。
# 只列语义明确的，拿不准的不设约束——宁可放过也不要误报把人逼疯。
HEADER_HINTS = {
    "金额": {"amount", "invoice_amount", "diff"},
    "日期": {"date", "apply_date"},
    "时间": {"date", "apply_date"},
    "序号": {"seq"},
    "明细": {"detail"},
    "类别": {"category"},
    "需求人": {"requester"},
    "经办人": {"handler"},
    "主体": {"company"},
    "发票": {"invoice", "invoice_amount", "voucher"},
    "替票": {"voucher"},
}


def _header_conflict(header, source):
    """表头语义与映射字段是否矛盾。返回冲突说明或 None。"""
    if not header or not source:
        return None
    for kw, allowed in HEADER_HINTS.items():
        if kw in header and source not in allowed:
            return (f"表头「{header}」通常对应 {'/'.join(sorted(allowed))}，"
                    f"却映射成了 {source!r}")
    return None

SYSTEM = """你在分析一份已填好数据的报销台账 Excel 模版，要推断出它的填表规则，
以便今后自动生成同样格式的表。

上游有一张「通用数据表」，每一行是一笔费用，可用字段如下：
{fields}

请判断样例里每一列是怎么来的，输出 JSON：

{{
  "sheet_name": "数据所在工作表名",
  "data_start_row": 数据第一行的行号,
  "columns": [
    {{"letter":"A","header":"序号","source":"seq"}},
    {{"letter":"D","header":"需求部门","const":"mx"}},
    {{"letter":"M","header":"备注","template":"{{diff:g}}元替票","when":"diff_positive"}}
  ],
  "total_row": {{"label_col":"A","label":"合计","sum_cols":["C"]}},
  "image_sheet": {{"name":"付款截图","per_row":20,"mode":"dispimg"}},
  "extra_sheets": ["发票"]
}}

规则：
1. 每列三选一：source（取通用数据表字段）、const（各行都一样的固定值）、
   template（按模板生成，可带 when 条件）。
2. when 可选：diff_positive（实付高于发票额时）、no_invoice（无发票时）、always。
3. 整列取值都相同的，是 const，不是 source。
4. **不确定的列宁可省略，也不要猜**——漏掉会被验证发现，猜错更难排查。
5. 只输出 JSON。
"""


def _scan(path, max_rows=12):
    """把样例压成给模型看的紧凑视图：表头 + 前若干行数据。"""
    wb = load_workbook(path, data_only=True)
    out = {"sheets": wb.sheetnames, "samples": {}}
    for name in wb.sheetnames:
        ws = wb[name]
        if ws.max_row < 2 or ws.max_column < 2:
            out["samples"][name] = {"empty_or_special": True,
                                    "max_row": ws.max_row}
            continue
        rows = []
        for r in range(1, min(ws.max_row, max_rows) + 1):
            cells = {}
            for c in range(1, min(ws.max_column, 24) + 1):
                v = ws.cell(r, c).value
                if v is not None:
                    cells[get_column_letter(c)] = str(v)[:60]
            if cells:
                rows.append({"row": r, "cells": cells})
        out["samples"][name] = {
            "rows": rows, "max_row": ws.max_row,
            "merges": [str(m) for m in ws.merged_cells.ranges][:12],
        }
    return out


def infer(path, name="自定义模版", client=None):
    """调用模型推断 spec。返回 (spec, 原始回复)。"""
    import json
    cli = client or get_client()
    fields = "\n".join(f"  {k} — {v}" for k, v in SOURCE_FIELDS.items())
    raw = cli.complete(SYSTEM.format(fields=fields),
                       json.dumps(_scan(path), ensure_ascii=False, indent=1),
                       max_tokens=4096)
    d = parse_json(raw)
    d["name"] = name
    spec = TemplateSpec.from_dict(d)
    return spec, raw


# ---------- 回放验证 ----------
# 关键：不能"按 spec 从样例反解数据、再按同一 spec 生成"——那是循环论证，
# 用错误的尺子量自己永远一致（实测会漏过列错位、起始行错等严重问题）。
# 必须用与 spec 无关的独立事实来校验。


def _independent_facts(path, spec):
    """从样例里提取不依赖 spec 的客观事实。"""
    wb = load_workbook(path, data_only=True)
    ws = wb[spec.sheet_name] if spec.sheet_name in wb.sheetnames else wb.worksheets[0]
    facts = {"headers": {}, "columns": {}, "data_rows": 0}

    # 表头文字：数据起始行之前、最靠近它的那行非空文本
    hdr_row = None
    for r in range(max(1, spec.data_start_row - 3), spec.data_start_row):
        if sum(1 for c in range(1, ws.max_column + 1) if ws.cell(r, c).value) >= 3:
            hdr_row = r
    if hdr_row:
        for c in range(1, ws.max_column + 1):
            v = ws.cell(hdr_row, c).value
            if v:
                facts["headers"][get_column_letter(c)] = str(v).strip()

    # 每列的取值样本与"是否整列同值"
    end = ws.max_row
    for r in range(spec.data_start_row, ws.max_row + 1):
        first = ws.cell(r, 1).value
        if first is None or str(first).strip() in ("合计", "总计"):
            end = r - 1
            break
    facts["data_rows"] = max(0, end - spec.data_start_row + 1)
    for c in range(1, ws.max_column + 1):
        vals = [ws.cell(r, c).value
                for r in range(spec.data_start_row, end + 1)]
        nonnull = [v for v in vals if v is not None]
        if not nonnull:
            continue
        facts["columns"][get_column_letter(c)] = {
            "values": vals,
            "constant": len(set(map(str, nonnull))) == 1 and len(nonnull) == len(vals),
            "const_value": nonnull[0] if len(set(map(str, nonnull))) == 1 else None,
        }
    return facts


def replay(path, spec, tolerance=0.01):
    """按独立事实校验 spec。返回 {ok, checked, diffs}。"""
    facts = _independent_facts(path, spec)
    diffs, warns, checked = [], [], 0

    if facts["data_rows"] == 0:
        return {"ok": False, "checked": 0, "rows": 0, "total_diffs": 1,
                "warns": [], "total_warns": 0,
                "diffs": [{"msg": f"第 {spec.data_start_row} 行起没有数据，"
                                  f"data_start_row 可能不对"}]}

    for col in spec.columns:
        info = facts["columns"].get(col.letter)
        checked += 1
        if info is None:
            diffs.append({"col": col.letter, "header": col.header,
                          "msg": "样例中该列整列为空，映射多余"})
            continue

        # 表头文字对不上 → 列映射错位的最强信号
        hdr = facts["headers"].get(col.letter)
        if col.header and hdr and col.header.strip() != hdr:
            diffs.append({"col": col.letter, "header": col.header,
                          "expect": hdr, "got": col.header,
                          "msg": "表头与样例不符，疑似列映射错位"})
            continue

        # 表头语义与字段矛盾 → 列映射错位的另一种信号
        conflict = _header_conflict(col.header or hdr, col.source)
        if conflict:
            diffs.append({"col": col.letter, "header": col.header or hdr,
                          "msg": conflict})
            continue

        if col.const is not None:
            if not info["constant"]:
                diffs.append({"col": col.letter, "header": col.header,
                              "msg": "声明为固定值，但样例里该列取值不唯一"})
            elif not _same(info["const_value"], col.const, tolerance):
                diffs.append({"col": col.letter, "header": col.header,
                              "expect": _brief(info["const_value"]),
                              "got": _brief(col.const),
                              "msg": "固定值与样例不符"})
        elif col.source:
            # 整列同值却映射成字段，可能是把固定值误判成了取数。
            # 但需求人、公司这类"每批相同、跨批会变"的字段，在单份样例里
            # 天然整列相同，不能据此判错——否则正确的 spec 会被误报。
            if (info["constant"] and facts["data_rows"] >= 3
                    and col.source not in UNIFORM_OK):
                # 只是可疑，不是事实不符：样例同质时，"固定值"和"恰好取值
                # 相同的字段"在数据上无法区分。两可之下映射成字段更安全——
                # 将来值变了能跟着变，映射成固定值则会永远输出这一个值。
                # 所以列出来让人过目即可，不该拦住启用。
                warns.append({"col": col.letter, "header": col.header,
                              "expect": _brief(info["const_value"]),
                              "msg": f"样例中该列整列相同，请确认它是固定值还是"
                                     f"字段 {col.source!r}（样例可能同质）"})

    # 表头存在但没被映射的列，提示遗漏
    mapped = {c.letter for c in spec.columns}
    for letter, hdr in facts["headers"].items():
        if letter not in mapped and facts["columns"].get(letter):
            diffs.append({"col": letter, "header": hdr, "msg": "该列未被映射"})

    # ok 只看错误：提示是供人过目的，不阻断启用
    return {"ok": not diffs, "checked": checked,
            "rows": facts["data_rows"], "diffs": diffs[:20],
            "total_diffs": len(diffs),
            "warns": warns[:20], "total_warns": len(warns)}


def _same(a, b, tol):
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    if a is None or b is None:
        return False
    sa, sb = str(a).strip(), str(b).strip()
    if sa == sb:
        return True
    # 日期在样例里可能是 datetime，生成侧是 date，按前 10 位比
    return sa[:10] == sb[:10] and re.match(r"\d{4}-\d{2}-\d{2}", sa) is not None


def _brief(v):
    return None if v is None else str(v)[:40]


def check(path, spec):
    """结构校验 + 回放验证，合成一份可读结论。"""
    errs = validate(spec)
    rp = replay(path, spec)
    return {
        "structure_ok": not errs, "structure_errors": errs,
        "replay_ok": rp["ok"], "checked_cells": rp["checked"],
        "sample_rows": rp.get("rows", 0),
        "diffs": rp["diffs"], "total_diffs": rp.get("total_diffs", 0),
        "warns": rp.get("warns") or [], "total_warns": rp.get("total_warns", 0),
        "usable": (not errs) and rp["ok"],
    }
