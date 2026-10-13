# StockArena 镜像：前端打包后由后端托管，页面与接口同一端口（18080）。
# 构建：docker build -t stockarena .      运行：见 docker-compose.yml 或 docs/DEPLOY.md
# 运行时数据（数据库、日志、主密钥、行情缓存）都在 /app/data，请挂载为卷并定期备份。

# ---------- 前端打包 ----------
FROM node:24-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# 使用手册页面直接读取 docs/USER_GUIDE.md
COPY docs/USER_GUIDE.md /src/docs/USER_GUIDE.md
RUN npm run build

# ---------- 后端 ----------
FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv

# tzdata：日志按北京时间显示（各市场的定时任务本身都带明确时区，不依赖这里）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.13

WORKDIR /app/backend
# 先只装依赖，代码改动时可以复用这一层
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
COPY config/ /app/config/
COPY LICENSE NOTICE DISCLAIMER.md /app/
COPY --from=frontend /src/frontend/dist /app/frontend/dist

# 以普通用户运行：入口脚本先修正挂载目录的属主，再降权（见 scripts/docker/entrypoint.sh）
RUN useradd --create-home --uid 1000 stockarena \
    && mkdir -p /app/data \
    && chown -R stockarena:stockarena /app/data
COPY --chmod=755 scripts/docker/entrypoint.sh /usr/local/bin/stockarena-entrypoint

ENV PATH=/app/backend/.venv/bin:$PATH \
    SA_DATA_DIR=/app/data \
    SA_FRONTEND_DIST=/app/frontend/dist \
    SA_ENV=prod

VOLUME ["/app/data"]
EXPOSE 18080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/api/auth/status', timeout=5)"

ENTRYPOINT ["stockarena-entrypoint"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080", "--workers", "1"]
