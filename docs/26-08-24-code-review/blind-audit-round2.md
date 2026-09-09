# 独立盲审 · 第二轮确认（round 2）

- 日期：2026-08-24
- 输入：blind-audit-comparison.md + code-review-report.md（v2）
- 任务：①核对盲审每条发现是否被正确理解/收录；②判断是否还有双方均未发现的实质问题
- 结论先行：**全部条目收录无误，分级降调均接受；第二轮仅再挖出 5 条 P3 级微调，无 P0-P2 级遗漏，覆盖面闭合。**

## 一、收录核对：逐条确认

| 盲审条目 | v2 落点 | 核对结果 |
|---|---|---|
| S1 instruments NameError | P0-3 | 准确（并正确补充 `/api/categories` 同样受影响——比我原文更全）；我提的「删 fallback」选项被如实记录 |
| S2 MCP 无鉴权 | P0-1 | 准确；v2 额外补 calc_stop_loss 烧付费额度向量，属实（server.py:306 无鉴权） |
| S3 登录无防护 | P1-1 | 准确 |
| H1 cookie/绝对过期 | P1-9 | 准确，Secure 做成配置开关的表述比我原文更稳妥，接受 |
| H2 token 明文/明文兜底 | P1-9 | 准确 |
| H3 无用户创建入口 | P1-3 | 准确 |
| H4 deploy.sh 漂移 | P1-7 | 准确（v2 补 `rm -rf` 无确认，deploy.sh:68，亲核属实） |
| H5 copy2 备份 | P2-24 | 准确；**接受降 P2**（一次性脚本已执行，风险仅在重跑时兑现，修复仍要求） |
| H6 Web 指标窗口截断 | P1-4 | 准确 |
| H7 build_sw_tree 死循环体 | P2-6 末条 | 准确，接受 P3 |
| M1 L2 排序 priority_l3 | P2-4 | 见「二、对降调的裁决」 |
| M2/M3/M6/M7/M8/M9 | P2-3/P2-5 | 均准确 |
| M4 overlay 泄漏 DataService | P1-13 | 准确 |
| M5 时区 | P1-5 | 准确，且 v2 补的 intraday_service 11 处 datetime.now() 亲核属实（538,579,668,732,801,807,820,826,867,1010,1016） |
| M10-M16 性能 | P2-15/17/18/19 | 均准确；M10 的 stop_loss 全表扫被单列一行修复（db.py:794 主键查询），接受 P3 定级（数百行表，代价确实微秒级） |
| M17 innerHTML×2 | P1-8 | 准确，v2 另发现 batch_backtest 两处 tooltip 未转义 + market_view 双重转义，抽查 batch_backtest.html:1053/1484 与 market_view.html:1604 属实，为 v2 加分项 |
| M18 401 不统一 | P1-12 | 准确；v2 修正的细节（instruments 有 resp.ok 检查、batch 是 TypeError 红条）亲核属实，优于我的笼统表述 |
| M19 计时侧信道 | P1-9 可选 | 接受 |
| M20/M21/M22/M23 | P0-2/P1-10/P2-23 | 准确 |
| M24-M27 架构 | P2-8/12/10/13 | 准确；P2-8 的克制裁决（DDL 分段 + auth 域顺手迁出）我接受——单人项目全拆确实 ROI 不足 |
| Low 重复实现清单 | P1-14/P1-15 | 全部收录，且 v2 补出第 5 份 `_category_path`（instrument_admin.py:84，亲核属实）与前端止损卡渲染两份**已漂移**（manual_trade.html:309 vs subject_market.html:1146 文案不一致，属实——这是比我「重复」更强的证据） |
| 死代码/运维/测试/文档/卫生各 Low 条 | P2-6/9/16/21/22/25/26/30/31 | 全部收录；v2 补的冗余 import（db.py:1325,1723,1793,2117）、孤儿日志、tushare 镜像、scripts DB_PATH×7 等均亲核属实 |

**无一处误读、无一条遗漏收录、无一处行号错引（我抽查了约 20 处 v2 行号全部吻合）。**

## 二、对降调的裁决

1. **M1（L2 排序字段）→ 接受「疑似字段误用待确认」**，但补充一条加强证据供 owner 决策：`instrument_admin.py:101-117` 的 `category_priorities` 为 L1/L2/L3 三级各自返回独立 priority——priority_l2 这个字段存在的全部意义就是给 L2 排序用；当前 L2 排序改吃「子级 priority_l3 的 min 聚合」使 priority_l2 配置对排序**实际失效**（死配置）。因此误用概率高，但「按最重要子类排」也可能是有意设计，维持 P2-4 待确认定级，不坚持升级。
2. **H5 → P2 接受**（理由见上表）。
3. **R-0（__dev_set_session）**：确认我盲审未见到该端点系工作区回滚所致，非漏审；当前 auth.py 65 行无此端点，亲核一致，闭环无异议。

## 三、第二轮深挖：v2 + 盲审合并清单之外的残余发现（全部 P3 级）

### N1 周期性批量读缺二级索引，且三张行情表各带一个与 PK 完全重复的冗余索引
- 缺索引（读侧）：`load_trend_daily_bulk`（db.py:1778-1790）`WHERE param_set=? AND time>=?` 无法利用 PK(symbol,time,param_set) 前缀 → trend_daily（约 90 万行）全扫，被盘中快照每 5 分钟调用一次（intraday_service.py:581，经 dashboard_snapshot.py:127）；`load_indicator_latest`（db.py:1763-1776）的 `GROUP BY symbol, MAX(time)` 为全 PK 覆盖扫描（约 90 万行），同样 5 分钟级调用（intraday_service.py:577）；`load_market_tail`（db.py:808-822）`WHERE time>=?` 无 time 列索引 → market_data_qfq 全扫。
- 冗余索引（写侧）：`idx_market_data_raw_symbol_time`（db.py:143-144）、`idx_market_data_qfq_symbol_time`（db.py:159-160）、`idx_ex_factors_symbol_time`（db.py:170-171）与各自 `PRIMARY KEY (symbol,time)`（141/157/168 行）完全同列——rowid 表上 PK 已自动建索引，这三个是重复的，白白放大 1M 行表的每次写入（与日更全量重写 P2-17 叠加）。
- 现状评估：单次数十至数百毫秒、5 分钟级频率，**可接受不紧急**；建议：删 3 个冗余索引（零风险），视容量再加 `trend_daily(param_set,time)`、`market_data_qfq(time)` 两个索引。

### N2 看板冷路径与批量 meta 页的全表重查询（记录备查）
- `load_market_dashboard_history`（db.py:824-846）ROW_NUMBER 窗口函数作用于 qfq×metadata 全量 join 后才过滤 rn<=90——冷重建每次全扫 1M 行 join；`routers/batch_backtest.py:97` meta 接口每次页面加载跑 `count_bars_by_symbol()` 全表 GROUP BY。均为冷路径/低频，v2 P2-18 的 COUNT(*) 条目邻近但未含这两条，补记备查，不单独列级。

### N3 标的管理路径重复全表加载（微优化）
- `instrument_admin.py:66-81` `_known_managed_symbols` 一次调用内 `list_instrument_metadata()` 两次（68 行经 `_config_items()`、79 行再一次）外加一次 DISTINCT 全表 `list_market_symbols()`；`instrument_admin.py:120-136` `_next_sort_order` 同样两次全表。仅管理操作路径，代价毫秒级，与 P2-18 的 instruments 列表 N+1 同源，合并处理即可。

### N4 API 测试每用例两次 pbkdf2 拖慢套件（测试基建微优化）
- `tests/api/conftest.py:56-58`：每个 API 测试 `create_user`（哈希一次）+ 登录（校验一次），pbkdf2 20 万次约 0.1s/次 → 每个用例固定 +0.2s。建议夹具内注入预计算哈希（如直接 `hash_password` 一次复用，或把迭代数在测试配置里调低），与 P2-26 的 CI 提速目标一致。

### N5 误导性变量名（纯可读性）
- `intraday_service.py:663` `intraday_ts = result["trend_score"]`——名为时间戳实为趋势值（下一行拼进 `extended_scores`），与真正的 `intraday_ts`（668 行构造的日期）语义冲突，建议改名 `intraday_score`。

## 四、对 v2 独有新发现的抽查验证（反向核对）

v2 有而盲审清单没有的条目，我抽查了最关键的几处，全部属实：
- P0-4 收集错误：tests/unit/test_mcp_stop_mode.py:5、test_mcp_symbol_detail.py:24 顶层 `from trend_mcp import server`，无 importorskip，属实。
- P1-11 批量回测标的未冻结：batch_service.py:387 vs :456 两次 `resolve_batch_symbols`，属实。
- P2-1 引擎 KeyError：engine.py:283-286 的 else 分支 `pd.to_datetime(df["date"])` 在 date/time 俱缺时 KeyError，属实（service.py:340-343 同构）。
- P2-2 报价缓存键不一致：service.py:198-202（原始 symbol 命中）vs :216-221（归一化 symbol 写入），属实，当前上游均归一化故安全。
- P2-23 foreign_keys 未开：db.py:66-76 `_connect` 全文无 `PRAGMA foreign_keys`，属实（manual_trades.user_id、sessions.user_id 外键虚设）。

## 五、最终结论

1. 盲审全部发现已被 v2 正确理解与收录；对全部降调无异议（M1 维持待确认但补强证据）。
2. 第二轮针对 scripts/sizing/索引/测试基建的定向深挖仅再获 5 条 P3 级微调（N1-N5），**没有双方在 P0-P2 级别都遗漏的实质问题**。
3. 覆盖面声明：src/ 68 模块、web/ 9 模板+style.css、scripts/ 14 脚本、tests/ 48 文件、config/、README/CLAUDE/Makefile/pyproject/.gitignore、deploy/run_dev 脚本均已由双方独立覆盖。**无更多可挖掘点，审查闭合。**
