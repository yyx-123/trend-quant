# 复审意见（review-2，第二轮：v1 → v2）

- 复审日期：2026-08-25
- 复审对象：`code-review-report.md` v2 + `CHANGELOG.md`
- 复审方式：对照我第一轮（review-2-arch.md）的「必须修改 4 项 / 建议修改 10 项 / 可选 5 项 / 新发现 6 项」逐条核验 v2 正文与行动清单；并对 v2 修订中新引入的条目（R-0、P0-1、P1-3、P1-4、P1-13、P2-3、P2-4、P2-5、P2-10、P2-12、P2-13、P2-17、P2-19 等）打开代码抽查证据真实性。

---

## 一、「必须修改」4 项落实情况

### 1. 行动清单补漏 + 重排 —— **已落实**

- 补漏：v1 无行动行的 P1-3（会话硬化）→ v2 P1-9 → 行动 #14；P1-4（CSRF/logout）→ v2 P1-10 → 行动 #14；P1-6（deploy.sh）→ v2 P1-7 → 行动 #13。P1-12（401 封装）→ 行动 #18 并正确标注「依赖 #16」。
- 重排：备份升 P1 提前为行动 #5；H1-H3 测试为行动 #10，排在前端公共 JS（#16）、死代码清理（#22）等所有重构之前，且条目内文明示「先补测试再重构」——正是我 4.2 表的核心主张。
- 证据：v2 第十一节（439-474 行）共 32 行，级别/成本/条目回链齐全。

### 2. 分级调整 —— **已落实**

CHANGELOG「分级调整」表与正文标注一致：迁移脚本备份 P0→P2（P2-24，正文注明「v2 自 P0 降 P2」）；无定时备份 P2→P1（P1-2，注明「v2 自 P2 升级」）；build_sw_tree 死循环体 P1→P3（P2-6 末尾）；stop_loss 全表扫 P1→P3（P2-6，注明降级理由与我的原判一致：表数百行、微秒级）；报价键 P1→P2（P2-2）；2s 轮询 P1→P2（并入 P2-20，innerHTML 子问题并入 P1-8 XSS 条目——正是我建议的去重方式）。

### 3. gitignore 例外 + 公共 JS 两个前置 —— **已落实**

- `.env.example`：P2-13 正文写明「`.gitignore:16` 的 `.env.*` 会把它一并忽略（git check-ignore 实测）——行动必须包含 `!.env.example` 例外」；行动 #21 同步包含。
- 公共 JS 前置：P1-15 正文与行动 #16 均列入两个前置（asset_version 扩展为跟踪所有静态资源；止损卡/postJson 口径裁决是产品决策）。且 P2-27 把 asset_version  stale 问题正确定性为「bug 级 + P1-15 的前置」，交叉引用闭环。

### 4. P1-13 补 jobs.py 时区证据 —— **已落实**

v2 P1-5（v1 P1-13 + P2-6 合并）证据清单含 `core/jobs.py:63`、`main.py:167,175`、`routers/rule_backtest.py:72-73`、intraday_service 11 处。我亲核：`jobs.py:63 today = date.today()`、`main.py:167` 与 `:175` 两处 `_date.today()`——**行号与描述全部精确**。

---

## 二、「建议修改」10 项落实情况

| # | 建议 | 结论 | 证据 |
|---|---|---|---|
| 5a | dev 依赖未声明升 P1 | 已落实 | P1-6（新条目，注明「评审 2 指出根因」），行动 #6 |
| 5b | chinese_calendar 年度边界 | 已落实 | P2-30 末条（含「每次重启触发无效 force 补跑」的完整推理链），行动 #30（1 小时） |
| 5c | 并发专项 + 正面结论 | 已落实 | 总体评价「并发纪律总体良好」四条正面证据 + P2-16 两条残留风险 |
| 6 | 聚合合并两阶段 | 已落实 | P1-14 末段：阶段一抽纯函数 / 阶段二以双实现一致性测试为守门员 / 允许停在阶段一，原文采纳 |
| 7 | qfq 增量拆单行 + 边界 | 已落实 | P2-17（含「新 bar 日期 > max(ex_date) 才 append，回补历史必须全量重写」边界、成本改估半天、test_adjustment.py 兜底），行动 #19 独立成行 |
| 8 | 备份 keep=3 + 异机周备 | 已落实 | P1-2：「keep=3（约 8.8GB 磁盘，keep=7 的 20GB 预算不现实）+ 每周一份异机/云盘 + 恢复演练」 |
| 9a | P2-16 拆条、foreign_keys 单列 | 已落实 | v2 P2-23 拆为 4 子项，foreign_keys 标注「数据正确性问题，单列」 |
| 9b | P0-6 拆出 pytest-asyncio | 已落实 | P0-4 只留收集失败；asyncio_mode 死配置并入 P1-6，且查明 async 测试实际走 `unittest.IsolatedAsyncioTestCase`（比我的处置建议更精确） |
| 10 | CI 单矩阵 + 前置 dev 依赖 | 已落实 | P2-26：「单矩阵即可……前置依赖 P1-6；不想维护 Actions 可用 pre-push hook，任选其一」 |

「可选」5 项亦全部落实：is_trading_time（P2-31，P3，含改名建议）、登录计时侧信道（P1-9 可选段，定性「单人系统危害极低，记录备查」）、依赖上界（P1-6「可选」）、migrate_raw_qfq 补测标注可选（附录 A 末条）、恢复路径正面结论（总体评价第 15 行）。

**未采纳项**：无。CHANGELOG「未采纳/保留分歧」两条均不涉及我的意见（一条是对评审 1 的反驳、一条是对盲审 M1 的降调表述），我复核后认为主审查人的处置合理（见三、P2-4）。

---

## 三、v2 新引入内容的证据抽查（防修订引入新错误）

逐项打开代码亲核，**全部属实，行号精确**：

| 条目 | 抽查结果 |
|---|---|
| R-0 端点已消失 | 属实。全仓 grep `dev_set_session` 零匹配；auth.py 现 65 行；基线声明（含「工作区改动被回滚」）写在报告头部，时间线自洽 |
| P0-1 行号 | 精确。main.py:312-313 `_EXEMPT_PREFIXES = ("/static", "/mcp")`；server.py 工具定义 99/124/195/306/348/420/462 逐一命中 |
| P1-3 无用户创建入口 | 属实。`db.py:1428 create_user` 在 src/scripts 全仓零调用方（grep 确认，仅 tests 使用） |
| P1-4 截断窗口算指标 | 属实。market_view.py:288-290 先 `data.tail(limit)` 后算指标；server.py:223-226 注释明确记载「先截断再算会让数值依赖请求窗口——旧的窗口截断 bug」，MCP/Web 口径分裂论证成立 |
| P1-13 DataService 泄漏 | 属实。intraday_service.py:204 `ds = data_service or DataService()`，全函数无 close/finally；stop_loss.py:82-88 有正确 close 对照 |
| P2-3 伪造当前时间 | 属实。provider_utils.py:55 缺时间列时 `pd.Series([datetime.now()] * len(data))` |
| P2-4 priority_l3 疑似误用 | 证据属实（dashboard.py:272 排序 key 用 `priority_l3`，而 143-144 行显示 priority_l2 存在且已聚合）。主审查人把盲审的「复制粘贴错误」降调为「疑似字段误用，建议确认意图」（CHANGELOG 未采纳段）——**我同意这个降调**：L2 行的 priority_l3 是其子级 min 聚合，排序行为确定，是否语义错误取决于设计意图，审慎表述更专业 |
| P2-5 聚合口径/NaN/快照不重试 | 三子项全部属实：intraday_service.py:843 `float(rows_df["return_1d"].mean())` 无 dropna；dashboard_snapshot.py:52-60 `_snapshot_loaded=True` 先置位、异常后 None 被永久缓存 |
| P2-10 注释矛盾 | 属实。server.py:88 注释「shared RevisionCache from services.dashboard」与 :91 新建实例矛盾；subject_market.py:20 确为另一实例 |
| P2-12 单例固化 get_db | 属实。rule_backtest.py:20 模块级 `service = RuleBacktestService()`；market_store.py:11-16 `_get_db` 首次调用即永久固化 |
| P2-13 cwd 依赖 | 属实。main.py:14 `load_dotenv()`、settings.py:45 `Path("config/app.yaml")` 均为 cwd 相对 |
| P2-17 raw 全量重写 | 属实。service.py:348-352 existing+fetched 合并后整段 save_history |
| P2-19 result_full | 属实。rule_backtest.py:176 存 full、注释自承「future on-demand detail endpoints」 |

两个不构成错误的表述余量（记录备查，不要求修改）：
- P2-5 第二子项「产出 NaN 或抛异常」——实测全 None 列 mean 产出 NaN + RuntimeWarning（代码已有 `"return_1d" in rows_df` 守卫列存在性，不守卫全 None）；「或抛异常」是冗余但无害的兜底表述。
- P1-2 主库体积 2.94GB vs 我 du -sh 测得 2.8G——块大小与字节数口径差异，非错误。

---

## 四、我的新发现 6 项的吸收情况

| 新发现 | 结论 | 落点 |
|---|---|---|
| jobs.py:63 本地时区门控 | 已吸收且行号精确 | P1-5 + 行动 #12 |
| chinese_calendar 年度边界 | 已吸收（含我推理的 catchup 无效补跑链） | P2-30 + 行动 #30 |
| dev 依赖根因 | 已吸收并升 P1 | P1-6 + 行动 #6 |
| 并发专项（正面 + 两残留） | 已吸收 | 总体评价 + P2-16 |
| 设计容量结论 | 已吸收（含「2000 标的触发条件」的量化判断） | P2-14 |
| .env.example × gitignore 冲突 | 已吸收（正文 + 行动双重写入） | P2-13 + 行动 #21 |

---

## 五、结论

**达成一致。** 我的必须修改 4 项、建议修改 10 项、可选 5 项、新发现 6 项全部落实；v2 修订新引入的 14 个条目经逐项亲核证据全部属实、行号精确；CHANGELOG 的两条「未采纳」均不涉及我的意见且处置合理。v2 可作为终版。

非阻塞尾巴（不要求修改，供执行阶段参考）：
1. 轮询退避/可见性暂停（P2-20 的建议部分）在行动清单中仍无独立行动行——建议执行时并入 #16/#18 的验收标准，避免再次漏项。
2. v2 全量重编号后，review-1/review-2 中的 v1 条目号与 v2 不再对应，CHANGELOG 的分级表是唯一映射——后续引用请以 v2 编号为准。
3. P1-3（create_user 零调用方）与 P0-3（instruments NameError 降级路径）叠加意味着「全新部署」是双重断裂，行动 #7 与 #3 建议安排在同一次部署演练中验证。
