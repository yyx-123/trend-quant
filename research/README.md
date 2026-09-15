# research/ —— 小型研究与跑数归档

每个子目录是一项独立的小研究，命名 `YYYY-MM-DD_<english-slug>`
（目录名一律英文，如 `2026-09-12_trend-score-distribution`），自包含：
计算脚本、绘图脚本、报告（`REPORT.md`，中文，插图用相对路径引用）。

约定：

- **可再生的中间产物不入库**（见本目录 `.gitignore`）：`data/*.npy` 这类
  原始样本由计算脚本从生产库再生，字体等大文件由脚本运行时自动下载。
- 报告引用的小体积权威结果（如 `stats.json`）与最终图表入库。
- 计算脚本用生产 venv（`.venv/bin/python`）运行；绘图等需要额外依赖的
  步骤用隔离 venv（如 `scripts/temp/plot-venv`），不给生产环境引入新包。
- 读库脚本经 `scripts/_common.py` 引导（.env + DB 路径锚定项目根），
  只读不改，除非研究主题本身就是写路径。

## 已有研究

- `2026-09-12_trend-score-distribution` —— trend_score 全市场分布：股票/ETF × 日/周/月
  六组，为「正/无/负趋势」阈值划分提供数据依据。
- `2026-09-13_phase-migration` —— 日/周/月三态组合的 27 种相位：首次出现后
  1周/2周/1月/2月收益，以及 27×27 迁移矩阵的频次与迁移后收益分布。
- `2026-09-13_phase-migration-live-synth` —— 上一研究的口径修正：周/月趋势
  改为逐日实时计算（在途 bar 用日K 合成，与生产函数逐点等价），
  量化「信号提前量」并重新评估反转效应。
- `2026-09-13_rolling-trend-calibration` —— 滚动锚定周/月趋势值（core/rolling_bars）
  的全市场标定：六组分布与 ±5 分位、tanh 饱和率/量能/ER 中间量诊断、
  月 er_period ∈ {3,4,6} 对比，给出阈值与 ER 窗口建议。
- `2026-09-13_phase-migration-rolling` —— 相位迁移 v3：周/月状态改用滚动锚定口径
  （周 ATR8/ER4、月 ATR6/ER3），月分量变化的月内分布接近期望均匀；
  v1→v2→v3 三版对比显示旗舰反转路径收益单调衰减（计时 artifact 被消除）。
  数字基于周/月 ±5 阈值，已被 v4 取代。
- `2026-09-14_phase-migration-th9` —— 相位迁移 v4：同 v3 滚动口径，三态阈值
  调整为日 ±5 / 周·月 ±9（依据 rolling-trend-calibration 的分布标定）。
  周/月分量噪声事件清零，「日级与周/月反向」结构保持且增强。**当前优先口径**。
- `2026-09-14_portfolio-construction` —— 组合层研究「选谁/买多少/调仓」：
  主报告 `REPORT.md`（信号密度、选股边际+beta 伪装检验、组合引擎实测——
  持仓数是唯一稳健杠杆，选股≈随机）；补充报告 `REPORT_buckets.md`
  （固定 horizon 分桶、ETF 新鲜度衰减、breadth 市场状态 ex-ante 条件）。
