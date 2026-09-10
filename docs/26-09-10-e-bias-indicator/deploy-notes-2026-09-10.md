# 线上部署手册：E-Bias（均线偏离度）落库 + 展示（2026-09-10）

目标：把本地已完成的 E-Bias 功能同步到线上服务器（Ubuntu + systemd + nginx，
代码 `/srv/trend-quant`，服务 `trend-quant`，库 `/srv/trend-quant/data/trend_quant.db`）。

**核心结论：历史数据的回填不需要手工脚本。** 本次把
`INDICATOR_FORMULA_VERSION` 由 `2` 提到 `3`，服务重启时的启动自检
（`services/indicator_builder.rebuild_if_needed`）会检测到版本落后，自动
备份并**全量重建全部标的的指标缓存**，`e_bias20` 的历史值即在此时写入。

预计耗时：10-20 分钟，其中重建本身约 1-2 分钟，**主要耗时是重建前的
`VACUUM INTO` 全库备份**。

---

## 0. 这次改动会做什么（先读，避免误判）

首启时会依次发生三件事，全部自动：

1. **建列/迁移**：`db._migrate_schema` 给 `indicator_daily` 加一列
   `e_bias20 REAL`（`ALTER TABLE ADD COLUMN`，幂等，不动存量数据）。
2. **备份**：`rebuild_if_needed` 检测到 `indicator_global_version() != 3` →
   调 `db.backup_to()` 做一次 `VACUUM INTO` 全库快照，落到
   `data/backups/trend_quant-<时间戳>.db`，并把备份目录修剪到只留最新 3 份。
3. **全量重建**：`rebuild_all()` 对全部标的重算 `indicator_daily` +
   `trend_daily`，此时 `e_bias20` 全history 写入。

⚠️ **这一步是必需的、不是可选的。** 如果不提版本号，`e_bias20` 列会被
ALTER 出来但永远为 NULL，而缓存新鲜度判定（`indicator_store._cache_fresh`
只比版本号 + 缓存末尾日期）仍会报告「新鲜」，于是读取永远返回全 NaN、
**永不回退实时计算** —— 指标会安静地永远为空，且没有任何报错。

### 实测量级（本地在真实数据上模拟，非估算）

| 项 | 实测值 |
|---|---|
| 单标的重建（1623 根日K） | ≈ 76 ms |
| 30 标的重建总耗时 | 2.27 s |
| 外推 874 标的重建 | **≈ 66 s** |
| 备份产物大小 | ≈ 库文件大小（VACUUM INTO 全量快照） |

**磁盘要求**：`data/` 所在分区需有 ≥ 当前库文件大小的空闲空间。
这是本次部署唯一的硬性前置条件 —— 库有多大就需要多少空间。

---

## 1. 本地：提交并推送

```bash
cd /Users/yyx/myProjects/trend-quant
git add -A && git commit -m "feat: 新增 E-Bias（均线偏离度·减法版）—— 落库 + 查看页副图 + 看板热力图维度"
git push
```

## 2. 服务器：确认磁盘空间（关键前置）

```bash
ssh root@<服务器IP>
df -h /srv            # 可用空间必须 ≥ data/trend_quant.db 的大小
du -sh /srv/trend-quant/data/trend_quant.db
ls -lh /srv/trend-quant/data/backups/
```

空间不足时先清理旧备份（`backup_to` 只保留 3 份，但历史遗留文件不会自动清）：

```bash
ls -lt /srv/trend-quant/data/backups/    # 人工确认后删除过旧的快照
```

## 3. 服务器：更新代码并重启

```bash
cd /srv/trend-quant && git pull
source .venv/bin/activate && pip install -e .    # 本次无依赖变动，装一次保险
systemctl restart trend-quant
```

重启即刻返回，重建在后台线程里跑。**不要立刻连续重启**（会打断重建）。

## 4. 观察重建是否完成

```bash
journalctl -u trend-quant -f | grep -E "Indicator cache|Formula/params changed|rebuild"
```

期望看到（顺序）：

```
Formula/params changed (trend_stale=..., indicator_stale=True) — full indicator rebuild scheduled
Indicator cache startup check: rebuilt
```

`status` 为 `up_to_date` 说明版本号已是 3（即此前已重建过），无需处理。

## 5. 验证回填结果

```bash
sqlite3 /srv/trend-quant/data/trend_quant.db <<'SQL'
-- ① 版本号已是 3
SELECT MAX(formula_version) AS formula_version FROM indicator_daily;

-- ② e_bias20 已全量写入（NULL 数应为 0）
SELECT COUNT(*) AS total,
       SUM(e_bias20 IS NULL) AS nulls,
       MIN(e_bias20) * 100 AS min_pct,
       MAX(e_bias20) * 100 AS max_pct
FROM indicator_daily;

-- ③ 逐标的覆盖：每个标的的 e_bias20 都不为空
SELECT COUNT(*) AS symbols_with_nulls FROM (
  SELECT symbol FROM indicator_daily GROUP BY symbol HAVING SUM(e_bias20 IS NULL) > 0
);
SQL
```

期望：`formula_version = 3`；`nulls = 0`；`symbols_with_nulls = 0`；
`min_pct/max_pct` 落在 ±30% 量级（本地 852 只标的实测区间
`[-12.7%, +22.9%]`，全历史逐日 P1/P99 为 `[-17.6%, +24.9%]`）。

### 手工兜底（仅在自动重建未触发/中断时用）

```bash
cd /srv/trend-quant && source .venv/bin/activate
python -c "
import sys; sys.path.insert(0, 'src')
from data.storage.db import init_db
from services.indicator_builder import rebuild_if_needed
init_db()
print(rebuild_if_needed())
"
```

## 6. 页面验收

| 检查项 | 期望 |
|---|---|
| `/market-view` 任一标的 | BIAS 副图**下方**出现「E-BIAS」副图；4 条虚线参考线带标签（过热 15 / 失速 5 / 零轴 0 / 止损参考 −5）；右上角有来源说明文字 |
| `/subject-market` 热力图 | 「着色」多出第 4 个按钮「E-Bias 偏离度」；切换后色块按偏离度染色（正红负绿），无「数据不足」灰块 |
| `/subject-market` 悬停任一色块 | 详情表多一行「E-Bias 偏离度」，带正负号与百分号 |
| MCP `symbol_detail` | 返回 `indicators.e_bias`（`series` / `period=20` / `lines`） |
| MCP `dashboard` | 标的与类目行都带 `e_bias_pct`（`detail="lite"` 也保留） |

## 7. 回滚

代码回滚即可，无需回滚数据：

```bash
cd /srv/trend-quant && git revert <commit> && systemctl restart trend-quant
```

- `e_bias20` 列会留在表里（无副作用，回滚后的代码不读它）；
- 回滚后 `INDICATOR_FORMULA_VERSION` 回到 `2`，启动自检会**再触发一次
  全量重建**（把缓存写回 v2 形态），同样会自动先备份。

若要还原数据，用第 2 步时间点生成的 `data/backups/trend_quant-<时间戳>.db`。

---

## 附：三个口径备忘（排障时最容易搞混的地方）

1. **单位**：`indicator_daily.e_bias20` 是 **decimal**（`0.05` = 高于 20 日 EMA 5%），
   遵守 `core/indicators.py` 的锁定约定；而**所有对外 payload 都是百分比**
   —— 查看页/MCP `symbol_detail` 是 `indicators.e_bias.series`（`×100`），
   看板/MCP `dashboard` 是 `e_bias_pct`（`×100`，字段名带 `_pct` 后缀）。
   两处 payload 同单位，可跨接口直接比较。
2. **公式**：`ln(Close) − EMA(ln Close, 20)`。EMA 作用在**对数价**上
   （`EMA(ln C)`，通达信口径），不是 `ln(EMA(C))`。
3. **参考线未标定**：15 / 5 / −5 取自广发策略的**行业指数**分组统计，
   未在本项目 ETF/股票池上标定，页面上已如实标注。直接照搬可能不合适 ——
   本地实测 852 只标的中仅 0.8% 高于 15%、7.7% 高于 5%。若要用它做交易规则，
   需先按 `docs/26-09-10-e-bias-indicator/*.md` 的「阶段 4」做阈值标定。
