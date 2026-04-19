# syntax=docker/dockerfile:1.7
# AnthropicProxy 多阶段构建
# - builder：装依赖到独立 venv
# - runtime：拷代码 + venv，非 root 运行

# ─── Stage 1: builder ──────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ─── Stage 2: runtime ──────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Shanghai \
    ANTHROPIC_PROXY_DATA_DIR=/app/data

# 时区 + curl（HEALTHCHECK 用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl gosu \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -s /bin/bash app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY --chown=app:app server.py ./
COPY --chown=app:app src ./src

# 数据目录（挂载点）
RUN mkdir -p /app/data && chown -R app:app /app

# 不在这里 USER app；entrypoint 会修复 data 目录所有权后用 gosu 降权
EXPOSE 22122

# 健康检查走 /health（无鉴权）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:22122/health > /dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "server.py"]
