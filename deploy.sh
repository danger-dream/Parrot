#!/usr/bin/env bash
# Parrot 一键部署脚本 🦜
#
# 用法（远程一键）:
#   bash <(curl -Ls https://raw.githubusercontent.com/danger-dream/Parrot/main/deploy.sh)
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

# 已运行的 Parrot 必须先收到 SIGTERM 并完成数据库收尾。绝不使用 rm -f；
# 超时后中止部署，由管理员检查仍未退出的容器。
graceful_remove_container() {
    local name="$1" timeout="${PARROT_GRACEFUL_STOP_SECONDS:-120}" stopped=0
    [[ -z "$name" ]] && return 0
    docker inspect "$name" >/dev/null 2>&1 || return 0
    if [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" == "true" ]]; then
        info "向旧容器发送 SIGTERM，等待 Parrot 优雅退出（最多 ${timeout}s）..."
        docker kill --signal=TERM "$name" >/dev/null 2>&1 || {
            err "无法向旧容器发送 SIGTERM，已中止；不会强制删除"
            return 1
        }
        for _ in $(seq 1 "$timeout"); do
            if [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" != "true" ]]; then
                stopped=1
                break
            fi
            sleep 1
        done
        if [[ $stopped -ne 1 ]]; then
            err "旧容器未在 ${timeout}s 内优雅退出，已中止；不会使用 SIGKILL"
            return 1
        fi
    fi
    docker rm "$name" >/dev/null 2>&1 || {
        err "旧容器已停止但无法安全移除，已中止"
        return 1
    }
    ok "旧容器已优雅停止并移除"
}

# ─── 项目信息 ──────────────────────────────────────────────────
print_banner() {
    cat <<'EOF'

  ╔═══════════════════════════════════════════════════════╗
  ║                    🦜  Parrot  🦜                      ║
  ║   多家族 · 多渠道 · 故障转移的 AI 协议代理            ║
  ║   （Anthropic / OpenAI / 第三方 API 一网打尽）        ║
  ╚═══════════════════════════════════════════════════════╝

  仓库 : https://github.com/danger-dream/Parrot
  镜像 : ghcr.io/danger-dream/parrot:latest
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
    read_tty INSTALL_DIR "安装目录" "/opt/parrot"
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

    # 计算 docker.sock 的 gid，写进 compose 的 group_add（降权后的 app 用户才能访问 sock）
    SOCK_GROUP_ADD=""
    if [[ -S /var/run/docker.sock ]]; then
        local _sock_gid
        _sock_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo '')"
        if [[ -n "$_sock_gid" && "$_sock_gid" != "0" ]]; then
            SOCK_GROUP_ADD="    group_add:
      - \"${_sock_gid}\"
"
        fi
    fi

    # compose 文件每次都重写（升级时也确保 image tag 拉到最新策略）
    cat > "$INSTALL_DIR/docker-compose.yml" <<EOF
# Parrot compose (generated by deploy.sh) 🦜
services:
  parrot:
    image: ghcr.io/danger-dream/parrot:latest
    container_name: parrot
    restart: unless-stopped
    ports:
      - "${PORT:-22122}:22122"
    environment:
      - TZ=Asia/Shanghai
      - ANTHROPIC_PROXY_DATA_DIR=/app/data
    volumes:
      - ./data:/app/data
      # 自更新：挂载 docker.sock，容器内才能通过 Engine API 起 sidecar 重建自己
      - /var/run/docker.sock:/var/run/docker.sock
${SOCK_GROUP_ADD}    healthcheck:
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
        # 关键：容器内服务固定监听 22122（容器内端口），用户选的 PORT 只用于宿主侧
        # 端口映射（ports: PORT:22122）。HEALTHCHECK 也查 22122，三者一致。
        # 早期脚本误把 listen.port 设成 PORT，导致选非 22122 端口时容器永远 unhealthy。
        cat > "$INSTALL_DIR/data/config.json" <<EOF
{
  "listen": { "host": "0.0.0.0", "port": 22122 },
  "apiKeys": {},
  "oauthAccounts": [],
  "channels": [],
  "telegram": {
    "botToken": "${TG_TOKEN}",
    "adminIds": [${TG_ADMIN}]
  },
  "updateChecker": {
    "enabled": true,
    "autoUpdate": false,
    "repo": "danger-dream/Parrot",
    "runtimeMode": "docker",
    "composeDir": "${INSTALL_DIR}",
    "composeService": "parrot",
    "containerName": "parrot",
    "image": "ghcr.io/danger-dream/parrot:latest",
    "updaterImage": "docker:cli"
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
    # 升级/覆盖时旧容器可能带着旧 compose service label（如历史 anthropic-proxy），
    # 直接 up 新 service 会撞 container_name。数据在 ./data，删除容器不删数据。
    local _cname
    _cname="$(grep -oP 'container_name:\s*\K\S+' "$INSTALL_DIR/docker-compose.yml" 2>/dev/null | head -1)"
    if [[ "$MODE" != "fresh" && -n "$_cname" ]]; then
        graceful_remove_container "$_cname" || return 1
    fi
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
  容器名  : parrot

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

# ─── update 子命令：把老部署迁移到支持自更新 ────────────────────
#
# 用法：bash deploy.sh update [安装目录]
# 作用（幂等）：
#   1. 给 docker-compose.yml 补上 docker.sock 挂载 + group_add（自更新所需）
#   2. 给 data/config.json 补上 updateChecker 段（composeDir/containerName 等）
#   3. compose pull + up -d 让改动生效
# 不动用户的渠道/OAuth/APIKey 等任何业务配置。
cmd_update() {
    print_banner
    section "迁移老部署 → 支持自更新"

    # 定位安装目录
    INSTALL_DIR="${1:-}"
    if [[ -z "$INSTALL_DIR" ]]; then
        read_tty INSTALL_DIR "Parrot 安装目录" "/opt/parrot"
    fi
    INSTALL_DIR="${INSTALL_DIR%/}"
    if [[ ! -f "$INSTALL_DIR/docker-compose.yml" ]]; then
        err "未找到 $INSTALL_DIR/docker-compose.yml，请确认安装目录"
        exit 1
    fi
    ok "安装目录: $INSTALL_DIR"

    local compose="$INSTALL_DIR/docker-compose.yml"
    local cfg="$INSTALL_DIR/data/config.json"
    local ts; ts="$(date +%Y%m%d-%H%M%S)"

    # ① 备份现有文件
    cp "$compose" "$compose.bak.$ts"
    [[ -f "$cfg" ]] && cp "$cfg" "$cfg.bak.$ts"
    ok "已备份 compose / config（.bak.$ts）"

    # 失败自动回滚：迁移中任何一步出错（set -e 触发 ERR），从 .bak 还原 compose/config，
    # 避免留下半改坏的文件。成功收尾时会解除该 trap。
    _migrate_rollback() {
        warn "迁移中断，正在从备份还原 compose / config …"
        [[ -f "$compose.bak.$ts" ]] && cp -f "$compose.bak.$ts" "$compose" 2>/dev/null || true
        [[ -f "$cfg.bak.$ts" ]] && cp -f "$cfg.bak.$ts" "$cfg" 2>/dev/null || true
        err "已回滚到迁移前状态。请检查错误后重试。"
    }
    trap '_migrate_rollback' ERR

    # ② 给 compose 补 docker.sock 挂载 + group_add（用 python 安全改 YAML）
    local sock_gid=""
    [[ -S /var/run/docker.sock ]] && sock_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo '')"
    python3 - "$compose" "$sock_gid" <<'PYEDIT'
import sys, re
path, gid = sys.argv[1], sys.argv[2]
s = open(path, encoding="utf-8").read()
changed = False
# 把 image 指向带自更新的最新版（老部署可能 pin 了旧 tag/旧镜像）
import re as _re
import os as _os
img = _os.environ.get("PARROT_IMAGE", "ghcr.io/danger-dream/parrot:latest")
new_s = _re.sub(r"(\n[ \t]*image:[ \t]*).*", lambda m: m.group(1)+img, s, count=1)
if new_s != s:
    s = new_s; changed = True
# 修复端口映射：容器侧强制 22122（镜像 EXPOSE/HEALTHCHECK 固定 22122），保留宿主侧用户原值。
# 形如 "22125:9999" 或 "22125:22125" → "22125:22122"。
def _fix_ports_block(m):
    head = m.group(1)          # "\n<indent>ports:\n"
    body = m.group(2)          # 映射行们
    # 把每条 "host:container" 的容器侧改成 22122
    fixed = _re.sub(r'("?)(\d+)(:)(\d+)("?)',
                    lambda x: f"{x.group(1)}{x.group(2)}{x.group(3)}22122{x.group(5)}",
                    body)
    return head + fixed
# 仅匹配 ports: 紧跟的列表块（- 开头的连续行）
new_s2 = _re.sub(r'(\n[ \t]*ports:\n)((?:[ \t]+-[ \t]*.*\n)+)', _fix_ports_block, s, count=1)
if new_s2 != s:
    s = new_s2; changed = True
# 加 docker.sock 挂载（如缺）
if "/var/run/docker.sock" not in s:
    m = re.search(r"(\n([ \t]*)volumes:\n(?:\2[ \t]+- .*\n)+)", s)
    if m:
        block, indent = m.group(1), m.group(2)
        item = f"{indent}  - /var/run/docker.sock:/var/run/docker.sock\n"
        s = s.replace(block, block + item, 1)
        changed = True
# 加 group_add（如缺且有 gid）
if gid and gid != "0" and "group_add:" not in s:
    m = re.search(r"\n([ \t]*)volumes:\n", s)
    if m:
        indent = m.group(1)
        ga = f"{indent}group_add:\n{indent}  - \"{gid}\"\n"
        # 插在 volumes 块之后：找 volumes 块结束位置
        vm = re.search(r"(\n[ \t]*volumes:\n(?:[ \t]+- .*\n)+)", s)
        if vm:
            s = s.replace(vm.group(1), vm.group(1) + ga, 1)
            changed = True
open(path, "w", encoding="utf-8").write(s)
print("compose changed:", changed)
PYEDIT
    ok "docker-compose.yml 已补 docker.sock 挂载 + group_add"

    # ③ 给 config.json 补 updateChecker 段（用 python，保留所有原有字段）
    if [[ -f "$cfg" ]]; then
        # 从 compose 探测真实 container_name / service 名（老部署可能不叫 parrot）
        local detected_cname detected_svc detected_cport
        detected_cname="$(grep -oP 'container_name:\s*\K\S+' "$compose" 2>/dev/null | head -1)"
        detected_svc="$(grep -oP '^\s{2}\K[a-zA-Z0-9_-]+(?=:\s*$)' "$compose" 2>/dev/null | head -1)"
        # 探测 compose 端口映射的"容器侧"端口（"宿主:容器" 的右值），用于对齐 listen.port
        detected_cport="$(awk '/ports:/{f=1;next} f&&/[0-9]+:[0-9]+/{gsub(/[^0-9:]/,"");split($0,a,":");print a[2];exit}' "$compose" 2>/dev/null)"
        python3 - "$cfg" "$INSTALL_DIR" "${detected_cname:-parrot}" "${detected_svc:-parrot}" "${detected_cport:-22122}" <<'PYEDIT'
import sys, json
import os
path, install_dir = sys.argv[1], sys.argv[2]
det_cname = sys.argv[3] if len(sys.argv) > 3 else "parrot"
det_svc = sys.argv[4] if len(sys.argv) > 4 else "parrot"
det_cport = sys.argv[5] if len(sys.argv) > 5 else "22122"
try:
    c = json.load(open(path, encoding="utf-8"))
except Exception as e:
    print("config parse failed:", e); sys.exit(1)
uc = c.get("updateChecker") or {}
# 只补缺失键，不覆盖用户已设的值
defaults = {
    "enabled": True, "autoUpdate": False, "repo": "danger-dream/Parrot",
    "runtimeMode": "docker", "composeDir": install_dir,
    "composeService": det_svc, "containerName": det_cname,
    "image": os.environ.get("PARROT_IMAGE", "ghcr.io/danger-dream/parrot:latest"),
    "updaterImage": "docker:cli",
}
for k, v in defaults.items():
    uc.setdefault(k, v)
# composeDir / container / service 强制对齐实际值（防失配）
uc["composeDir"] = install_dir
uc["containerName"] = det_cname
uc["composeService"] = det_svc
c["updateChecker"] = uc
# 修复端口错位：容器内服务必须监听"容器侧映射端口"（通常 22122），否则与
# compose 端口映射 + HEALTHCHECK 错位导致永久 unhealthy（老脚本遗留 bug）。
try:
    want_port = int(det_cport)
    cur_port = ((c.get("listen") or {}).get("port"))
    if cur_port != want_port:
        c.setdefault("listen", {})["port"] = want_port
        print(f"config listen.port fixed: {cur_port} -> {want_port}")
except Exception as _e:
    print("listen.port fix skipped:", _e)
json.dump(c, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("config updateChecker patched")
PYEDIT
        ok "data/config.json 已补 updateChecker 段（保留原有业务配置）"
    else
        warn "未找到 config.json，跳过（容器首启会生成默认）"
    fi

    # 文件改动已全部完成 → 解除回滚 trap（后续 compose 操作失败不该回滚文件，
    # 因为镜像可能已拉、容器可能已重建，回滚 compose 反而造成不一致）。
    trap - ERR

    # ④ 拉新镜像 + 重建
    section "拉取新镜像 + 重建容器"
    cd "$INSTALL_DIR"
    # 旧容器仍在跑时 compose up 可能撞名，这里显式先停旧容器再起（秒级停机）
    local _cname
    _cname="$(grep -oP 'container_name:\s*\K\S+' "$compose" 2>/dev/null | head -1)"
    docker compose pull || { err "镜像拉取失败"; return 1; }
    if [[ -n "$_cname" ]]; then
        graceful_remove_container "$_cname" || return 1
    fi
    docker compose up -d || { err "容器启动失败"; return 1; }
    # 宿主映射端口（compose ports 的左值）
    local _hport
    _hport="$(awk '/ports:/{f=1;next} f&&/[0-9]+:[0-9]+/{gsub(/[^0-9:]/,"");split($0,a,":");print a[1];exit}' "$compose" 2>/dev/null)"
    info "等待健康（最多 60s）..."
    local okc=0
    for _ in $(seq 1 30); do
        if [[ -n "$_hport" ]] && curl -fsS "http://127.0.0.1:${_hport}/health" >/dev/null 2>&1; then okc=1; break; fi
        if docker compose ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then okc=1; break; fi
        sleep 2
    done
    if [[ $okc -gt 0 ]]; then
        ok "容器已健康，自更新迁移完成 ✅"
    else
        warn "60s 内未确认健康，手动 docker compose logs -f 看看"
    fi

    cat <<EOF

${C_GREEN}${C_BOLD}迁移完成${C_RESET}：现在去 Telegram 进入「⚙ 系统设置 → 🆕 版本更新」
即可看到「🚀 立即更新」按钮，后续升级一键完成（双重确认 + 自动备份）。

回退：cp ${compose}.bak.${ts} ${compose} 并 docker compose up -d
EOF
}

# ─── main ──────────────────────────────────────────────────────
main() {
    # 子命令分发：update = 迁移老部署到自更新；其余 = 全新/升级安装
    case "${1:-}" in
        update|--update|upgrade-selfupdate)
            shift || true
            check_docker
            cmd_update "$@"
            return
            ;;
    esac
    print_banner
    check_docker
    collect_config
    write_files
    start_and_verify
}

main "$@"
