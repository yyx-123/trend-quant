#!/bin/bash
set -e

# =============================================================================
# Trend Quant 部署脚本（2026-08-25 按线上真实现状重写，P1-7）
#
# 线上架构事实（与 docs/stock-industry-etf-holdings/server-rollout.md 一致）：
#   - 代码目录 /srv/trend-quant（不是 /opt）
#   - 公网入口是 frp 直连 8000 端口，无 nginx 前置（gzip 由应用内中间件承担）
#   - 代码分发走 GitHub：本地 push → 服务器 git pull（与 server-rollout 第 1 步相同）
#   - 服务以专用非 root 用户 trendquant 运行（不再是 root）
#   - 密钥/通道配置在 .env（TICKFLOW_API_KEY / TREND_MCP_TOKENS 等）
#
# 适用系统：Ubuntu 22.04/24.04
# 运行方式：sudo bash scripts/deploy.sh
# =============================================================================

REPO_URL="https://github.com/yyx-123/trend-quant.git"
INSTALL_DIR="/srv/trend-quant"
SERVICE_USER="trendquant"
SERVICE_NAME="trend-quant"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    log_error "请使用 sudo 或 root 用户运行此脚本"
    exit 1
fi

if ! grep -qs "ubuntu" /etc/os-release; then
    log_warn "此脚本针对 Ubuntu 优化，其他系统可能需要手动调整"
fi

log_info "开始部署 Trend Quant（$INSTALL_DIR，服务用户 $SERVICE_USER）..."

# =============================================================================
# 1. 系统依赖（无 nginx：frp 直连 8000）
# =============================================================================
log_info "安装系统依赖 ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl

# =============================================================================
# 2. 专用服务用户（降权运行，不再 root）
# =============================================================================
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log_info "创建服务用户 $SERVICE_USER ..."
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$SERVICE_USER"
fi

# =============================================================================
# 3. 克隆/更新代码（分发方式与 README/server-rollout 统一：git）
# =============================================================================
if [ -d "$INSTALL_DIR/.git" ]; then
    log_info "检测到已有代码，执行 git pull ..."
    cd "$INSTALL_DIR"
    sudo -u "$SERVICE_USER" git pull --quiet
else
    if [ -e "$INSTALL_DIR" ]; then
        # 目录存在但不是 git 仓库：可能含 data/ 生产库，绝不静默删除
        log_error "$INSTALL_DIR 已存在但不是 git 仓库。请人工确认 data/ 等资产已备份后，"
        log_error "手动移走该目录再重跑（脚本不会对非 git 目录执行 rm -rf）。"
        exit 1
    fi
    log_info "从 GitHub 克隆项目 ..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# =============================================================================
# 4. Python 虚拟环境与依赖
# =============================================================================
if [ ! -d ".venv" ]; then
    log_info "创建 Python 虚拟环境 ..."
    sudo -u "$SERVICE_USER" python3.11 -m venv .venv
fi

log_info "安装 Python 依赖（可能需要几分钟）..."
sudo -u "$SERVICE_USER" .venv/bin/pip install --upgrade pip --quiet
sudo -u "$SERVICE_USER" .venv/bin/pip install -e . --quiet

# =============================================================================
# 5. .env 引导（密钥不入库；缺则生成模板并提示补全）
# =============================================================================
if [ ! -f ".env" ]; then
    log_warn ".env 不存在，生成模板——请编辑补全后重启服务："
    cat > .env << 'EOF'
# TickFlow 实时报价密钥（必需，盘中/实时功能依赖）
TICKFLOW_API_KEY=
# MCP 通道 Bearer token（token=用户名 映射；不配则 /mcp 对所有请求 401 失败关闭）
TREND_MCP_TOKENS=
# MCP DNS rebinding 保护（frp 域名，可带端口或 :* 通配；先验证 token 再开启）
# TREND_MCP_ALLOWED_HOSTS=mcp.example.com:* 
# 内置管理员 yyx 的引导密码（仅首次创建时生效；缺省见 README，首次登录后请改密）
# TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD=
EOF
    chown "$SERVICE_USER:$SERVICE_USER" .env
    chmod 600 .env
    log_warn "  → $INSTALL_DIR/.env"
else
    log_info ".env 已存在，跳过（密钥不落脚本）"
fi

# 数据与日志目录归属服务用户
mkdir -p data logs
chown -R "$SERVICE_USER:$SERVICE_USER" data logs

# =============================================================================
# 6. Systemd 服务（专用用户降权；frp 直连 8000，无 nginx）
# =============================================================================
log_info "配置 Systemd 服务 ..."

cat > /etc/systemd/system/trend-quant.service << EOF
[Unit]
Description=Trend Quant System
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PYTHONPATH=src"
Environment="PATH=$INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"

if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
else
    systemctl start "$SERVICE_NAME"
fi

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log_info "Trend Quant 服务运行正常"
else
    log_error "服务启动失败，请查看日志：journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

# =============================================================================
# 7. 完成信息
# =============================================================================
log_info "========================================"
log_info "部署完成！"
log_info "========================================"
echo ""
echo -e "  项目目录：${GREEN}$INSTALL_DIR${NC}"
echo -e "  服务状态：${GREEN}systemctl status $SERVICE_NAME${NC}"
echo -e "  查看日志：${GREEN}journalctl -u $SERVICE_NAME -f${NC}"
echo -e "  应用日志：${GREEN}$INSTALL_DIR/logs/app/${NC}"
echo -e "  重启服务：${GREEN}systemctl restart $SERVICE_NAME${NC}"
echo ""
echo -e "  ${YELLOW}重要提醒：${NC}"
echo -e "  1. 公网入口为 frp 直连 8000 端口（无 nginx），frp 配置不在本脚本范围"
echo -e "  2. 首次部署请编辑 .env 补全 TICKFLOW_API_KEY / TREND_MCP_TOKENS 后重启服务"
echo -e "  3. 内置管理员 yyx 首次登录后请立即改密（网页端无改密入口时见 README）"
echo -e "  4. 全新部署还需导入类目种子与行情数据，见 README 部署章节"
echo ""
