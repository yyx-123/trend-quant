#!/bin/bash
set -e

# =============================================================================
# Trend Quant 一键部署脚本
# 适用系统：Ubuntu 22.04/24.04 (国内云服务器推荐)
# 运行方式：sudo bash scripts/deploy.sh
# =============================================================================

REPO_URL="https://github.com/yyx-123/trend-quant.git"
INSTALL_DIR="/opt/trend-quant"
# 如果有域名，请修改下一行，例如：DOMAIN="quant.yourdomain.com"
DOMAIN=""

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then
    log_error "请使用 sudo 或 root 用户运行此脚本"
    exit 1
fi

# 检查系统
if ! grep -qs "ubuntu" /etc/os-release; then
    log_warn "此脚本针对 Ubuntu 优化，其他系统可能需要手动调整"
fi

log_info "开始部署 Trend Quant ..."

# =============================================================================
# 1. 系统更新与基础依赖
# =============================================================================
log_info "更新系统并安装依赖 ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

apt-get install -y -qq \
    python3.11 \
    python3.11-venv \
    python3-pip \
    git \
    nginx \
    curl \
    ufw \
    apache2-utils

# =============================================================================
# 2. 克隆/更新代码
# =============================================================================
if [ -d "$INSTALL_DIR/.git" ]; then
    log_info "检测到已有代码，执行 git pull ..."
    cd "$INSTALL_DIR"
    git pull --quiet
else
    log_info "从 GitHub 克隆项目 ..."
    rm -rf "$INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# =============================================================================
# 3. Python 虚拟环境与依赖
# =============================================================================
cd "$INSTALL_DIR"

if [ ! -d ".venv" ]; then
    log_info "创建 Python 虚拟环境 ..."
    python3.11 -m venv .venv
fi

log_info "安装 Python 依赖（可能需要几分钟）..."
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -e . --quiet

# =============================================================================
# 4. 配置 Systemd 服务
# =============================================================================
log_info "配置 Systemd 服务 ..."

cat > /etc/systemd/system/trend-quant.service << 'EOF'
[Unit]
Description=Trend Quant System
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/trend-quant
Environment="PYTHONPATH=src"
Environment="PATH=/opt/trend-quant/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/trend-quant/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trend-quant.service

# 启动/重启服务
if systemctl is-active --quiet trend-quant; then
    systemctl restart trend-quant
else
    systemctl start trend-quant
fi

sleep 2
if systemctl is-active --quiet trend-quant; then
    log_info "Trend Quant 服务运行正常"
else
    log_error "服务启动失败，请查看日志：journalctl -u trend-quant -n 50"
    exit 1
fi

# =============================================================================
# 5. 配置 Nginx
# =============================================================================
log_info "配置 Nginx ..."

if [ -z "$DOMAIN" ]; then
    SERVER_NAME="_"
    log_warn "未配置域名，将使用服务器 IP 访问"
else
    SERVER_NAME="$DOMAIN"
    log_info "使用域名：$DOMAIN"
fi

cat > /etc/nginx/sites-available/trend-quant << EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location /static {
        alias /opt/trend-quant/web/static;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # 如需密码保护，取消下面两行注释并运行：htpasswd -c /etc/nginx/.htpasswd admin
    # auth_basic "Restricted";
    # auth_basic_user_file /etc/nginx/.htpasswd;
}
EOF

# 启用配置，禁用默认站点
ln -sf /etc/nginx/sites-available/trend-quant /etc/nginx/sites-enabled/trend-quant
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl restart nginx

# =============================================================================
# 6. 防火墙配置
# =============================================================================
log_info "配置防火墙 ..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw --force enable

# =============================================================================
# 7. 部署完成信息
# =============================================================================
PUBLIC_IP=$(curl -s -4 ifconfig.me || echo "未知")

log_info "========================================"
log_info "部署完成！"
log_info "========================================"
echo ""
echo -e "  项目目录：${GREEN}$INSTALL_DIR${NC}"
echo -e "  服务状态：${GREEN}systemctl status trend-quant${NC}"
echo -e "  查看日志：${GREEN}journalctl -u trend-quant -f${NC}"
echo -e "  重启服务：${GREEN}systemctl restart trend-quant${NC}"
echo ""

if [ -z "$DOMAIN" ]; then
    echo -e "  访问地址：${GREEN}http://$PUBLIC_IP${NC}"
    echo -e "  ${YELLOW}提示：如需绑定域名，请修改 DNS 指向此服务器 IP，${NC}"
    echo -e "  ${YELLOW}      然后编辑本脚本修改 DOMAIN 变量重新运行。${NC}"
else
    echo -e "  访问地址：${GREEN}http://$DOMAIN${NC}"
    echo -e "  ${YELLOW}提示：建议配置 HTTPS，可运行：certbot --nginx -d $DOMAIN${NC}"
fi

echo ""
echo -e "  ${YELLOW}重要提醒：${NC}"
echo -e "  1. 请确保云厂商安全组已放行 TCP 80/443 端口"
echo -e "  2. 如需密码保护，编辑 /etc/nginx/sites-available/trend-quant 取消注释 auth_basic 两行"
echo -e "  3. 生产环境建议配置 HTTPS（安装 certbot）"
echo ""
