# Round 20 修复与回归（loop-review-ds4f）

> **状态：CLOSED（2026-09-26 闭合）** —— 审查报告见 `round20-review.md`
> 日期：2026-09-26

## 修复总览

| 项 | 级别 | 修复 | 触及文件 |
|---|---|---|---|
| **R20B-F1** 上游因子响应缺键/为空 → 本地因子表被整体清空 → qfq 回落不复权 + 真除权日变假跌停 | **P1** | `sync_ex_factors` 加**清空守卫**：上游返回空而本地有因子时**拒绝覆盖**（保留本地因子、`changed` 不计入）+ `logger.warning`（"因子消失不是正常公司行为"）；合法因子变更（新增/修改）照常落库 | `src/data/service.py` |
| **R20A-F1** 我 R19 的 heat_cap 告警读错参数名（`cap` vs 声明名 `max_heat_pct`）→ 写出与配置矛盾的数字 | P2 | 抽 `heat_cap_of(config)` 读**声明参数** `max_heat_pct`（默认 0.06）；告警分母改为"可比行"（heat 有值且 equity>0） | `src/portfolio/backtester.py` |
| 顺带修复：`rematerialize_qfq` 的冻结守卫被放在 docstring **之前**（使 docstring 失效） | P3 | 移到 docstring 之后 | `src/data/service.py` |

## 新增钉子（4 条，全部变异实证）

| 钉子 | 断言 | 变异实证 |
|---|---|---|
| `test_empty_upstream_does_not_wipe_local_factors`（r20） | 本地有 1 条因子、上游返回空 → `changed == []` 且本地因子**原样保留**（qfq 基准不被清） | 去掉清空守卫 → **红** |
| `test_legit_factor_update_still_applies`（r20） | 上游返回 2 条（原 1 条 + 新增）→ 照常落库（守卫不挡合法更新） | — （反向钉子，防过度拦截） |
| `test_heat_cap_reads_declared_param_name`（r19） | `max_heat_pct: 0.12` → `heat_cap_of == 0.12`；未配该门 → 0.06 | 参数名退回 `cap` → **红** |
| heat_cap 告警文案（r19，加强） | 越线文案含"可比日结"计数与倍数；含 `heat=None`/`equity=0` 行时分母不变 | 告警恒 None → **红** |

## 回归结果

- 相关面：数据服务/网关/关键路径/回测器/评估 + r11~r20 钉子文件全绿；
- 全量套件：**1704 passed / 1 failed**（既有 Windows 临时文件 flake）；
- ruff：`(file, rule)` 与基线 78/78 一致（新增 0 条）；
- 变异实证：本轮 **6 个**语义变异（新增 4 条 + 既有 2 条复核）全部被抓住。

## 数字影响与运维提示

- **因子清空守卫不改任何现有数字**（生产当前无"上游返回空而本地有因子"的状态；守卫只在异常时
  阻止一次会毁数据的写入）。若 vendor 某次确实返回空，行为从"静默清除并重物化"变为
  "保留 + 告警"——**需要你知悉**：真因子消失（如数据源换口径）时现在需要人工介入。
- heat_cap 告警文案在**默认 cap 6%** 下与之前一致；非默认 cap 现在是**正确**的数字。
- R20B 的 B1/B2/B3 三条 backlog 见 `round20-review.md`（其中 B2 的 4 条样本全部落在在用回测
  窗口之外）。
