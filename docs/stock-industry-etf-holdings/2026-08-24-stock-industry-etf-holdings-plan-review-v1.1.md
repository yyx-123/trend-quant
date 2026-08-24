# 方案 v1.1 复审意见

- 日期：2026-08-24
- 复审对象：`2026-08-24-stock-industry-etf-holdings-plan.md` v1.1（首部声明已并入 v1 评审意见）
- 前置评审：`2026-08-24-stock-industry-etf-holdings-plan-review.md`（v1）
- **结论：v1 评审的 A1/A3/A4、B1-B6、D1-D5 全部采纳且落实位置正确；A2 经复核是 v1 评审本身的错误，v1.1 的更正如实。本轮仅剩 1 条实施注意项（不阻塞）。方案通过，可进入 P0。**

---

## 1. v1 评审意见落实核对

| v1 条目 | v1.1 落点 | 核对结果 |
|---|---|---|
| A1 `etf_constituents.fetched_at` 本地时间 | §4.2 新增"与前方案的唯一差异"段，明确要求 `datetime('now','localtime')` | ✅ |
| A2 `_category_priority_map` 不存在 | §4.4 改写为指向 `instrument_admin.py:88` + 抽公共函数建议 | ✅ **v1 评审有误，v1.1 正确**（见 §2） |
| A3 tushare 依赖与 pyproject | §10 P0 明确"本仓库无 requirements.txt"，给出 optional 依赖组/手动安装两条路 | ✅ |
| A4 "查名称"按钮不存在 | §6.3 改为"代码输入后自动查询"的准确描述，并补了名称查询失败时 suggest 仍调用的边界 | ✅ |
| B1 迁移刷新 `updated_at` + 重启兜底 | §7.2"实现要求"三条（显式 updated_at / 停服执行 / 人工冒烟） | ✅ 且 `save_instrument_metadata` 的 UPSERT 分支确实写死 `updated_at=datetime('now','localtime')`（`db.py:698-720`，已核实），推荐路径成立 |
| B2 名称唯一性/分隔符/罗马数字规范化 | §4.4 建树前校验两条 + §4.1 规范化两条（Unicode/ASCII、全半角） | ✅ |
| B3 登录墙测试 | §9 新增"登录墙"测试项 + §10 P3 列入 | ✅ |
| B4 不删行语义 + 变更清单口径 | §4.1"删除语义"段 + §8 归属变更清单写明比较口径 | ✅ |
| B5 回补移动清单 → job_runs | §5 待分类回补段末 | ✅ |
| B6 停服迁移 | §7.2 执行环境 + §10 P2"停服正式迁移 → 重启 + 看板冒烟" | ✅ |
| D1 二级类目 11→10 | §1 已改 10 | ✅ |
| D2 `sw_l3_code` 混码注释 | §4.1 注明"消费方须按 source 解释、仅留档" | ✅ |
| D3 调度器落点 | §10 P4 点名 `core/scheduler.py` 的 `start()` | ✅ |
| D4 测试补两条 | §9 罗马数字混写用例 + 含 `-` 拒绝执行断言 | ✅ |
| D5 预览 `hit` 标志 | §6.1 修改 2 已加 | ✅ |

## 2. 关于 A2：v1 评审的错误，更正并致歉

v1 评审依据子代理"全仓 grep 无 `_category_priority_map`"的结论判定该函数不存在。**经本轮直接复核，该函数确实存在于 `src/services/instrument_admin.py:88`**（读类目树返回 `{path: priority}`，`:133` 被 `_build_new_instrument_record` 调用），是此前 grep 范围/方式有误导致的误判。v1.1 的更正（保留函数引用、采纳"抽公共函数供迁移脚本/回补/导入 Job 三处共用"的建议）是正确处理方式，以 v1.1 为准。

## 3. 本轮唯一新增：1 条实施注意项（不阻塞）

**迁移脚本走 `save_instrument_metadata` 时必须传入完整现有行。** 该函数的 UPSERT 冲突分支里，`name / category_l1-l3 / factor_tags / region_tag / priority_l1-l3 / sort_order / source / enabled` 是**硬覆盖**（只有 `stop_atr_mul / risk_budget_pct / asset_type / start_date` 用 COALESCE 保留原值）。迁移脚本若只构造 `{symbol, category_l2, category_l3, priority_*}` 的瘦记录，会把其余字段抹成空值；尤其 `enabled` 缺省为 `True`，会把已禁用标的静默重新启用（当前库 enabled=0 的标的为 0 只，属理论风险，但脚本应对任何库状态都安全）。

正确做法（建议写进 §7.2 实现要求）：迁移第 5 步以 `list_instrument_metadata()` 读出的完整行为基底，只改 `category_l2/l3` 与 `priority_l1/l2/l3` 四个字段后回写。§9 迁移脚本单测可加一条"迁移后 enabled/name/factor_tags 等非类目字段与原值一致"的断言。

## 4. 总结

v1.1 对评审意见的吸收质量很高：不是简单打勾，而是把每条落到了正确的章节（数据模型 §4、数据流 §5、迁移 §7.2、测试 §9、实施步骤 §10 各就其位），且 v1 评审的一处事实错误被正确顶回。**方案通过，可按 P0 → P4 推进**，实施时带上 §3 的一条注意项即可。
