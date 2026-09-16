# -*- coding: utf-8 -*-
"""对象存储。

原件（截图、发票 PDF）存文件系统、只把路径与 sha256 入库——
二进制塞进数据库会让备份和迁移都变痛苦。
按 sha256 分片存放，天然去重：IM 里重复发同一张图很常见。
"""
import hashlib
import shutil
from pathlib import Path

from .config import settings


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_path_for(digest: str, filename: str) -> str:
    """按 sha256 前两级分片，避免单目录堆积几万个文件。"""
    ext = Path(filename).suffix.lower()
    return f"{digest[:2]}/{digest[2:4]}/{digest}{ext}"


def abs_path(rel: str) -> Path:
    return settings.blob_dir / rel


def put_bytes(data: bytes, filename: str):
    """存入并返回 (sha256, 相对路径, 是否新文件)。内容相同则不重复落盘。"""
    digest = sha256_of(data)
    rel = rel_path_for(digest, filename)
    dst = abs_path(rel)
    if dst.exists():
        return digest, rel, False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst)                 # 原子落盘，避免半截文件被读到
    return digest, rel, True


def put_file(src):
    src = Path(src)
    digest = sha256_of_file(src)
    rel = rel_path_for(digest, src.name)
    dst = abs_path(rel)
    if dst.exists():
        return digest, rel, False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src, tmp)
    tmp.replace(dst)
    return digest, rel, True


def open_bytes(rel: str) -> bytes:
    return abs_path(rel).read_bytes()
