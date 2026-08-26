# 线上部署操作手册（2026-08-25 CR 修复版上线）

> 适用版本：commit `ce1b3f1`（2026-08-24 代码审查 v3 实施方案全量落地）及之后。
> 目标环境：`/srv/trend-quant`（Ubuntu + systemd，frp 直连 8000 端口，无 nginx）。
> **核心风险点只有一个：不配 `TREND_MCP_TOKENS` 的话 /mcp 通道会全部 401（失败关闭设计）。**

## 一、部署前必做（不改就会坏）

### 1. `.env` 新增 `TREND_MCP_TOKENS`

这是本次部署唯一会破坏现有功能的新配置：

```bash
# /srv/trend-quant/.env 追加（token 用 openssl rand -hex 32 生成，不要用弱值）
TREND_MCP_TOKENS=<随机长token>=yyx
```

不配的话，MCP 通道（其他机器上的 MCP 客户端、daily-trade-report skill）**全部 401**。

### 2. 备份目录清理

`data/backups/` 下现有的旧手工备份（如 7 月的小文件、8-19 的 984M 快照）会在**次日凌晨 3:00 首次自动备份时被 keep=1 修剪删除**。想保留就先移出该目录：

```bash
mkdir -p /root/tq-backup-archive && mv /srv/trend-quant/data/backups/*.db /root/tq-backup-archive/
```

### 3. 服务降权的属主问题

仅当从「root 跑服务」切到新版 deploy.sh 的 `trendquant` 用户时需要。旧库 data/、logs/、.venv/ 很可能是 root 属主，切用户前执行：

```bash
chown -R trendquant:trendquant /srv/trend-quant
```

（deploy.sh 只 chown data/logs/.env，存量 .venv 需要手动处理，否则 pip install 写不进去。）

## 二、部署步骤

```bash
ssh root@<服务器IP>
cd /srv/trend-quant && git pull
source .venv/bin/activate && pip install -e .   # pyproject 有变动（dev 组新增）
sudo bash scripts/deploy.sh                     # 或直接 systemctl restart trend-quant
```

deploy.sh 已按线上现状重写（/srv、frp 直连 8000、无 nginx、trendquant 用户、.env 引导模板）。

## 三、数据库变更（全部自动，零手工 SQL）

启动时自动、幂等执行，详见同目录 `code-review-2026-08-24/db-changes-2026-08-25.md`：

- 删 3 个冗余索引（与 PK 同列，`DROP INDEX IF EXISTS`）；
- `PRAGMA foreign_keys=ON` + `busy_timeout=30s`（生产库已实测零孤儿行，安全）；
- 每日 03:00 自动备份（VACUUM INTO 在线备份，keep=1 只留最新一份）；
- job_runs 新增 running→interrupted 语义（三类任务行数会翻倍，属正常）。

回滚：代码回滚即可；被删索引如需重建用 git 历史里的 DDL，无数据损失。

## 四、部署后验证清单

```bash
# 1. 登录
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"yyx","password":"20160702"}'

# 2. MCP 通道：无 token 应 401，带 token 应通
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/mcp/sse
# 期望 401
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer <你的token>" http://127.0.0.1:8000/mcp/sse
# 期望 200

# 3. 次日 03:00 后确认备份落盘
ls -lh /srv/trend-quant/data/backups/
```

网页打开看板确认渲染正常（本次前端改动大：JS 抽成了独立文件，静态资源版本串已改为全量追踪，浏览器会自动拿到新版，无需强刷）。

## 五、MCP 客户端与 skill

- `~/.agents/skills/daily-trade-report/scripts/config.json` 的 `token` 字段填入与 `TREND_MCP_TOKENS` 里一致的那个 token；
- 其他 MCP 客户端在连接配置里加 `Authorization: Bearer <token>` 头即可，工具调用不再传账号密码。

## 六、可选的后续加固（不急，按顺序来）

1. **先验证 Bearer token 正常工作几天**，然后在 `.env` 配 `TREND_MCP_ALLOWED_HOSTS=<你的frp域名>:*` 开启 DNS rebinding 保护（配错会导致所有 MCP 请求 421，所以放在 token 验证之后）；
2. 首登后改密（缺省引导密码在仓库里是公开的，README 已注明）；
3. 每年 12 月 `pip install --upgrade chinese_calendar`（README 运维约定已写入；过期时导航栏会出「日历数据过期」提示条）；
4. GitHub Actions 已启用（push 自动跑 pytest + ruff + 死 CSS + 前端 JS 加载检查），首次 push 后看一眼 Actions 是否绿。
