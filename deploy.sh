#!/bin/bash
# anthropic-proxy 一键部署脚本
# 用法: 先把整个 anthropic-proxy 目录上传到服务器，然后执行此脚本
#   scp -P 27920 -r anthropic-proxy/ root@<host>:/opt/
#   ssh -p 27920 root@<host> 'bash /opt/anthropic-proxy/deploy.sh'

set -e

INSTALL_DIR="/opt/anthropic-proxy"
SERVICE_NAME="anthropic-proxy"

echo "=== anthropic-proxy 部署 ==="

echo "[1/5] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv > /dev/null 2>&1

echo "[2/5] 创建 Python 虚拟环境..."
cd "$INSTALL_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r requirements.txt

echo "[3/5] 检查配置..."
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    # 首次启动 server.py 会自动生成，这里仅预创建 logs 目录
    mkdir -p "$INSTALL_DIR/logs"
    echo "  config.json 不存在，首次启动将自动生成默认模板"
fi

echo "[4/5] 配置 systemd 服务..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=anthropic-proxy — Anthropic API multi-channel proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 -u ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}

echo "[5/5] 启动服务..."
# 端口占用检测（排除本服务自己占着的情况）
PORT=$(python3 -c "import json; print(json.load(open('${INSTALL_DIR}/config.json'))['listen']['port'])" 2>/dev/null || echo "18082")
if ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then
    # 已占用：排查是不是自己的 service
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        echo "  端口 ${PORT} 被本服务（${SERVICE_NAME}）占用，将 restart"
    else
        echo "⚠ 端口 ${PORT} 已被其他进程占用："
        ss -tlnp 2>/dev/null | grep ":${PORT}\b" || true
        echo "  请修改 ${INSTALL_DIR}/config.json 的 listen.port 后重试"
        exit 1
    fi
fi

systemctl restart ${SERVICE_NAME}
sleep 2

if systemctl is-active --quiet ${SERVICE_NAME}; then
    PORT=$(python3 -c "import json; print(json.load(open('${INSTALL_DIR}/config.json'))['listen']['port'])" 2>/dev/null || echo "18082")
    echo ""
    echo "=== 部署完成 ==="
    echo "  状态: $(systemctl is-active ${SERVICE_NAME})"
    echo "  端口: ${PORT}"
    echo "  日志: journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo "下一步:"
    echo "  1. 编辑 ${INSTALL_DIR}/config.json 填入 apiKeys / oauthAccounts / telegram"
    echo "  2. systemctl restart ${SERVICE_NAME}"
else
    echo "启动失败！查看日志:"
    journalctl -u ${SERVICE_NAME} -n 30 --no-pager
fi
