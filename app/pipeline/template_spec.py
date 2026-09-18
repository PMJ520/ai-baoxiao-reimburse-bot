# -*- coding: utf-8 -*-
"""专用模版规格。

模版的怪癖不止"哪列填什么"：两行合并表头、合计行公式、固定值列、
WPS 的 DISPIMG 图片页……spec 必须能把这些都表达出来，
否则换一家公司的模版还得改代码。

spec 由 AI 从填好的样例里推断，人确认后固化；运行时纯代码套用，不碰模型。
"""
from dataclasses import dataclass, field

# 通用数据表能提供的字段，AI 只能从这里选来映射
SOURCE_FIELDS = {
    "seq": "序号",
    "date": "消费日期",
    "amount": "实付金额",
    "detail": "费用具体明细",
    "category": "费用类别",
    "invoice": "发票文件名",
    "invoice_amount": "发票金额",
    "diff": "实付与发票的差额",
    "voucher": "凭证类型（发票/发票+替票/替票）",
    "shot": "支付截图文件名",
    "requester": "需求人",
    "handler": "经办人",
    "company": "报销出账主体",
    "apply_date": "发起报销日期",
    "note": "备注（差额需替票时自动生成，否则为空）",
}

VERSION = 1


@dataclass
class ColumnSpec:
    """一列的取数规则。三选一：取字段 / 填固定值 / 按表达式生成。"""
    letter: str                      # 列号，如 "B"
    header: str = ""                 # 表头文字，仅作核对与可读性
    source: str | None = None        # SOURCE_FIELDS 里的键
    const: object | None = None      # 固定值，如 数量=1、规格="次"
    template: str | None = None      # 形如 "{diff:g}元替票"，仅在条件成立时填
    when: str | None = None          # 条件：diff_positive / no_invoice / always
    number_format: str | None = None
    width: float | None = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class TemplateSpec:
    """一份专用模版的完整规格。"""
    name: str
    sheet_name: str = "Sheet1"
    header_rows: list[dict] = field(default_factory=list)   # 表头区，含合并信息
    data_start_row: int = 2
    columns: list[ColumnSpec] = field(default_factory=list)
    total_row: dict | None = None        # {"label_col":"A","label":"合计","sum_cols":["C"]}
    image_sheet: dict | None = None      # {"name":"付款截图","per_row":20,"mode":"dispimg"}
    extra_sheets: list[str] = field(default_factory=list)    # 需要但留空的页，如"发票"
    version: int = VERSION

    def to_dict(self):
        return {
            "name": self.name, "sheet_name": self.sheet_name,
            "header_rows": self.header_rows,
            "data_start_row": self.data_start_row,
            "columns": [c.to_dict() for c in self.columns],
            "total_row": self.total_row, "image_sheet": self.image_sheet,
            "extra_sheets": self.extra_sheets, "version": self.version,
        }

    @classmethod
    def from_dict(cls, d):
        cols = [ColumnSpec(**c) for c in d.get("columns", [])]
        return cls(
            name=d.get("name", ""), sheet_name=d.get("sheet_name", "Sheet1"),
            header_rows=d.get("header_rows", []),
            data_start_row=d.get("data_start_row", 2), columns=cols,
            total_row=d.get("total_row"), image_sheet=d.get("image_sheet"),
            extra_sheets=d.get("extra_sheets", []),
            version=d.get("version", VERSION))


def validate(spec: TemplateSpec):
    """结构校验。返回问题列表，空表示通过。

    只查能机械判定的：列号合法、source 在白名单内、取数方式唯一。
    映射对不对要靠样例回放验证，那才是真正的闸门。
    """
    errs = []
    seen = set()
    for c in spec.columns:
        if not c.letter or not c.letter.isalpha():
            errs.append(f"列号不合法：{c.letter!r}")
        if c.letter in seen:
            errs.append(f"列 {c.letter} 重复定义")
        seen.add(c.letter)
        ways = sum(x is not None for x in (c.source, c.const, c.template))
        # 三者皆无是合法的：台账里常有我们没有对应数据的格子（打款时间、
        # 备用金转入等），保留列、内容留空，比整列不输出更贴近原表
        if ways > 1:
            errs.append(f"列 {c.letter} 同时指定了多种取数方式")
        if c.source and c.source not in SOURCE_FIELDS:
            errs.append(f"列 {c.letter} 的字段 {c.source!r} 不在通用数据表里")
    if spec.data_start_row < 2:
        errs.append("数据起始行不能小于 2")
    return errs
