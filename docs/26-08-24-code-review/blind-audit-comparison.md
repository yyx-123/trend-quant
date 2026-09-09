# 独立盲审比对（查漏补缺）

- 日期：2026-08-24
- 盲审方式：独立子代理对整个仓库做同范围审查，**全程禁止阅读本目录与 docs/ 方案文档**，结论完全来自代码本身
- 比对目的：验证主报告覆盖面，找出主报告遗漏；分歧逐条裁决
- 盲审原始输出全文见会话记录；本文件为比对结论（盲审条目编号 → 主报告处置）

## 一、收敛情况总览

盲审 3 个 Critical、7 个 High、27 个 Medium、一批 Low。与主报告 v1 **独立重复发现**的条目（双方各自独立找到，互为印证）：

| 盲审条目 | 主报告条目 | 双方结论一致性 |
|---|---|---|
| S1 instruments 潜伏 NameError | P0-5 | 完全一致（行号、触发条件、修复建议均同） |
| S2 MCP 无鉴权 + 注释矛盾 | P0-2 | 一致；盲审补充 intraday_dashboard 是未鉴权 CPU/额度 DoS 向量（采纳进 v2） |
| S3 登录无限流无审计 | P1-1 | 一致；盲审补充 4xx 不记日志使爆破不可见（采纳） |
| H1 cookie Secure/无绝对过期 | P1-3 | 一致；盲审补充绝对过期与「登出全部会话」建议（采纳为可选） |
| H2 token 明文落库 + 明文兜底 | P1-3/P1-2 | 一致 |
| H4 deploy.sh 漂移 | P1-6 | 一致；盲审补充 User=root、nginx/frp 矛盾、bundle/GitHub 分发矛盾（采纳） |
| H5 copy2 备份 WAL 活库 | P0-4 | 一致（v2 采纳评审 2 降为 P2） |
| H7 build_sw_tree 死循环体 | P1-7 | 一致（v2 采纳评审 2 降 P3） |
| M5 时区混用 | P1-13 | 一致；盲审补 rule_backtest.py:72-73（采纳） |
| M10 连接/N+1/止损全表扫 | P1-9/P1-10 | 一致；盲审补 instruments.py:429-431 列表接口逐标的一次连接（采纳） |
| M12 热路径 iterrows | P2-7 | 一致；盲审补 metrics.py:16、intraday_service.py:477,834（采纳） |
| M17 两处 innerHTML | P1-5 | 一致 |
| M18 401 处理不统一 | P1-14 | 方向一致（v1 的 instruments 子条错误已由评审 1 修正） |
| M19 登录计时侧信道 | （评审 2 可选 12） | 一致，v2 收为 P3 |
| M20 MCP 密码参数 | P0-3 | 一致 |
| M23 GET logout/CSRF | P1-4 | 一致 |
| M24 db.py 上帝类 | P2-1 | 一致（v2 采纳评审 2 的克制方案） |
| M26 看板缓存双实例 | P2-3 | 一致 |
| 无自动备份调度 | P2-15 | 一致（v2 升 P1） |
| 无告警通道 | P2-14 | 一致 |
| 无 CI/无覆盖率门槛 | P2-18 | 一致 |
| 调度层零测试 | P1-21 | 一致 |
| README/CLAUDE 工具数漂移 | P2-22 | 一致（盲审补 CLAUDE.md:30，采纳） |
| 根目录无 marker 测试 | P2-17 | 一致 |
| 重复实现清单（esc/_category_path/_number/_date_span/symbol_to_code 等） | P1-17/P1-19 | 高度一致；盲审补 safe_float×3、SH↔SS 转换×3、报价规整×2、RSI avg 组件×2、_assign_strength/_DISPLAY_DAYS（全部采纳进 v2 重复实现表） |
| 仓库卫生（zip/.coverage/pycache 残骸/BOM） | P2-23 | 一致；盲审补 src/engine/ 残骸与 provider_utils.py BOM（采纳） |

收敛度：盲审 60+ 条发现中约 6 成与主报告完全重合，无任何「主报告有、盲审证明为假」的条目（盲审未发现 P0-1 系因其审查时端点已从工作区消失，见 CHANGELOG 事实性修正 1）。

## 二、盲审新发现（v1 遗漏）及处置

| 盲审条目 | 处置 | 主审查人亲核 |
|---|---|---|
| H3 无用户创建入口，全新部署无法登录 | **采纳为 P1** | 属实：create_user 全仓零调用方（src/scripts grep 仅 tests 使用）；deploy.sh 不建用户不配 .env |
| H6 Web 日 K 指标窗口截断（与 MCP 已修 bug 同源） | **采纳为 P1** | 属实：market_view.py:288-290 先 tail(limit) 后算指标；server.py:223-227 注释记载 MCP 侧已修 |
| M4 build_intraday_overlay 泄漏 DataService | **采纳为 P1** | 属实：intraday_service.py:204 自建后全函数无 close；stop_loss.py:82-88 有正确对照 |
| M1 L2 排序用 priority_l3 | **采纳为 P2（疑似字段误用）** | 属实但降调：L2 行的 priority_l3 是子级 min 聚合值，排序确定；字段意图不符，建议确认意图后改 priority_l2 |
| M2 EOD 加权 vs 盘中均值的聚合口径不一致 | **采纳为 P2** | 属实：dashboard.py:83-112 成交额加权 vs intraday_service.py:842-846 简单平均 |
| M3 盘中全 None 聚合出 NaN/非法 JSON | **采纳为 P2** | 方向属实：return_1d 初始化 None（intraday_service.py:736），843 行 mean 无防护；措辞按「全 None 组触发异常或 NaN 序列化风险」表述 |
| M6 standardize_ohlcv 缺时间列伪造 now() | **采纳为 P2** | 属实：provider_utils.py:55 |
| M7 快照首次读库失败永久不重试 | **采纳为 P2** | 属实：dashboard_snapshot.py:52-60 |
| M9 record_industry_sync_job 恒 success | 采纳为 P3 | 属实：stock_industry.py:399 |
| M11 日更全历史重写（raw 层） | 采纳并入 v2 日更性能条目 | 属实：service.py:348-352 合并后全量 save_history；与 qfq 重物化叠加 |
| M13 get_strategy_config 逐次查库 | 采纳为 P2 | 属实：strategy_config.py:45-65 无缓存 |
| M14 批量回测每格双事务 | 采纳为 P3 | 属实：batch_service.py:533+535 |
| M16 result_full 常驻内存无读取方 | 采纳为 P2 | 属实：rule_backtest.py:176-177 |
| M21 登录墙边缘（/api 无尾斜杠、startswith 前缀） | 采纳并入 P1-4 | 属实：main.py:331,339 |
| M25 模块级单例固化测试替身风险 | 采纳为 P2 | 属实：rule_backtest.py:20 + market_store.py:11-16；main.py:59-63 注释自警 |
| M27 cwd 相对路径依赖 | 采纳为 P2 | 属实：main.py:14,64、settings.py:45、app_logger.py:8 |
| 死代码：benchmarks 零调用 API/分钟 K 死链路/fetch_trading_calendar/DIVIDEND_CHECK_BARS/YAML fallback/plan:starter | 全部采纳（P3） | 逐一 grep 确认零调用；config/rule_strategies 目录不存在 |
| 测试：长期红套件未 xfail、/mcp 豁免无测试、test_subject_market 偶然再导出依赖 | 全部采纳（P2 测试节） | 属实：CLAUDE.md 自述 2 个 pre-existing failures；test_subject_market.py:8 从路由导入 |
| 运维：硬编码回填起点 2020-01-01、JobManager 中断无标记、tushare 镜像私有属性 hack | 全部采纳（P2/P3） | 属实：instrument_jobs.py:432,628；batch 有 mark_interrupted 而三个 Manager 没有 |
| dev 依赖未声明（盲审「无依赖锁定」+评审 2「无 dev 组」） | 采纳为 P1 | 属实：pyproject 无 dev 组、无 lock/requirements；async 测试经查走 unittest.IsolatedAsyncioTestCase 正常运行，asyncio_mode 为死配置 |

## 三、分歧与裁决

1. **P0-1（__dev_set_session）**：盲审未发现该端点——因其审查时工作区已回滚。裁决：非盲审遗漏、非主报告臆造，系审查期间代码变动；v2 以 R-0 闭环条目处理（见 CHANGELOG）。
2. **M1 排序字段**：盲审定性「两处同错（复制粘贴）」，主审查人降调为「疑似字段误用」——L2 行的 priority_l3 来自子级 min 聚合，行为确定但语义可疑，需 owner 确认意图。
3. **分级风格**：盲审用 S/H/M/L，主报告用 P0-P3；映射关系 S→P0、H→P1、M→P2、L→P3，v2 已按此归并。

## 四、结论

盲审代理与主审查人在「是否存在可再挖掘的优化点」上达成一致：**盲审的全部新发现已录入 v2；双方对彼此清单无反驳项；盲审未在主报告之外发现任何新的 P0 级问题。** 覆盖面可认为已闭合（后端 68 模块 / 前端 9 模板 + style.css / 13 脚本 / 48 测试文件 / 配置与文档均被双方独立覆盖）。
