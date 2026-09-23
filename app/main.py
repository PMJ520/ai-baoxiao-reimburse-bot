# -*- coding: utf-8 -*-
"""应用入口。"""
import logging

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db.base import init_db
from .admin import views as admin_views
from .api import batches, bridge, documents, templates
from .im import runner as im_runner
from .pipeline.ocr import engine as ocr

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("expense-hub")

app = FastAPI(title="费用报销中台", version="0.1.0")
app.include_router(documents.router)
app.include_router(batches.router)
app.include_router(templates.router)
app.include_router(bridge.router)
app.include_router(admin_views.router)
app.mount("/admin/static",
          StaticFiles(directory=str(Path(__file__).parent / "admin" / "static")),
          name="admin-static")


@app.on_event("startup")
def _startup():
    init_db()
    log.info("数据目录 %s", settings.data_dir)
    log.info("识别引擎 %s — %s", ocr.available(), ocr.describe())
    if not settings.api_token:
        log.warning("未配置 API_TOKEN，所有接口将拒绝访问")
    from .services.llm import current_config
    if not (current_config().api_key or "").strip():
        log.warning("未配置 LLM，对话功能不可用")
    # 长连接放后台线程：它是阻塞的，放主线程会挡住 API
    im_runner.start_all()


@app.get("/health", tags=["meta"])
def health():
    """健康检查。不鉴权，供容器与安装脚本探活。"""
    return {"ok": True, "version": "0.1.0", "ocr": ocr.available(),
            "im": im_runner.status()}
