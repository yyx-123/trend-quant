# Round 5 审查报告（loop-review-ds4f）

> **状态：CLOSED（2026-09-25 闭合）**
> - 修复清单与验收记录：`round5-fixes.md`；Round 6 两个确认代理的复核结论与后续修复见
>   `round6-review.md` / `round6-fixes.md`（Round 6 起每轮独立成文件，索引见 `round5-fixes.md`）；
> - 全量回归：1637 passed / 2 failed（失败均为既有 Windows 临时文件 flake）；
> - ruff：(file, rule) 集合与基线完全一致。
>
> 日期：2026-09-25
> 审查对象：commit `ebcd0f6`（Round 4 闭合后全量代码）
> 审查方式：独立审查代理 R5 审**存量栈与跨栈原语**（前四轮集中在投研栈）：
> `src/rule_backtest/`（既有生产功能共享的旧引擎）、`src/services/` + `src/app/` 存量编排、
> `src/core/` 双栈共用的数值原语、配置/CI/脚本运行面，以及"新栈端到端链"（队列→worker→
> 评估→判定→台账→物化文件）在**真实 runner** 上的数字自洽性。

## 总体结论

**FAIL（1 项 P1 + 3 项 P2 + 3 项 P3）**。先给出一个定性事实：`git diff --stat f93031e HEAD
-- src/rule_backtest/ src/services/` **为空**——这两个包与投研栈之前**逐字节相同**，因此
下述存量缺陷都是**既有的**、只是被新栈消费而放大其可见性。

---

## P1

### R5-P1-1 生产管理员账号仍在使用**源码可见的默认引导密码**，且以明文 HTTP 提供服务、会话 cookie 无 `Secure`

- 位置：`src/app/main.py:59`（`_BUILTIN_ADMIN_DEFAULT_PASSWORD = "20160702"`）、`:72`、
  `src/app/routers/auth.py:73-79`；部署面 `docs/26-09-06-nginx-sse-gzip/deploy-notes-2026-09-06.md:3`
- 事实（只读库探针）：`data/trend_quant.db` 的 `users.yyx`（`is_admin=1`）哈希与仓库内常量
  一致——用应用自身的 `verify_password(hash, "20160702")` 返回 **True**（另两个用户 False）。
  部署文档写明"80 端口统一反代 → frp 隧道 → 后端"；会话 cookie 只设
  `HttpOnly; SameSite=lax`、**无 `Secure`**，代码中亦无 HSTS/HTTPS 跳转。
- 影响：任何能看到仓库（或猜到默认值）的人都能登录这台生产实例的管理员账号；链路上任何
  一跳都可截取会话 cookie。属**部署/凭据**问题 + 引导机制设计问题。
- 本轮处置（不改行为，只可见化）：`_ensure_builtin_admin` 检测到"内置默认密码仍在用"
  时**启动即响亮告警**（`SECURITY: ... rotate it immediately`），新建时若未显式配置
  `TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD` 同样告警。
- 需用户决策（**最高优先**）：立即轮换 `yyx` 密码；长期策略二选一（启动缺显式引导密码
  即拒绝启动 / 首次登录强制改密）；以及是否在 frp 前终结 TLS 并加 `Secure`+HSTS。

---

## P2

### R5-P2-2 零成交/全现金腿产出 Sharpe ≈ 6e12 的噪声值，而该值**决定判定**

- 位置：`src/rule_backtest/metrics.py:102`（`sharpe = mean/std*sqrt(252) if std_ret > 0`）
  ← 数据来自 `src/engine/account.py:69-71`（空仓计息）；消费 `research/evaluations/backtest.py:187`
  → `verdict_rules.py:139`
- 事实（真引擎）：1200 个交易日、**0 笔成交**（universe diff 使候选为空）→ `_nav_summary`
  给出 `sharpe = 6,090,575,971,984.006`、`std_daily_return = 1.03e-16`（纯浮点残差）。
  再以"好实验（ΔSharpe +0.9、高原、配对 t=3.0、DSR 0.4、n=2400）"对**全现金基准**取差
  → `suggested_verdict = rejected`；同一证据换正常基准 → `confirmed`。
- 修复（本轮）：在**新栈侧**（不碰共享 `compute_summary`，避免改动旧分支数字）：
  `_nav_summary` 判定退化腿（`std ≤ |mean|·1e-6`）→ `sharpe/sortino = None` +
  `degenerate_leg=True`；Δ 计算抽 `_deltas_from_summaries`，任一腿退化则
  `delta_sharpe/delta_sortino/delta_calmar` 记 **None**（而非用噪声作差）→ 判定自然
  落到 inconclusive 而不是 rejected。配钉子（退化腿 + 判定方向 + 非退化指标照常作差）。
- 共享函数的既有口径（分母守卫、Sortino 用负收益子集 std、`total_return` 不含首日等）
  属**存量口径保护**范围 → 进待决策（R5-D-4）。

### R5-P2-3 因子表先落库、派生 qfq 失败时静默分叉且**永不重试**

- 位置：`src/data/service.py:395-399` → `src/services/indicator_builder.py:202-206,220-224`
  → `src/data/service.py:419-426` + `:80-82`
- 事实（探针，临时库）：`sync_ex_factors` 先把因子 1.10 改写为 1.25；随后
  `repair_broken_symbols` 因 raw 不全而**拒绝物化**，但因子已落库 → 不变量
  `market_data_qfq == compute_qfq(raw, ex_factors)` 在重叠 500 行上破裂
  （最大相对差 **0.0913**）；pipeline 仍报 `{'rebuilt': 1, 'dividend_breaks': []}`，
  且缓存版本号等于当前 qfq 版本 → `_cache_fresh` 返回 **True**（读的是旧值）；
  次日 `sync_ex_factors` 返回 `changed: []` → **永不重试**，`job_runs` 无痕。
- 生产现状：875/875 标的 raw 覆盖 qfq（当前 0 个受影响）→ **潜在**缺陷，触发条件为
  任何一次物化失败（回填异常、磁盘满、进程中途退出）。
- 处置：属**存量数据管线行为**（改写入顺序会动既有业务），本轮只**如实记录**并进待决策
  （R5-D-3：因子写入是否必须先物化成功，或落 `pending_rematerialize` 标记 + 重试 + 留痕）。

### R5-P2-4 CI 跑不了新栈的 CLI 用例（硬编码 Windows venv 解释器）

- 位置：`tests/integration/test_critical_paths.py:106`（`ROOT/.venv/Scripts/python.exe`）
- 事实：`ci.yml:12` 是 `ubuntu-latest`，`.venv/` 被 gitignore、CI 用系统 Python →
  `FileNotFoundError`。即唯一覆盖 `scripts/research_cli.py` 的路径在 CI 上从未执行。
- 修复：改用 `sys.executable`（同一解释器、跨平台、去掉"本地必须有 .venv"的隐含前提）+ 钉子。

---

## P3

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| R5-P3-5 | `rule_backtest/metrics.py:93-105,109,131` | 共享 `compute_summary` 的退化口径：Sortino 用**负收益子集** std（非标准下行偏差，实测 2761.59 vs 30.42，91 倍）；无亏损日/无回撤时 `sortino/calmar = 0.0`（与"最差"不可区分）；无亏损时 PF 记 `999.0` 哨兵；`total_return` 以**首日收盘**为基（漏掉 r₁，`services/manual_trade.py` 自行补一个 1.0 点、新栈未补） | **记录在案** → 待决策 R5-D-4（共享函数，改动会重述所有存量批测数字与已发布 sample 数） |
| R5-P3-6 | `src/research/runs.py:40-46` + `research_runs` DDL | `research_runs` **无 role/arm 列** → 候选腿与对照腿在台账/manifest 上不可区分（两条都是 `window_kind='sample'`，manifest 只列 4 个 run 无标签，evidence 不带 run_id，详情页不显示 run id）；目前只能靠未文档化的写入顺序辨认（walk-forward 交错 2×N） | **记录在案** → 待决策（加 `role` 列需迁移 + 两处写入 + 展示） |
| R5-P3-7 | `src/core/indicators.py:108-129`（docstring），消费 `services/market_indicators.py:117` vs `services/dashboard.py:220-226`/`data/indicator_store.py:64-70` | `macd(warmup=False)` 不只掩码不同——DEA 递推种子改在"第一根有效 DIF"，故预热窗**之后**仍有差异（实测 140 根波动序列 max\|Δdea\| ≈ 0.032，按 ≈0.8^k 衰减）→ 同一标的/日在看板迷你图与详情图上可能是两个 MACD | 修：docstring 如实说明（删掉"仅掩码"的暗示）+ 注明跨页面口径差；统一口径属展示面改造，进待决策 |

## 已验证无问题（本轮实跑核对）

1. **存量 `rule_backtest` 未被扰动**：95/95 通过；源码与 `f93031e` 逐字节相同 → 不存在
   新栈回归通道。
2. **`compute_summary` 的算术本身正确**：22 个字段在合成 NAV 上手算逐一吻合（1e-12）。
3. **新栈端到端链在真实 runner 上数字自洽**（本轮新增探针）：临时库 → seed → 提实验 →
   **真 ResearchWorker** → 判定 → confirm → 物化 → HTTP 下载；12 个 summary 值从
   `engine_daily_nav`+`engine_fills` 独立重算**逐位一致**；`delta_*` 是严格恒等式；
   `suggested_verdict` 等于 `suggest_backtest_verdict(evidence)`；物化 `report.json`
   与 HTTP 端点 10 键逐值一致。**不存在的曲线、不存在的形状断言都没有**。
4. **已发布验收产物可由自身字节复算**：4 条策略的 total/annual/mdd/sharpe/sortino/calmar
   在 4 位有效数字内复现（最大相对差 4.9e-3）。
5. **跨栈 ATR/TR 一致**：`_precompute_atr` 并非重新推导——它调用 `core.indicators.atr`
   且列内 ffill；连续面板上 `max|panel − core| = 0.0`。
6. **`trend_score_cross` 的生产指标忠实**：`trend_daily` 与 `calculate_trend_score_series`
   在真实标的上重算（7796 行）`max|diff| = 0.0`；窗口锚定敏感性 ≤ 1.4e-05（远低于任何阈值）。
7. **面板 MACD 信号与 core 原语一致**（`max|dif| = max|dea| = 0.0`）→ `macd_cross@1`
   不是静默重写。
8. **新栈表从不绕过服务面读取**：`src/services|app|core` 中无 `engine_*`/`research_*` 的
   SQL，`sqlite3.connect` 只在 `data/storage/db.py`；`after_update`/冻结门线程语义保持。
9. **凭据面 fail-closed**：`core/env.py` 是唯一读取点；MCP 在 `TREND_MCP_TOKENS` 为空时
   一律 401；`deploy.sh` 生成 `.env` 且 `chmod 600`；`.env`/`*.db`/`exports/` 已 gitignore。

## 待决策点（新增）

1. **R5-D-1（最高优先）**：立即轮换 `yyx` 密码；长期策略（启动缺显式引导密码即拒绝 /
   首次登录强制改密）；是否在 frp 前终结 TLS 并加 `Secure`+HSTS。
2. **R5-D-2**：退化腿的正式语义——"Sharpe→None + 警告"（本轮实现）还是"拒绝该比较
   （inconclusive）+ `degenerate_leg` 警告"？建议后者（彻底阻止噪声进入判定）。
3. **R5-D-3**：因子写入是否必须先物化成功（事务序）还是允许 `pending_rematerialize`
   标记 + 重试 + 留痕？
4. **R5-D-4**：共享 `compute_summary` 的口径（Sortino 下行偏差、`total_return` 含首日、
   零值 vs None、PF 哨兵）——(a) 改共享函数并重述全部数字；(b) 新栈另建 v2 摘要；
   (c) 只记文档不动数字。**建议 (b)**（新栈要正确口径、存量数字不动）。
5. **R5-D-5**：`research_runs` 增加 `role`（experiment/base/plateau_probe）列并在
   manifest/详情页暴露——是否本轮之后排期。
6. **R5-D-6（操作面）**：探针实例化真实 app 时触发了针对**生产库**的启动迁移；与 03:00
   备份对比显示正是 Round 3 的修复集（11 个 `trg_engine_*` 守卫、删 2 个冗余索引、
   verdict 触发器体重建），**零行数差异**。含义：若生产服务自 R3 以来未重启，则引擎子表
   的 append-only 守卫在 17:12 之前**并未生效**——请确认生产是否已重启。
