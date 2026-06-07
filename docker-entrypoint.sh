#!/bin/sh
# 容器入口：以 root 启动 → 修正 /app/data 所有权（host bind mount 大概率 root 拥有）
# → 若挂载了 docker.sock，把它的 gid 加到 app 的补充组（自更新 sidecar 需要）
# → 用 gosu 降权到 app 启动。
set -e

DATA_DIR="${ANTHROPIC_PROXY_DATA_DIR:-/app/data}"
DOCKER_SOCK="/var/run/docker.sock"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$DATA_DIR/logs" "$DATA_DIR/backups"
    # 仅在所有权不正确时才 chown，避免每次启动都全量 walk（大日志库时慢）
    if [ "$(stat -c %u "$DATA_DIR")" != "1000" ]; then
        chown -R app:app "$DATA_DIR"
    fi
    # backups 子目录可能被早期 root 操作创建，单独兜底修正属主（轻量，仅一层）
    if [ "$(stat -c %u "$DATA_DIR/backups" 2>/dev/null)" != "1000" ]; then
        chown app:app "$DATA_DIR/backups" 2>/dev/null || true
        chown app:app "$DATA_DIR/backups/"* 2>/dev/null || true
    fi

    # 自更新支持：若挂载了 docker.sock，让降权后的 app 用户能访问它。
    # gosu 会重置补充组，所以必须把 sock 的 gid 注册成一个组并把 app 加进去，
    # gosu 才会带上这个组。没挂 sock（普通部署）则完全跳过，无副作用。
    if [ -S "$DOCKER_SOCK" ]; then
        SOCK_GID="$(stat -c '%g' "$DOCKER_SOCK" 2>/dev/null || echo '')"
        if [ -n "$SOCK_GID" ] && [ "$SOCK_GID" != "0" ]; then
            # 找一个已有该 gid 的组名；没有就建一个 dockerhost 组
            GRP_NAME="$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1)"
            if [ -z "$GRP_NAME" ]; then
                addgroup -g "$SOCK_GID" dockerhost 2>/dev/null \
                  || groupadd -g "$SOCK_GID" dockerhost 2>/dev/null || true
                GRP_NAME="$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1)"
            fi
            if [ -n "$GRP_NAME" ]; then
                adduser app "$GRP_NAME" 2>/dev/null \
                  || usermod -aG "$GRP_NAME" app 2>/dev/null || true
            fi
        fi
    fi

    exec gosu app "$@"
else
    exec "$@"
fi
