# -*- coding: utf-8 -*-
"""按备用金台账模板生成台账（纯函数：数据进，xlsx 出）。


读 <批次>/.work/entries.json（由对话确认后写入），每条含：
    shot / date / amount / detail / category / invoice / invoice_amount / note
截图从 <批次>/source/ 取原图，嵌入台账的「付款截图」页（WPS DISPIMG）。
"""
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
from openpyxl.drawing.image import Image as XLImage

from .parsers import DOCS_DIR, with_amount  # noqa: F401  供服务层复用

# 未提供时的占位值：宁可留下醒目的待补记号，也不猜一个可能错的名字
PH_NAME = "请补充姓名"
PH_COMPANY = "请补充公司名"

# 台账固定字段。可用 --config 或命令行覆盖。
DEFAULTS = {
    "标题": "费用报销-台账",
    "需求部门": "mx",
    "需求人": PH_NAME,          # 运行时传入，见 --requester
    "是否通过采购流程": "否",
    "数量": 1,
    "规格": "次",
    "费用类别": "交通费",
    "经办人": PH_NAME,          # 运行时传入，见 --handler（可与需求人不同）
    "发起报销日期": None,          # None = 运行当天，格式 YYYY.MM.DD
    "报销出账主体": PH_COMPANY,  # 运行时传入，见 --company
    "是否提供支付截图": "是",
    "替票备注模板": "{diff:g}元替票",
    "每行图片数": 20,
}

COLS = ["序号", "费用发生日期", "费用金额", "需求部门", "需求人", "是否通过采购流程",
        "费用具体明细", "数量", "规格", "费用类别", "经办人", "备用金转入", "备注",
        "发起报销日期", "报销出账主体", "是否提供支付截图", "提供发票or替票", "打款时间"]
WIDTHS = [6.9, 18.5, 11.1, 13.9, 12.5, 8.6, 48.4, 4.6, 9, 9.7, 13.3, 11.9, 18.7,
          12.7, 13.8, 11.9, 12.7, 11.0, 12.2]

F = "宋体"
_thin = Side(style="thin", color="000000")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)


def build_ledger(rows, images, cfg, out_path):
    n_img = cfg["每行图片数"]
    apply_date = cfg["发起报销日期"] or date.today().strftime("%Y.%m.%d")

    wb = Workbook()
    ws = wb.active
    ws.title = "备用金台账"

    ws["A1"] = cfg["标题"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=19)
    ws["A1"].font = Font(name=F, bold=True, size=16)
    ws["A1"].alignment = CENTER
    ws.row_dimensions[1].height = 30

    ws.merge_cells("A2:A3")
    ws["A2"] = "序号"
    ws.merge_cells("C2:M2"); ws["C2"] = "费用使用情况"
    ws.merge_cells("N2:S2"); ws["N2"] = "报销情况"
    for c, name in enumerate(COLS[1:], start=2):
        ws.cell(row=3, column=c, value=name)
    for r in (2, 3):
        for c in range(1, 20):
            cell = ws.cell(row=r, column=c)
            cell.font = Font(name=F, bold=True, size=10)
            cell.alignment = CENTER
            cell.border = BORDER
            cell.fill = PatternFill("solid", start_color="D9D9D9")
        ws.row_dimensions[r].height = 22
    for i, w in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A4"

    body = Font(name=F, size=10)
    first = 4
    for i, r in enumerate(rows):
        n = first + i
        inv_amt = r.get("invoice_amount")
        diff = round(r["amount"] - inv_amt, 2) if inv_amt is not None else 0.0
        if not r.get("invoice"):
            voucher, note = "替票", r.get("note")          # 无发票，全额需替票
        elif diff > 0:
            voucher = "发票+替票"
            note = r.get("note") or cfg["替票备注模板"].format(diff=diff)
        else:
            voucher, note = "发票", r.get("note")
        vals = [r["seq"], r["date"], r["amount"], cfg["需求部门"], cfg["需求人"],
                cfg["是否通过采购流程"], r["detail"],
                cfg["数量"], cfg["规格"],
                r["category"], cfg["经办人"], None, note,
                apply_date, cfg["报销出账主体"], cfg["是否提供支付截图"], voucher, None]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=n, column=c, value=v)
            cell.font = body
            cell.border = BORDER
            cell.alignment = LEFT if c == 7 else CENTER
            if c == 2:
                cell.number_format = "yyyy/m/d"
            elif c == 3:
                cell.number_format = "#,##0.00"
        ws.row_dimensions[n].height = 20
    last = first + len(rows) - 1
    tot = last + 1
    ws.merge_cells(start_row=tot, start_column=1, end_row=tot, end_column=2)
    ws.cell(row=tot, column=1, value="合计").font = Font(name=F, bold=True, size=10)
    c3 = ws.cell(row=tot, column=3, value=f"=SUM(C{first}:C{last})")
    c3.font = Font(name=F, bold=True, size=10); c3.number_format = "#,##0.00"
    ws.merge_cells(start_row=tot, start_column=7, end_row=tot, end_column=11)
    for c in range(1, 20):
        ws.cell(row=tot, column=c).border = BORDER
        ws.cell(row=tot, column=c).alignment = CENTER
    ws.row_dimensions[tot].height = 22

    # ---- 付款截图：DISPIMG 网格（图片行 + 金额行 交替）----
    ws2 = wb.create_sheet("付款截图")
    for c in range(1, n_img + 1):
        ws2.column_dimensions[get_column_letter(c)].width = 18.775
    ids = []
    for i, r in enumerate(rows):
        band, col = divmod(i, n_img)
        img_row, amt_row = band * 2 + 1, band * 2 + 2
        if r.get("shot") and r["shot"] in images:
            iid = "ID_" + uuid.uuid4().hex.upper()
            ids.append((iid, images[r["shot"]]))
            ws2.cell(row=img_row, column=col + 1,
                     value=f'=_xlfn.DISPIMG("{iid}",1)')
        a = ws2.cell(row=amt_row, column=col + 1, value=r["amount"])
        a.number_format = "#,##0.00"
        a.alignment = CENTER
        a.font = Font(name=F, size=10)
        ws2.row_dimensions[img_row].height = 231
        ws2.row_dimensions[amt_row].height = 18

    wb.create_sheet("发票")          # 按约定留空，发票 PDF 由人工拖入

    wb.save(out_path)
    if ids:
        inject_cellimages(out_path, ids)
    return out_path, len(ids)


DISPIMG_CELL = re.compile(
    r'<c r="([A-Z]+\d+)"((?:(?!/>)[^>])*)>'
    r'<f>_xlfn\.DISPIMG\("(ID_[0-9A-F]+)",1\)</f>'
    r'\s*<v\s*/>\s*</c>')


def fix_dispimg_cells(data):
    """WPS 需要 t="str" 与缓存值才会在未重算时显示图片，openpyxl 不会写缓存值。"""
    s = data.decode("utf-8")
    if "DISPIMG" not in s:
        return data

    def rep(m):
        ref, attrs, iid = m.group(1), m.group(2), m.group(3)
        if ' t="' not in attrs:
            attrs += ' t="str"'
        return (f'<c r="{ref}"{attrs}>'
                f'<f>_xlfn.DISPIMG(&quot;{iid}&quot;,1)</f>'
                f'<v>=DISPIMG(&quot;{iid}&quot;,1)</v></c>')

    return DISPIMG_CELL.sub(rep, s).encode("utf-8")


def inject_cellimages(path, ids):
    """在 zip 层写入 WPS 的 cellimages 部件，使 DISPIMG 公式能显示图片。"""
    NS = ('xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"')
    imgs, rels, media = [], [], []
    for i, (iid, blob) in enumerate(ids, start=1):
        name = f"image{i}.jpeg"
        media.append((f"xl/media/{name}", blob))
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org'
                    f'/officeDocument/2006/relationships/image" Target="media/{name}"/>')
        imgs.append(
            f'<etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{i+1}" name="{iid}" '
            f'descr="Picture"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
            f'<a:blip r:embed="rId{i}" cstate="print"/><a:stretch><a:fillRect/></a:stretch>'
            f'</xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="2343150" cy="5105400"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic></etc:cellImage>')
    cellimages = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                  f'<etc:cellImages {NS}>' + "".join(imgs) + "</etc:cellImages>")
    cellrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships">' + "".join(rels) + "</Relationships>")

    tmp = path + ".tmp"
    zin = zipfile.ZipFile(path)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                data = fix_dispimg_cells(data)
            if item.filename == "[Content_Types].xml":
                s = data.decode("utf-8")
                if "cellimage+xml" not in s:
                    s = s.replace("</Types>",
                                  '<Override PartName="/xl/cellimages.xml" ContentType='
                                  '"application/vnd.wps-officedocument.cellimage+xml"/></Types>')
                if 'Extension="jpeg"' not in s:
                    s = s.replace("</Types>",
                                  '<Default Extension="jpeg" ContentType="image/jpeg"/></Types>')
                data = s.encode("utf-8")
            elif item.filename == "xl/_rels/workbook.xml.rels":
                s = data.decode("utf-8")
                used = [int(x) for x in re.findall(r'Id="rId(\d+)"', s)]
                nid = max(used) + 1 if used else 1
                s = s.replace("</Relationships>",
                              f'<Relationship Id="rId{nid}" Type="http://www.wps.cn'
                              f'/officeDocument/2020/cellImage" Target="cellimages.xml"/>'
                              "</Relationships>")
                data = s.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("xl/cellimages.xml", cellimages)
        zout.writestr("xl/_rels/cellimages.xml.rels", cellrels)
        for name, blob in media:
            zout.writestr(name, blob)
    zin.close()
    shutil.move(tmp, path)



EMBED_W, SHOW_W_MAX, SHOW_H_MAX = 620, 300, 536


def make_thumb(src_path, cache_dir):
    """截图完整保留、不裁剪（凭证不宜改动外观）。

    内部分辨率高于显示尺寸，Excel 保留原始像素，故版面紧凑而放大/打印仍清晰。
    """
    os.makedirs(cache_dir, exist_ok=True)
    im = PILImage.open(src_path).convert("RGB")
    ratio = im.height / im.width
    dst = os.path.join(cache_dir,
                       os.path.splitext(os.path.basename(src_path))[0] + "_t.jpg")
    im.resize((EMBED_W, int(EMBED_W * ratio)), PILImage.LANCZOS).save(
        dst, "JPEG", quality=88)
    sw, sh = SHOW_W_MAX, SHOW_W_MAX * ratio
    if sh > SHOW_H_MAX:
        sh, sw = SHOW_H_MAX, SHOW_H_MAX / ratio
    return dst, int(sw), int(sh)
