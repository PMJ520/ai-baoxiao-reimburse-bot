# -*- coding: utf-8 -*-
"""文字识别引擎，三级自动降级。

1. macOS Vision —— 最快，零额外依赖，仅 macOS
2. RapidOCR   —— 跨平台，需 pip install rapidocr-onnxruntime（约 246MB）
3. 交给模型读图 —— 零依赖兜底：导出待读清单，由 AI 看图填写

三级都受同一道保险：识别结果须与发票金额交叉验证，读错会在核对环节暴露。
"""
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VISION_SRC = os.path.join(HERE, "vision.swift")
IS_MAC = platform.system() == "Darwin"


def _cache_dir():
    """OCR 二进制的缓存位置。容器内 app/ 可能只读，故允许用环境变量指定。"""
    d = os.environ.get("OCR_CACHE_DIR") or os.path.join(os.path.dirname(HERE), ".cache")
    os.makedirs(d, exist_ok=True)
    return d


def vision_binary(build=True):
    """返回可用的 Vision 二进制路径；不可用时返回 None。"""
    if not IS_MAC or not os.path.exists(VISION_SRC):
        return None
    binp = os.path.join(_cache_dir(), "vision_ocr")
    if os.path.exists(binp) and os.path.getmtime(binp) >= os.path.getmtime(VISION_SRC):
        return binp
    if not build or not shutil.which("swiftc"):
        return None
    r = subprocess.run(["swiftc", "-O", VISION_SRC, "-o", binp],
                       capture_output=True, text=True)
    return binp if r.returncode == 0 and os.path.exists(binp) else None


def has_rapidocr():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def available():
    """返回当前可用的引擎名：vision / rapidocr / manual。"""
    if vision_binary(build=False) or (IS_MAC and shutil.which("swiftc")):
        return "vision"
    if has_rapidocr():
        return "rapidocr"
    return "manual"


def describe():
    e = available()
    return {
        "vision": "macOS Vision（最快，零额外依赖）",
        "rapidocr": "RapidOCR（跨平台）",
        "manual": "无本地识别引擎——将交由 AI 读图",
    }[e]


# ---------- 各引擎 ----------
def _run_vision(binp, paths):
    out = subprocess.run([binp] + paths, capture_output=True, text=True).stdout
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


def _run_rapidocr(paths):
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image
    ocr = RapidOCR()
    blocks = {}
    for p in paths:
        name = os.path.basename(p)
        try:
            W, H = Image.open(p).size
            res, _ = ocr(p)
        except Exception:
            blocks[name] = []
            continue
        items = []
        for box, text, _score in (res or []):
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            # 归一化到 0~1、原点左上，与 Vision 输出对齐
            items.append((text, min(xs) / W, min(ys) / H,
                          (max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H))
        blocks[name] = items
    return blocks


def recognize(paths):
    """识别图片，返回 {文件名: [(文本, x, y, w, h)]}。

    无可用引擎时抛 NeedManualOCR，由调用方走"AI 读图"路径。
    """
    if not paths:
        return {}
    binp = vision_binary()
    if binp:
        return _run_vision(binp, paths)
    if has_rapidocr():
        return _run_rapidocr(paths)
    raise NeedManualOCR([os.path.basename(p) for p in paths])


class NeedManualOCR(Exception):
    """没有本地识别引擎，需要由 AI 读图补齐。"""

    def __init__(self, files):
        super().__init__("无可用的本地文字识别引擎")
        self.files = files


# ---------- 兜底：交给 AI 读图 ----------
MANUAL_TEMPLATE = {
    "_说明": "本地没有文字识别引擎。请逐张打开 source/ 下的截图，把读到的内容填进 payments。"
             "金额取实际扣款额（正数），时间取消费/交易时间，商户取『商品说明』或『交易对方』。",
    "payments": {},
}


def write_manual_request(batch, files):
    """导出待读清单，供 AI 看图填写。"""
    work = os.path.join(batch, ".work")
    os.makedirs(work, exist_ok=True)
    tpl = dict(MANUAL_TEMPLATE)
    tpl["payments"] = {f: {"time": "", "amount": None, "merchant": "",
                           "pay_method": "", "order_no": ""} for f in files}
    p = os.path.join(work, "ocr.json")
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(tpl, fh, ensure_ascii=False, indent=2)
    return p


def load_manual(batch):
    """读取 AI 填好的 ocr.json；未填完则返回 None。"""
    p = os.path.join(batch, ".work", "ocr.json")
    if not os.path.exists(p):
        return None
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    pays = data.get("payments") or {}
    if not pays or any(v.get("amount") in (None, "") for v in pays.values()):
        return None
    return pays
