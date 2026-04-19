#!/usr/bin/env bash
# AnthropicProxy 一键部署脚本
#
# 用法（远程一键）:
#   bash <(curl -Ls https://raw.githubusercontent.com/danger-dream/AnthropicProxy/main/deploy.sh)
#
# 行为:
#   1. 显示项目信息
#   2. 检查 / 引导安装 Docker + Docker Compose
#   3. 交互式收集: 安装目录 / TG Bot Token / Admin User ID / 监听端口
#   4. 生成最小 docker-compose.yml + data/config.json
#   5. docker compose pull && up -d
#   6. 等待 /health 通过 + 验证 TG Bot polling
#
# 全程英文 set -e；失败立即退出并提示原因。

set -euo pipefail

# ─── 颜色 / 工具 ───────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_RESET='\033[0m'; C_BOLD='\033[1m'
    C_RED='\033[31m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_BLUE='\033[36m'
else
    C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

info()    { printf "${C_BLUE}[i]${C_RESET} %s\n" "$*"; }
ok()      { printf "${C_GREEN}[✓]${C_RESET} %s\n" "$*"; }
warn()    { printf "${C_YELLOW}[!]${C_RESET} %s\n" "$*"; }
err()     { printf "${C_RED}[✗]${C_RESET} %s\n" "$*" >&2; }
section() { printf "\n${C_BOLD}=== %s ===${C_RESET}\n" "$*"; }

# 交互输入兼容 `bash <(curl ...)`：stdin 被 curl 占用时强制走 /dev/tty
read_tty() {
    # 用法: read_tty <var_name> <prompt> [default]
    local __var="$1" __prompt="$2" __default="${3:-}"
    local __input
    if [[ -n "$__default" ]]; then
        __prompt="$__prompt [$__default]: "
    else
        __prompt="$__prompt: "
    fi
    if [[ -r /dev/tty ]]; then
        printf "%s" "$__prompt" > /dev/tty
        IFS= read -r __input < /dev/tty || __input=""
    else
        printf "%s" "$__prompt"
        IFS= read -r __input || __input=""
    fi
    [[ -z "$__input" && -n "$__default" ]] && __input="$__default"
    printf -v "$__var" "%s" "$__input"
}

confirm_tty() {
    # 用法: confirm_tty "问题" [Y|N]   默认 Y
    local prompt="$1" default="${2:-Y}" hint ans
    [[ "$default" == "Y" ]] && hint="[Y/n]" || hint="[y/N]"
    while true; do
        read_tty ans "$prompt $hint" ""
        [[ -z "$ans" ]] && ans="$default"
        case "$ans" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)   return 1 ;;
            *) warn "请输入 y / n" ;;
        esac
    done
}

# ─── 项目信息 ──────────────────────────────────────────────────
print_banner() {
    cat <<'EOF'

  ╔═══════════════════════════════════════════════════════╗
  ║                  AnthropicProxy                       ║
  ║   多渠道 · 智能调度 · 故障转移的 Anthropic 协议代理   ║
  ╚═══════════════════════════════════════════════════════╝

  仓库 : https://github.com/danger-dream/AnthropicProxy
  镜像 : ghcr.io/danger-dream/anthropicproxy:latest
  端口 : 22122 (默认)
  数据 : <安装目录>/data (config.json / state.db / logs/)

EOF
}

# ─── Docker 环境检测 / 安装 ────────────────────────────────────
check_docker() {
    section "[1/6] 检查 Docker 环境"
    if command -v docker >/dev/null 2>&1; then
        ok "Docker: $(docker --version)"
    else
        warn "未检测到 Docker"
        if confirm_tty "是否使用官方脚本一键安装 Docker（curl -fsSL https://get.docker.com | sh）？" Y; then
            curl -fsSL https://get.docker.com | sh
            systemctl enable --now docker || true
            ok "Docker 安装完成: $(docker --version)"
        else
            err "AnthropicProxy 需要 Docker 才能部署，已退出"
            exit 1
        fi
    fi

    if docker compose version >/dev/null 2>&1; then
        ok "Compose: $(docker compose version --short 2>/dev/null || echo v2)"
    elif command -v docker-compose >/dev/null 2>&1; then
        warn "检测到旧版 docker-compose（v1），脚本需要 v2 (docker compose)"
        err "请升级到 Docker 24+ 自带的 compose v2 后重试"
        exit 1
    else
        err "未检测到 docker compose"
        exit 1
    fi

    if ! docker info >/dev/null 2>&1; then
        err "Docker daemon 不可用 / 当前用户无权限。请用 root 或将用户加入 docker 组后重试"
        exit 1
    fi
}

# ─── 收集配置 ──────────────────────────────────────────────────
collect_config() {
    section "[2/6] 安装目录"
    read_tty INSTALL_DIR "安装目录" "/opt/anthropic-proxy"
    INSTALL_DIR="${INSTALL_DIR%/}"

    if [[ -d "$INSTALL_DIR" ]]; then
        warn "目录已存在: $INSTALL_DIR"
        printf "  当前内容:\n"
        ls -la "$INSTALL_DIR" 2>/dev/null | sed 's/^/    /' | head -10
        echo
        local choice
        read_tty choice "已存在，[U]pgrade 升级镜像 / [O]verwrite 覆盖配置 / [C]ancel 取消" "U"
        case "${choice^^}" in
            U) MODE="upgrade" ;;
            O) MODE="overwrite" ;;
            C|*) info "已取消"; exit 0 ;;
        esac
    else
        MODE="fresh"
        mkdir -p "$INSTALL_DIR/data"
    fi

    section "[3/6] Telegram Bot 配置"
    if [[ "$MODE" == "upgrade" && -f "$INSTALL_DIR/data/config.json" ]]; then
        info "检测到已有 config.json，升级模式跳过 Bot 配置（保留原值）"
        TG_TOKEN=""; TG_ADMIN=""; PORT=""
    else
        echo "  到 https://t.me/BotFather 创建 Bot 后获取 Token"
        echo "  到 https://t.me/userinfobot 查询自己的 Telegram User ID"
        echo
        while [[ -z "${TG_TOKEN:-}" ]]; do
            read_tty TG_TOKEN "Bot Token" ""
            [[ -z "$TG_TOKEN" ]] && warn "Bot Token 不能为空"
        done
        while [[ -z "${TG_ADMIN:-}" || ! "$TG_ADMIN" =~ ^[0-9]+$ ]]; do
            read_tty TG_ADMIN "Admin Telegram User ID（纯数字）" ""
            [[ ! "$TG_ADMIN" =~ ^[0-9]+$ ]] && warn "必须是纯数字"
        done

        section "[4/6] 监听端口"
        read_tty PORT "监听端口" "22122"
        if ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then
            warn "端口 ${PORT} 已被占用："
            ss -tlnp 2>/dev/null | grep ":${PORT}\b" | sed 's/^/    /'
            confirm_tty "继续使用此端口？（启动可能失败）" N || { err "已取消"; exit 1; }
        fi
    fi
}

# ─── 写文件 ────────────────────────────────────────────────────
write_files() {
    section "[5/6] 写入 docker-compose.yml + 初始化数据"
    mkdir -p "$INSTALL_DIR/data/logs"

    # compose 文件每次都重写（升级时也确保 image tag 拉到最新策略）
    cat > "$INSTALL_DIR/docker-compose.yml" <<EOF
# AnthropicProxy compose（由 deploy.sh 生成）
services:
  anthropic-proxy:
    image: ghcr.io/danger-dream/anthropicproxy:latest
    container_name: anthropic-proxy
    restart: unless-stopped
    ports:
      - "${PORT:-22122}:22122"
    environment:
      - TZ=Asia/Shanghai
      - ANTHROPIC_PROXY_DATA_DIR=/app/data
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:22122/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF
    ok "docker-compose.yml 已生成: $INSTALL_DIR/docker-compose.yml"

    # 仅 fresh / overwrite 时写最小 config.json；upgrade 保留原文件
    if [[ "$MODE" != "upgrade" ]]; then
        local effective_port="${PORT:-22122}"
        cat > "$INSTALL_DIR/data/config.json" <<EOF
{
  "listen": { "host": "0.0.0.0", "port": ${effective_port} },
  "apiKeys": {},
  "oauthAccounts": [],
  "channels": [],
  "telegram": {
    "botToken": "${TG_TOKEN}",
    "adminIds": [${TG_ADMIN}]
  }
}
EOF
        ok "data/config.json 已写入（最小模板，server 启动时会自动补全默认值）"
    else
        info "升级模式：保留原 data/config.json"
    fi
}

# ─── 启动 + 验证 ───────────────────────────────────────────────
start_and_verify() {
    section "[6/6] 拉镜像 + 启动 + 验证"

    cd "$INSTALL_DIR"
    info "拉取最新镜像..."
    docker compose pull
    info "启动容器..."
    docker compose up -d

    # 等容器健康
    info "等待容器健康（最多 60s）..."
    local ok_count=0
    for _ in $(seq 1 30); do
        if docker compose ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then
            ok_count=$((ok_count + 1))
            break
        fi
        # 兼容旧版没有 healthy 字段：退化为 /health 直接 curl
        if curl -fsS "http://127.0.0.1:${PORT:-22122}/health" >/dev/null 2>&1; then
            ok_count=$((ok_count + 1))
            break
        fi
        sleep 2
    done

    echo
    if [[ $ok_count -gt 0 ]]; then
        ok "容器运行中"
    else
        err "容器未在 60s 内健康"
        docker compose logs --tail 50
        exit 1
    fi

    # /health
    local health
    health=$(curl -fsS "http://127.0.0.1:${PORT:-22122}/health" 2>/dev/null || echo "")
    if [[ -n "$health" ]]; then
        ok "/health 响应: $health"
    else
        warn "/health 暂时拿不到，但容器已起，可稍后手动 curl 验证"
    fi

    # TG Bot polling 验证
    if docker compose logs --tail 50 2>/dev/null | grep -qE "tg.*polling|getUpdates"; then
        ok "TG Bot polling 已启动"
    else
        warn "未检测到 TG Bot polling 日志（也可能只是日志没刷出来），稍后再 docker compose logs 看看"
    fi

    cat <<EOF

${C_GREEN}${C_BOLD}╔════════════════════════════════════╗
║         🎉 部署完成 🎉              ║
╚════════════════════════════════════╝${C_RESET}

  安装目录: ${INSTALL_DIR}
  端口    : ${PORT:-22122}
  数据    : ${INSTALL_DIR}/data
  容器    : anthropic-proxy

下一步:
  1. 去 Telegram 找你的 bot 发 /start
  2. 在 [🔀 渠道管理] 添加第三方 API 渠道
  3. 在 [🔐 管理 OAuth]  添加 Claude 官方账户（可粘贴已有 OAuth JSON）
  4. 在 [🔑 管理 API Key] 创建下游调用用的 Key

常用命令:
  cd ${INSTALL_DIR}
  docker compose ps                  # 状态
  docker compose logs -f             # 实时日志
  docker compose restart             # 重启
  docker compose pull && docker compose up -d   # 升级到最新镜像
  docker compose down                # 停止 (数据保留)

EOF
}

# ─── main ──────────────────────────────────────────────────────
main() {
    print_banner
    check_docker
    collect_config
    write_files
    start_and_verify
}

main "$@"
