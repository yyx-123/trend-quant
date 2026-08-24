# 《股票行业分类体系重建（申万2021）+ ETF 前十大重仓股导入 方案 v1》评审意见

- 日期：2026-08-24
- 评审对象：`docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md`
- 评审方式：对方案中的关键断言逐条对照代码与本地库实测核实
- **结论：方向与整体设计同意，按 P0→P4 推进没有异议。但有 4 处与代码现状不符的事实性错误必须修正（其中 2 处会直接写坏数据），另有若干风险点需要补强。修正 §A 后即可进入 P0。**

---

## A. 必须修正（与代码现状不符 / 会写坏数据）

### A1. `etf_constituents` 不能"一字不改"沿用前方案 —— 时间戳默认值违反项目本地时间约定

方案 §4.2 称前方案 §4.1 表结构"一字不改"沿用。但前方案 DDL 是：

```sql
fetched_at TEXT DEFAULT CURRENT_TIMESTAMP   -- UTC 时间
```

项目在 commit `42ccaa1` 已统一全库时间戳为本地时间（`datetime('now','localtime')`，INSERT 显式写时间列不再依赖默认值）。本方案自己的 `stock_industry` 表（§4.1）已经用了 `datetime('now','localtime')`，是对的；但 `etf_constituents` 若照抄前方案就会混入 UTC 时间，新鲜度判断（期次距今月数）会差 8 小时起跳。

**改法**：`fetched_at TEXT DEFAULT (datetime('now','localtime'))`，或对齐现有代码风格在 INSERT 时显式写入。方案 §4.2 的"一字不改"表述需要删除并注明此差异。

### A2. `_category_priority_map` 不存在 —— priority 重算逻辑的实际位置在 `instrument_admin.py`

方案 §4.4 与 §7.2 步骤 5 都写"复用 `_category_priority_map` 同款逻辑"。全仓 grep 无此函数：priority 是**写入时由调用方算好传入**的，逻辑在 `src/services/instrument_admin.py:123-169`（`_build_new_instrument_record` 自动推 priority 那段），db 层只存不算。

**改法**：方案改指向 `instrument_admin.py`；实施时建议把这段 priority 计算抽成独立可复用函数（迁移脚本、待分类回补、ETF 导入 Job 三处都要用），否则三处各抄一份又会平行演化（架构评审 §P1-7 刚批评过这种重复实现模式）。

### A3. tushare 不是项目依赖，且仓库没有 `requirements.txt`

前方案 §5.4 写"`requirements.txt` 中加入并注释说明"——本仓库没有 requirements.txt，依赖在 `pyproject.toml`（tickflow 在 `pyproject.toml:18`），tushare 目前**完全未安装**（pyproject 无、src 无 import）。季度脚本跑不起来时这不是文档问题而是硬阻塞。

**改法**：方案明确依赖安装方式——在 pyproject 加 optional/dev 依赖组（如 `tushare = ["tushare"]`）或写明"脚本运行时手动 `pip install tushare` 到本地 .venv"。部署即本地（127.0.0.1:8000 的 .venv），装一次即可。

### A4. "查名称"按钮不存在 —— 名称是代码输入后自动查询的

方案 §6.3 写"添加表单在「查名称」成功后再调 suggest"。实际 `web/templates/instruments.html:90` 名称输入框是 readonly、placeholder"输入代码后自动查询"，JS 在代码输入后自动调名称接口（`instruments.html:1080-1102`），没有独立按钮。

**改法**：suggest 调用挂在自动名称查询的成功回调之后即可，交互语义不变，但方案表述要改对，否则实施者会去找一个不存在的按钮。另外补一个边界：名称查询失败（TickFlow 未覆盖/网络失败）时 suggest 仍应照常调用——行业分类在本地表，不依赖名称查询成功。

---

## B. 需要补强的风险点

### B1. 迁移必须显式刷新 `metadata.updated_at`，且建议跑完重启服务

方案 §7.1 依赖"`updated_at` 变化 → RevisionCache 自动失效重建"。这个链条比方案设想的脆弱：

- 看板 revision 的计算（`get_market_dashboard_revision`，`db.py:680-693`）只看 `MAX(time)+COUNT+metadata.updated_at` —— **如果迁移脚本 UPDATE 时只改 category/priority 字段、不显式写 `updated_at`，看板会静默继续读旧分组缓存，无任何报错**。
- 架构评审（`docs/architecture-review-2026-08-01.md` P0-3）已点名这个 revision 机制不感知内容变化、且 `services/dashboard.py` 的 RevisionCache **零测试**。

**要求**：① 迁移脚本与待分类回补的所有 metadata UPDATE 显式写 `updated_at = datetime('now','localtime')`；② 迁移在停服或低峰执行（本地库就是生产库），跑完重启服务兜底；③ 迁移后人工冒烟：看板分组确实变成新行业树，而不是"校验通过但页面还是旧的"。

### B2. 类目树建树前必须做名称唯一性与字符校验

两个具体隐患：

- **剥罗马数字后缀后撞名**：§3.2 统一剥掉 `Ⅰ/Ⅱ/Ⅲ` 后缀。若同一申万一级下两个二级行业剥后缀后同名，`path`（`股票-电子-xxx`）直接撞车主键。建树脚本必须先校验「同 L1 下去后缀 L2 名唯一」，撞名时保留后缀消歧并告警，而不是 INSERT 失败 halfway。
- **类目名含路径分隔符**：实际库的 path 分隔符是 `-`（实测：`股票-先进制造-军工电子`），而现有类目名里已出现含 `/` 的名字（`股票-先进制造-检测设备/仪器仪表`）。申万行业名虽不含 `-`，但建树脚本应断言名称不含 `-`，防止静默破坏 path 解析。
- **罗马数字字符集**：剥后缀要同时处理 Unicode 罗马数字（`Ⅱ` U+2161）和 ASCII 写法（`II`），两个数据源（tickflow universe 名 vs tushare index_classify）的字符习惯未必一致——这也正是 §4.1"两源按名称对齐"的前提，对齐前必须先统一规范化，否则同一行业在两边对不上、merge 逻辑失效。

### B3. 新增接口要显式过登录墙，并补未授权测试

全站 cookie session 登录墙已于 2026-08-24 落地（`src/app/routers/auth.py` + `tests/api/test_auth_wall.py`）。方案新增的 `suggest-category`、`etf-constituents` 预览/导入三个接口挂在 instruments 路由下大概率自动被墙覆盖，但方案与测试计划都未提及。

**要求**：测试计划加一条——未登录访问三个新接口返回 401/重定向（对齐 `test_auth_wall.py` 写法）。ETF 导入是写操作，这一条不能省。

### B4. `stock_industry` 全量覆盖的删除语义需要写清楚

§4.1 说 `tushare_sw2021` "全量覆盖"，但没说明：**最新一期 `index_member_all` 拉不到的股票（退市、被调出指数），本地旧行删不删？** 建议明确"不删、保留旧行"——对在管标的无害（metadata 不自动跟随），且避免退市股行业信息丢失。同理 §8 的"归属变更清单"应写明比较口径：`stock_industry`（新拉取）vs `instrument_metadata`（当前类目），在同步脚本汇总输出，便于用户决定去留。

### B5. 待分类回补是"自动改看板分组"，需要与 §8 的保守原则对齐表述

§8 对申万官方调归属采取"不自动改、出清单人工确认"，但对「待分类回补」（§5）是自动批量改类目。两者语义其实一致（待分类是占位符不是用户选择），但**迁移后第一次回补可能一次性移动几十只股票**，看板分组会突变。建议：回补同样输出移动清单（哪只从待分类去了哪个行业），写进同步脚本汇总与 `job_runs`，可追溯即可。

### B6. 本地库即生产库 —— 迁移的执行环境要写明

`data/trend_quant.db` 就是 127.0.0.1:8000 在用的生产库（另有 raw→qfq 迁移未完成等既知历史遗留）。方案 §7.2 有备份 + dry-run + 校验三重兜底，是对的；但应补一句执行前提：**迁移与首次 tickflow/tushare 全量同步期间服务如何处理**（建议停服执行，或至少确认迁移期间看板/回测不在跑），避免迁移中途服务读到半新半旧的类目树。

---

## C. 已核实属实的关键断言（供存档，勿再重复核查）

| 方案断言 | 核实结果 |
|---|---|
| 类目树三级、`instrument_categories` 有 path/level/priority、`instrument_metadata` 有 l1/l2/l3 + priority_l1/l2/l3 | ✅ `db.py:173-205` |
| 看板 SQL 硬要求三级类目非空、空类目自然不显示 | ✅ `db.py:782-785`（INNER JOIN + 三级非空过滤）+ `core/display.py:61-74` `filter_fully_classified`（盘中/MCP 链路共用） |
| 批量回测只按 l1 过滤，迁移不影响过滤 | ✅ `rule_backtest/batch_service.py:96-106` 只匹配 `category_l1`；历史结果快照存 l1/l2/l3 不迁移，与前例一致（`db.py:345-381`） |
| MCP 按类目名搜索标的 | ✅ `trend_mcp/server.py` `list_instruments` 的 `category` 参数匹配 L1/L2/L3 |
| 策略/交易/止损/风控对类目零依赖 | ✅ 抽查无类目引用 |
| 存量 275 只在管股票 | ✅ 实测 enabled=1 且 l1='股票' 恰为 275 |
| `migrate_category_simplify.py` 可作模板（备份/dry-run/校验） | ✅ 三项齐备（备份至 `data/backups/`，`:87` dry-run，`:178` 校验） |
| tickflow 已是依赖且 starter key 可用 | ✅ `pyproject.toml:18` + `provider_tickflow.py` |
| path 前缀 `股票-%` 的写法 | ✅ 实测 path 形如 `股票-先进制造-军工电子`，分隔符确为 `-` |

---

## D. 小问题（不阻塞，顺手改）

1. **§1 背景数字小误差**："11 个二级类目"实测为 **10** 个（树节点与 metadata distinct 均为 10：科技硬件 123 / 新材料与化工 56 / 先进制造 24 / 软件与互联网 22 / 新能源 15 / 电力设备 14 / 大金融 9 / 医药健康 5 / 大消费 5 / 汽车与交通 2——其余数字全部吻合）。
2. **`sw_l3_code` 一列混存两套 code 体系**（tushare `850xxx.SI` vs tickflow 6 位内部码），消费方必须先看 `source` 再解释。建议列注释写明，或拆 `sw_l3_code_tushare` / `sw_l3_code_tickflow` 两列——反正该列目前没有功能消费方，怎么简单怎么来。
3. **P4 挂调度器的落点**：`core/scheduler.py` 的 `start()`（`:32-41`）目前只挂每日更新与盘中快照两类 job，加月度任务需要动签名/配置（`CronTrigger(day=...)`）。改动很小，但建议在 P4 里点名这个文件，免得实施时再找。
4. **测试计划可补两条**：① tickflow universe 名解析的测试加上"Unicode/ASCII 罗马数字混写"用例（对应 B2）；② 迁移脚本测试加"类目名含 `-` 时拒绝执行"的断言。
5. **§6.1 修改 2 的预览字段**：`resolved_category` 建议同时返回 `hit` 标志，UI 对待分类行做置灰/标黄，与 §6.3 手动添加的提示样式一致。

---

## E. 与 2026-08-09 回滚的关系（已澄清，无阻塞）

2026-08-09 用户曾要求整体回滚未提交的"ETF 前十大权重股季度快照"实现（旧实现代码已全删，当前 src 零残留，已核实）。本次是用户自己重新提出该方向并逐条确认了 4 项决策（方案开头所列），**回滚的是旧实现而非概念**，本方案不构成"擅自复活已否决功能"。

唯一遗留疑问：上次回滚用户未说明原因。本方案与旧实现的最大差异正是用户这次自己点的菜（申万分类取代"ETF权重股-综合"默认类目、tickflow 免费源先行），方向上已规避。若记得当时不满意的点，值得在实施前对一句，避免重蹈。

---

## F. 总结

| 类别 | 数量 | 处理 |
|---|---|---|
| A 必须修正 | 4 | 修正文档表述 + 实施时规避（A1/A2 直接影响数据正确性） |
| B 风险补强 | 6 | 落入实施步骤与测试计划（B1/B2/B3 建议写成验收条件） |
| D 小建议 | 5 | 顺手 |
| 总体判断 | — | **通过（修正 §A 后），可按 P0→P4 推进** |

设计本身的亮点值得保留：数据源调研扎实（tickflow universes 的申万三级覆盖是实测结论而非推测）、运行时零在线依赖符合项目 DB 优先的新鲜度约定、待分类桶 + 回补机制闭环、迁移三重兜底沿用已验证过的模板。主要风险不在设计而在执行细节——尤其是时间戳约定、缓存失效、名称规范化这三处"静默出错无告警"的点，已在 §A/§B 给出具体要求。
