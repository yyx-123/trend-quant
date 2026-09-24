# Round 3 修复清单（loop-review-glm53f）

> 对应审查报告：`round3-review.md`。状态：**CLOSED**（2026-09-25）——
> R3VA 首轮 FAIL（event 接线缺失 + meta 声明不符）→ 整改（1 行合并 + 五槽 meta
> 载入期拒绝 + AST 钉）→ **R3VA_REVERDICT: PASS**；R3VB（P3 批 13 项 + 回归）
> **R3VB_VERDICT: PASS**。最终全量回归：1495 passed / 1 failed（已知 Windows
> flake）/ 6 skipped。

## 修复项（P2×2 + P3×12 + 文档订正）

| 项 | 修复落点 | 验证 |
|---|---|---|
| R3-P2-1 meta 跨槽空洞 | `slots/execution.py::register_meta_modules` 七槽注册（universe/rank/sizing/portfolio_risk/execution 槽为显式拒绝工厂——载入合法、运行 loud）；`strategy.py::_validate_binding` 取消 meta 跨槽豁免（`spec.slot != slot` 一律拒） | tests/unit/test_module_behaviors.py + test_portfolio_config.py 回归全绿；跨槽引用 parse 即拒 |
| R3-P2-2 布尔字符串静默 False | `registry.py::validate_params` boolean 分支：字符串仅认 true 集合/false 集合，其余 ValueError；非 bool/str 类型拒绝 | `use_exit: "bogus"` 现在报错；"TRUE"/"false" 等合法形态不变 |
| R3-P3-1 (F1) benchmark 死校验 | `event.py`：bm_close 全 NaN → raise ValueError（不再静默剔除全部事件成假 inconclusive） | 集成路径由评估测试覆盖 |
| R3-P3-2 (F2) 流动性过滤静默失效 | `_common.py::load_eval_panel`：pre_idx<20 时经新增 `warnings_out` 通道产出警告；event/bucket runner 接线进 evidence warnings | tests/integration/test_research_evaluations.py 全绿 |
| R3-P3-3 (F3) distribution 全池口径未声明 | distribution runner 声明"全市场池化（§6.5.4）"入 warnings；bucket 字面量 1e8 → DEFAULT_MIN_AMOUNT20 常量 | 代码复核 |
| R3-P3-4 (F4) 垫片不足 200 交易日 | `_common.py` 垫片 300→320 自然日 | 回归全绿（无测试钉死 300） |
| R3-P3-5 (F5) er_10 伪 0 | `bucket.py::_feature_matrix` er 分支加"最近 11 行 close 全有限"有效性掩码（不触存量 core/indicators） | 合成场景：IPO warmup 行 NaN 化 |
| R3-P3-6 (F6) 去重键口径 | event.py 注释声明 (symbol, 事件日) 不含 kind 的统计无差性 | 注释 |
| R3-P3-7 数值参数域 | validate_params：numeric 拒 bool；integer 拒非整值 float | 探针场景（risk_budget_pct: true / atr_period: 12.7）现报错 |
| R3-P3-8 target_weight 缺省显式化 | sizing.py schema：mode default=equal | YAML 载入回归 |
| R3-P3-9 window_kind 枚举 | service.py L3 侧枚举校验（sample/holdout/plateau_probe） | 非法值 ValueError |
| R3-P3-10 engine_runs 守卫 | db.py：trg_engine_runs_guard_update（白名单=status/finished_at/error；DROP+CREATE 传播存量库；置于建表语句之后——首版误置于建表前，测试当场抓住已修） | 触发器测试沿用既有模式 |
| R3-P3-11 模块装载幂等 | modules.py：register(replace=True) + try 逐条隔离 | 启动回归 |
| R3-P3-12 retired 线拒绝新版本 | library.py::add_version：retired_at 非空 → LibraryError | 探针场景 |
| R3-P3-13 (R3C) 文档计数漂移 | round1-fixes（19→22 说明）、round2-fixes（10→12 说明）、round2-review（3→4 处弱断言）订正 | 文档 |

## 记录在案不修（R3-N-1..6）

promote config_hash 交叉校验（运行期增强）；同实验跨线晋升（设计自由度注记）；§5.6
落位偏差（设计文档修订走架构流程）；monotonicity 0.8 阈值从严（判定阈值平台持有）；
forward_returns 死参数与 docstring（随触碰处理）；R2VB 复验残余三项（逃逸面极小/既有
约束/误报方向）。

## 回归结果（2026-09-25 主审实测）

- 全量 `pytest tests/ -q`：**1495 passed / 1 failed / 6 skipped**——唯一失败为
  已知 Windows 临时目录清理竞态 flake（test_instruments_bulk_backfill，本轮仅
  复现 1 次，另一同文件用例通过）。零新增失败。
