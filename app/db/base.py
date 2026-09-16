# -*- coding: utf-8 -*-
"""数据库连接与会话。

SQLite 需要显式开启 WAL：异步解析任务与 API 会并发写库，
默认的库级写锁会直接抛 database is locked。
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ..config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url=None):
    url = url or settings.resolved_db_url
    is_sqlite = url.startswith("sqlite")
    kw = {"future": True, "echo": settings.debug}
    if is_sqlite:
        # 同一连接可跨线程使用：BackgroundTasks 里会用到
        kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
    eng = create_engine(url, **kw)
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")      # 读写不互斥
            cur.execute("PRAGMA busy_timeout=5000")     # 写冲突时重试而非报错
            cur.execute("PRAGMA foreign_keys=ON")       # SQLite 默认不校验外键
            cur.execute("PRAGMA synchronous=NORMAL")    # WAL 下兼顾安全与性能
            cur.close()
    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
    """建表并确保数据目录存在。可重复调用。"""
    from . import models  # noqa: F401  触发模型注册
    for d in (settings.data_dir, settings.blob_dir, settings.out_dir):
        d.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    return engine
