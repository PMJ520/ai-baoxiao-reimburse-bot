# 多阶段构建。所有依赖均有预编译 wheel，故无需编译器——
# 既省去 apt 拉取（常见的构建失败点），也让构建更快、镜像更小。
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt .
# rapidocr 会连带装完整版 opencv-python（依赖 X11），它与 headless 版共用 cv2
# 包名、互相覆盖，导致运行时缺 libxcb。装完强制只保留 headless 版。
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --only-binary=:all: -r requirements.txt \
    && /opt/venv/bin/pip uninstall -y opencv-python opencv-python-headless \
    && /opt/venv/bin/pip install --only-binary=:all: --no-deps opencv-python-headless


FROM python:3.12-slim AS runtime

# opencv-python-headless 仍需少量运行库；不装完整 opencv 以省约 60MB。
# 这些包体积小，即便软件源偶有波动也易于重试。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    OCR_CACHE_DIR=/tmp/ocr-cache \
    TZ=Asia/Shanghai

WORKDIR /app
COPY --chown=app:app app ./app

# 数据卷：SQLite 库、原件、产出物都在这里，备份只需打包此目录
RUN mkdir -p /data && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
