# 验收与上线执行手册：申万行业分类 + ETF 前十大重仓股

- 日期：2026-08-24
- 方案 / 评审 / 季度运维：同目录 `2026-08-24-stock-industry-etf-holdings-plan.md`、`...-plan-review-v1.1.md`、`ops.md`
- 当前状态：**代码与数据全部就绪**（本地已实跑数据脚本），**存量迁移尚未执行**（本地/线上都待执行）

## 1. 已完成的事实（可抽查）

| 事项 | 结果 |
|---|---|
| `stock_industry` 表 | 5552 行（5551 只 tushare 官方申万 + 1 只 tickflow 残留），含全部次新股 |
| 申万树（app_config `sw2021_tree`） | 31 个一级 / 132 个二级（含 tushare 补的「计算机-IT服务」分支） |
| `etf_constituents` 表 | 161 只 ETF 的前十大重仓快照（期次 20260331；镜像站无 20260630，自动回退），40 只债券/货币/QDII 无数据（正常） |
| 迁移 dry-run | 275/275 全部命中，0 待分类，0 树缺分支 |
| 测试 | 新增 68 个单测/API 测试全过；全量回归 621 过（仅既有 Windows tempfile flake） |
| 本地服务 | 已用新代码重启（127.0.0.1:8000） |

## 2. 本地验收清单（现状，未迁移）

### 2.1 功能验收（浏览器，登录 http://127.0.0.1:8000）

1. **ETF 权重股预览+导入**：标的管理 → 任意股票型 ETF 行（如 510300）→「权重股」按钮 →
   弹窗应显示：期次 20260331、抓取时间、10 只重仓股（代码/名称/权重/导入类目/状态），
   待分类行黄色。点「导入全部未管理标的」→ 进度轮询 → 完成后列表出现新股票，
   类目为申万行业（如 贵州茅台→食品饮料-白酒）。已管理的自动跳过，重复点导入全部 skipped（幂等）。
   - 注意：这是真实写操作，导入的股票会进标的池并回补行情（10 只约 1-2 分钟）。
   - 过渡态说明：迁移执行前，新导入股票的申万类目在「编辑类目」下拉里没有对应选项
     （树还是旧的），priority 为空；迁移后自动一致。
2. **手动添加类目建议**：标的管理 → 新增标的 → 输入股票代码（如 600519）→
   名称自动查询后，消息行显示「已识别：贵州茅台，建议类目：食品饮料-白酒（可修改）」；
   输入次新股/未覆盖代码显示「暂未识别行业……保持待分类」。
   （迁移前下拉选项还是旧树，预填值可能落不到选项上，文案正常即算通过。）
3. **登录墙**：退出登录后直接访问 `/instruments/api/suggest-category/600519.SS`
   和 `/instruments/api/etf-constituents/510300.SS` 应返回 401。

### 2.2 数据验收（命令行抽查）

```bash
cd "E:\codex project\tread quant"
.venv/Scripts/python -c "
import sqlite3
c = sqlite3.connect('data/trend_quant.db')
print(c.execute('SELECT source, COUNT(*) FROM stock_industry GROUP BY source').fetchall())
print(c.execute('SELECT COUNT(DISTINCT etf_symbol) FROM etf_constituents WHERE is_current=1').fetchone())
for r in c.execute('SELECT stock_symbol, stock_name, weight, rank FROM etf_constituents WHERE etf_symbol=? AND is_current=1 ORDER BY rank', ('510300.SS',)):
    print(r)
"
```

### 2.3 迁移 dry-run（随时可重跑，只读不写库）

```bash
.venv/Scripts/python scripts/migrate_category_sw2021.py --dry-run
```

输出：命中率、新树节点数、「旧类目 → 新类目」完整对照、待分类清单。

### 2.4 测试验收

```bash
.venv/Scripts/python -m pytest tests/test_stock_industry.py tests/test_tushare_scripts.py \
  tests/test_migrate_category_sw2021.py tests/api/test_instruments_etf_constituents_api.py -q
# 预期 53 passed
```

## 3. 存量迁移执行（本地、线上通用）

前置：§1 的三个数据脚本已在该环境跑过（`stock_industry` 与 `sw2021_tree` 存在）。

```bash
# 1. dry-run 确认对照报告
.venv/Scripts/python scripts/migrate_category_sw2021.py --dry-run

# 2. 停服（本地：kill 掉占 8000 端口的进程；线上：systemctl stop <服务名>）

# 3. 正式迁移（自动备份到 data/backups/，单事务，校验失败返回非 0）
.venv/Scripts/python scripts/migrate_category_sw2021.py

# 4. 重启服务 → 浏览器打开标的大盘/热力图，确认分组变成申万行业树
```

迁移后效果：275 只存量全部落入申万类目（0 待分类）；旧类目存于
`stock_category_archive`（migration=`sw2021_2026_q3`）；看板缓存自动失效重建。

**回滚**：停服 → 用 `data/backups/` 下迁移前备份替换 `data/trend_quant.db` → 重启。

## 4. 线上首次执行顺序（全新环境）

```bash
# 0. 代码到位（git pull / 部署包）后：
pip install tushare                     # 季度脚本依赖，应用本身不依赖
pip install -e .                        # pyproject 有变动时

# 1. 行业分类（免费源，先跑——树数据来自这里）
PYTHONPATH=src .venv/bin/python scripts/sync_stock_industry.py

# 2. tushare 窗口（账号 1-2 天有效，两个脚本一次跑完）
export TUSHARE_TOKEN=<token>
export TUSHARE_HTTP_URL=https://tuaremax.top   # 镜像站账号才需要；官方账号不用设
.venv/bin/python scripts/sync_sw_tushare.py     # 申万官方全量（补次新股 + 补树分支）
.venv/bin/python scripts/fetch_etf_holdings.py  # ETF 重仓快照（断点续传，中断重跑即可）

# 3. 存量迁移：见 §3（dry-run → 停服 → 迁移 → 重启 → 冒烟）
```

线上之后的日常：每月 1 日 04:30 调度器自动同步行业分类（无需操作）；
每季度重复 §4 第 2 步（详见 `ops.md`）。

## 5. 未提交说明

本功能全部改动目前在**工作区未提交**状态（8 个改动文件 + 11 个新文件）。
上线前需要先 commit & push，服务器再拉取。

## 6. 2026-08-24 晚间行为修正：重仓股如实展示（含港股）

**问题**：旧逻辑在抓取时把港股/美股/北交所行直接丢弃，跨境 ETF（如 517380
恒生沪深港创新药）A 股重仓不足 10 只时，权重 0.01%–0.03% 的打新锁定期新股
补位混进"前十大"。

**修正**（用户拍板）：
- `scripts/fetch_etf_holdings.py` 不再做市场过滤，全部市场的披露行按权重降序
  取前 10 如实落库；港股代码统一补 5 位前导零（`02269.HK`），名称仍由 tickflow 补全。
- 预览接口每行新增 `manageable` / `market_label`；弹窗里非 A 股行类目列显示
  市场名（港股/北交所/美股/境外），状态列固定「不纳入管理」，不计入待导入数量。
- 页面导入 Job 与 `import_all_etf_constituents.py` 对非 A 股行一律跳过
  （reason=`not_manageable`），完成消息单列「不纳入管理 N 只」。

**数据修正（需在 tushare 窗口重跑一次）**：

```bash
set TUSHARE_TOKEN=<token>
set TUSHARE_HTTP_URL=<镜像站地址>
.venv/Scripts/python scripts/fetch_etf_holdings.py --force
```

`save_etf_constituents` 会先整只软失效再 upsert 当期行，重抓后旧补位行自动失效，
无需手工清库。已入池标的不受本次修正影响（今日三次导入新增的 13 只均为正常
蓝筹股；517380 的摩尔线程/沐曦股份为 manual_add 手工添加，与本次问题无关）。
