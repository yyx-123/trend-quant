# 运维手册：行业分类同步 / ETF 重仓股快照 / 类目迁移

- 方案：`2026-08-24-stock-industry-etf-holdings-plan.md`
- 评审：`2026-08-24-stock-industry-etf-holdings-plan-review.md`
- 实施完成：2026-08-24（P0-P4 全部落地）

## 日常（无需操作）

- **每月 1 日 04:30**：调度器自动跑 TickFlow 申万行业同步（`stock_industry_sync` job），
  含待分类回补；移动清单落 `job_runs`（job_type=`stock_industry_sync_tickflow`）。
- 手动添加标的：代码输入后自动查询名称 + 预填申万类目（未识别 → 待分类，可手改）。
- ETF 行「权重股」按钮：预览前十大重仓（期次/新鲜度/导入类目/状态）→ 一键导入未管理标的。
- 极低频批量操作（无页面入口）：`scripts/import_all_etf_constituents.py` 把所有 ETF 快照的
  重仓股一次性入池（自动归类 + 批量回补行情，幂等），支持 `--dry-run` 先统计。

## 每季度（tushare 临时账号窗口，建议季度末后第 20-30 天：4/7/10/1 月下旬）

```bash
# 0. 安装依赖（只需一次）
.venv/Scripts/pip install tushare        # 或 pip install -e ".[tushare]"

# 1. 注入临时账号 token（仅当前 shell，不写任何配置）
set TUSHARE_TOKEN=xxx                     # Windows cmd
$env:TUSHARE_TOKEN="xxx"                  # PowerShell
export TUSHARE_TOKEN=xxx                  # Git Bash

# 2. 申万官方全量分类同步（约 32 次调用，1 分钟）
.venv/Scripts/python scripts/sync_sw_tushare.py
#    输出：写入数、归属变更清单（仅报告不自动改）、待分类回补结果

# 3. ETF 前十大重仓股快照（约 200-450 次调用，几分钟）
.venv/Scripts/python scripts/fetch_etf_holdings.py
#    中断/账号被封：重跑同一条命令即可断点续传（按 etf+period 幂等）
#    调试：--symbols 510300.SS --dry-run；重抓：--force；指定期次：--period 20260630
```

## 一次性：存量类目迁移（建议放在第一个 tushare 窗口之后）

```bash
# 1. dry-run 出对照报告（不写库），确认无误后：
.venv/Scripts/python scripts/migrate_category_sw2021.py --dry-run

# 2. 停服 → 正式迁移（自动备份到 data/backups/）→ 重启服务 → 冒烟看板分组
.venv/Scripts/python scripts/migrate_category_sw2021.py
```

回滚：停服后恢复 `data/backups/` 下迁移前备份即可；旧类目另存于
`stock_category_archive` 表（migration=`sw2021_2026_q3`）。

## 排障

| 现象 | 处理 |
|---|---|
| `sync_sw_tushare.py` 报积分/权限 | index_classify / index_member_all 需 2000 积分；fund_portfolio 需 5000 积分，买号时确认档位 |
| 某 ETF 预览提示无快照 | 该 ETF 是债券/货币/QDII（无 A 股持仓，正常）或漏抓 → `--symbols <代码>` 单独补 |
| 待分类越积越多 | 查 `stock_industry_sync_tickflow` 的 job_runs 里 `still_unclassified`；次新股等下个 tushare 窗口，或手动在标的管理页改类目（manual 来源永不被同步覆盖） |
| 看板分组没变 | 类目迁移/回补后 updated_at 会自动失效看板缓存；仍异常就重启服务 |
