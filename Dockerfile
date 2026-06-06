# ============ Stage 1: 构建前端 ============
FROM node:18-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com && npm install --no-audit --no-fund
COPY frontend/ .
RUN npm run build

# ============ Stage 2: 运行后端 ============
FROM python:3.12-slim
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources
LABEL maintainer="NapCat QQ Manager"
LABEL description="NapCat QQ Bot Docker 容器管理面板"

WORKDIR /app

# 安装系统依赖（仅 curl 用于 healthcheck）
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

# 复制后端代码
COPY main.py start.py ./
COPY services/ services/
COPY middleware/ middleware/
COPY routers/ routers/
COPY docs/ docs/
# BotShepherd 中间件（git submodule，构建前需 git submodule update --init）
COPY BotShepherd/ BotShepherd/

# 从前端构建阶段复制产物
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# 数据目录
RUN mkdir -p /app/config /app/data

# 创建非 root 用户运行应用
# 注意：Docker socket 挂载时需确保宿主机 docker 组 GID 与容器内一致，
# 或通过 docker run --group-add 指定 docker 组
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser \
    && chown -R appuser:appuser /app

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV CORS_ORIGINS=""
ENV COOKIE_SECURE=false

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

VOLUME ["/app/config", "/app/data"]

USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

