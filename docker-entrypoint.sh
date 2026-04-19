#!/bin/sh
# 容器入口：以 root 启动 → 修正 /app/data 所有权（因为 host bind mount 大概率是 root 拥有）→ 用 gosu 降权到 app 启动
set -e

DATA_DIR="${ANTHROPIC_PROXY_DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$DATA_DIR/logs"
    # 仅在所有权不正确时才 chown，避免每次启动都全量 walk（大日志库时慢）
    if [ "$(stat -c %u "$DATA_DIR")" != "1000" ]; then
        chown -R app:app "$DATA_DIR"
    fi
    exec gosu app "$@"
else
    exec "$@"
fi
