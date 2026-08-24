# 线上执行手册：申万行业分类 + ETF 前十大重仓股（2026-08-24）

目标：把本地已完成的功能与数据同步到线上服务器（Ubuntu + systemd + nginx，
代码 `/opt/trend-quant`，服务 `trend-quant`）。

**核心思路：服务器不需要 tushare，也不需要重新抓数据** —— 行业分类
（5552 只）与 ETF 重仓快照（161 只 ETF）已在本地抓完，导出种子 SQL 直接
导入服务器库。tushare 临时账号是否过期与本方案无关。

预计耗时：20-30 分钟（不含代码 review）。

---

## 第 0 步：本地提交并推送代码

```bash
cd "E:\codex project\tread quant"
git add -A && git commit && git push   # 由 ZCode 执行或手动
```

## 第 1 步：服务器更新代码

```bash
ssh root@<服务器IP>
cd /opt/trend-quant && git pull
source .venv/bin/activate && pip install -e .   # pyproject 有变动，装一次保险
```

不需要 `pip install tushare`（本方案用种子数据，见第 2 步）。

## 第 2 步：种子数据传到服务器

本地（Git Bash）：

```bash
# 种子文件已生成：scripts/temp/sw2021_seed.sql（5552 行业 + 1604 重仓 + 申万树）
# 若之后数据有更新，先重跑：.venv/Scripts/python scripts/export_industry_seed.py
scp scripts/temp/sw2021_seed.sql root@<服务器IP>:/opt/trend-quant/scripts/temp/
```

## 第 3 步：重启服务建表 → 导入种子 → 验证

```bash
# 新代码启动时 _init_tables 自动创建 stock_industry / etf_constituents /
# stock_category_archive 三张表（CREATE IF NOT EXISTS，对现有数据零影响）
systemctl restart trend-quant && sleep 5

# 导入（几秒完成；WAL 模式下服务在跑也可导入）
sqlite3 /opt/trend-quant/data/trend_quant.db < /opt/trend-quant/scripts/temp/sw2021_seed.sql
# 服务器没装 sqlite3 CLI 的话：apt-get install -y sqlite3，或用 .venv 的 python 执行

# 验证（应输出 5552 和 161）
sqlite3 /opt/trend-quant/data/trend_quant.db \
  "SELECT COUNT(*) FROM stock_industry; SELECT COUNT(DISTINCT etf_symbol) FROM etf_constituents WHERE is_current=1;"
```

此时 ETF 权重股弹窗、新增标的自动归类**已经可用**（它们只依赖种子数据）。

## 第 4 步：存量类目迁移（三级树重建）

```bash
cd /opt/trend-quant
# 4.1 dry-run：看命中率与「旧类目 → 新类目」对照，不写库
.venv/bin/python scripts/migrate_category_sw2021.py --dry-run

# 4.2 停服 → 正式迁移（自动备份到 data/backups/，单事务+校验）→ 起服
systemctl stop trend-quant
.venv/bin/python scripts/migrate_category_sw2021.py
systemctl start trend-quant
journalctl -u trend-quant -n 30 --no-pager   # 确认启动正常
```

## 第 5 步：线上冒烟

1. 标的大盘/热力图：分组已变成申万行业树（电子、机械设备、电力设备……），
   无「待分类」或仅有极少量。
2. 标的管理 → 任意股票型 ETF 行「权重股」：名称、申万类目列完整显示。
3. 新增标的：输入 600519 → 类目下拉消失，显示「将自动归类为 股票-食品饮料-白酒」。
4. 未登录访问 `/instruments/api/suggest-category/600519.SS` 返回 401。

⚠️ **nginx 静态缓存坑**：线上 nginx 对 `/static` 设了 `expires 7d; immutable`
（deploy.sh:168-171），style.css 的按钮/弹窗样式修复**用户浏览器可能缓存旧版最多 7 天**。
冒烟时务必 Ctrl+F5 硬刷新；如要强制所有用户立即生效，可在 style.css 引入处加版本号
query（如 `style.css?v=20260824`）后重启。

## 第 6 步（可选）：一键补齐全部重仓股入池

```bash
.venv/bin/python scripts/import_all_etf_constituents.py --dry-run   # 先看统计
.venv/bin/python scripts/import_all_etf_constituents.py
```

⚠️ 标的池将从 ~275 只扩到 ~660 只，每日 16:30 更新的请求量与耗时约 ×2.4
（tickflow 批量 100 只/请求，starter 档可承受，但请确认你要这个规模）。
不跑也没关系——页面单 ETF 导入功能不受影响。

## 回滚

只需回滚数据库（新代码对旧类目树完全兼容，不必回滚代码）：

```bash
systemctl stop trend-quant
cp /opt/trend-quant/data/backups/<迁移前备份>.db /opt/trend-quant/data/trend_quant.db
systemctl start trend-quant
```

旧类目另存于 `stock_category_archive` 表（migration=`sw2021_2026_q3`），可查。

## 上线后的日常

- 每月 1 日 04:30：调度器自动同步行业分类 + 待分类回补（无需操作）。
- 每季度：买 tushare 临时账号跑两个快照脚本（可继续用「本地跑 →
  export_industry_seed.py → scp 导入」的模式，服务器始终零 tushare 依赖），
  详见 `ops.md`。
