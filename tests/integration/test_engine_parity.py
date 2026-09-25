"""新旧引擎 parity 对拍（详设 §8 阶段 1 验收 + §9 parity 测试）。

同策略（MACD 金叉进 / 死叉出、无止损、全进全出）、同窗口、同费用参数，
两侧各跑一遍：

- 无涨跌停干预时：成交逐笔一致（日期/方向/数量/价格位级一致）、NAV 一致
  ——预期差异三类（涨跌停卡控/T+1/尾盘滑点）全部置零后，两侧必须重合；
- 尾盘滑点 slippage_tail > 0 时：买入价按 (1+base+tail)/(1+base) 比例抬升；
- 金叉日涨停卡控时：新引擎拒买（unfilled），旧引擎照成交——差异必须
  恰好落在卡控日。
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.matcher import TradabilityCard
from engine.parity import attribute_diffs, diff_against_legacy, run_macd_parity
from engine.profiles import MarketProfile
from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)

pytestmark = pytest.mark.integration

PARITY_PROFILE = MarketProfile(
    name="parity_no_interest",     # 空仓计息是详设 §4.4 新引入口径（验收
    commission_rate=0.0000854,     # 白名单外的第四类显式差异），parity 对拍
    commission_min=5.0,            # 置零隔离引擎力学；计息正确性由
    stamp_tax_sell_stock=0.0005,   # test_engine_golden 单独锁定。
    stamp_tax_sell_etf=0.0,
    lot_size=100,
    t_plus=1,
    cash_interest_rate=0.0,
)

STRATEGY = {
    "id": "parity_macd",
    "trade_mode": "single_symbol_all_in",
    "entry": {
        "type": "group", "combinator": "all",
        "children": [{
            "id": "e1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_above",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
    "exit": {
        "type": "group", "combinator": "any",
        "children": [{
            "id": "x1", "type": "condition",
            "left": {"type": "indicator", "name": "macd_line", "params": {}},
            "operator": "cross_below",
            "right": {"type": "indicator", "name": "macd_signal", "params": {}},
        }],
    },
}


def _bars(n_days: int = 260, seed: int = 11) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed)
    closes = 10 + np.cumsum(rng.normal(0.015, 0.25, n_days))
    closes = np.maximum(closes, 1.0)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    return pd.DataFrame({
        "time": dates,
        "open": closes * (1 + rng.normal(0, 0.002, n_days)),
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n_days, 1e6),
        "amount": closes * 1e6,
    })


def _run_legacy(bars, slippage=0.002):
    engine = SingleSymbolAllInBacktestEngine()
    request = RuleBacktestRequest(
        strategy=STRATEGY, symbol="PARITY.SS", bars=bars,
        execution=BacktestExecutionConfig(
            initial_capital=100_000.0, slippage=slippage, instrument_type="etf",
        ),
    )
    return engine.run(request)


def _run_new(bars, slippage_tail=0.0, cards=None):
    return run_macd_parity(
        bars, initial_capital=100_000.0, slippage_base=0.002,
        slippage_tail=slippage_tail, asset_type="etf",
        profile=PARITY_PROFILE, cards=cards,
    )


def test_parity_identical_when_all_deltas_off():
    bars = _bars()
    legacy = _run_legacy(bars)
    new = _run_new(bars)
    assert len(new["trades"]) >= 4, "合成数据应产生若干往返，否则对拍无意义"
    diff = diff_against_legacy(new, legacy)
    assert diff["trades_new"] == diff["trades_old"]
    assert diff["trade_diffs"] == []
    assert diff["nav_divergence"] == []
    # 阶段 1 验收机器判据接线（DS-P2-9 + loop-review R2-P1-1）：零卡控场景
    # unexplained 必须为空——此前该断言缺失，归因器被替换为恒空桩时本测试
    # 仍绿（mutation 实证），"超纲即失败"名存实亡
    report = attribute_diffs(new, legacy)
    assert report["violations"] == []
    assert report["unexplained"] == []


def test_parity_tail_slippage_is_the_only_delta():
    bars = _bars()
    legacy = _run_legacy(bars)
    new = _run_new(bars, slippage_tail=0.001)
    diff = diff_against_legacy(new, legacy)
    assert diff["trades_new"] == diff["trades_old"]
    # 日期/方向序列逐笔一致（价格按固定比例偏移；后续买入数量可差一手——
    # 更高的成交价改变购买力，这是滑点参数的合法传导，不是口径差异）
    new_ts = new["trades"]
    old_ts = legacy["trades"]
    for new_t, old_t in zip(new_ts, old_ts):
        assert new_t["date"] == pd.Timestamp(old_t["date"]).date()
        assert new_t["side"] == old_t["side"]
        if new_t["side"] == "BUY":
            assert new_t["price"] == pytest.approx(
                float(old_t["exec_price"]) * 1.003 / 1.002, rel=1e-9
            )
        else:
            assert new_t["price"] == pytest.approx(
                float(old_t["exec_price"]) * 0.997 / 0.998, rel=1e-9
            )
    # R2VB B-2：集成层补"带真实差异 → 归因必须分类"断言——unit 层轴钉
    # 抓"归因器回退为恒干净"，本断言抓"归因器在真实差异前不作为"
    report = attribute_diffs(new, legacy)
    assert report["classified"]["tail_slippage"] >= len(new_ts)
    assert report["unexplained"] == []


def test_parity_limit_up_card_blocks_buy():
    bars = _bars()
    legacy = _run_legacy(bars)
    first_buy_day = pd.Timestamp(legacy["trades"][0]["date"]).date()
    cards = {first_buy_day: TradabilityCard(is_limit_up=True)}
    new = _run_new(bars, cards=cards)

    new_buy_days = [t["date"] for t in new["trades"] if t["side"] == "BUY"]
    old_buy_days = [pd.Timestamp(t["date"]).date() for t in legacy["trades"] if t["side"] == "BUY"]
    # 涨停日拒买：新引擎的首笔买入不在被卡控的那一天（顺延到下一个金叉）
    assert first_buy_day not in new_buy_days
    assert first_buy_day in old_buy_days
    # 归因白名单（loop-review R2-P1-1 如实化）：卡控场景路径级联错位，
    # 逐笔位置归因不可用——机器判据 = 卡控日零违规成交（violations），
    # unexplained 在该场景必然非空、不得用作验收断言（docstring 同步声明）
    report = attribute_diffs(new, legacy, cards=cards)
    assert report["violations"] == []
    # 但"卡控日拒买"这一事实本身仍可机器验证：新引擎在被卡控日无买入成交
    assert first_buy_day not in new_buy_days


# ----------------------------------------------------------------------
# loop-review-ds4f Round 2：归因界的适用区间（真实引擎形态）
# ----------------------------------------------------------------------


def test_parity_stage1_scale_has_no_false_positive_and_still_discriminates():
    """**stage-1 验收尺度**（260 日 / 尾滑点 0.001，多随机种子）：
    合法纯尾滑点 run 必须零假报警；同一 run 上注入 +100% 净值错误必须被判超纲。

    R1 的"数量 1/2 硬帽"与 R2 的"纯现金项"都在此形态上出过假报警（合法漂移是
    整手取整的量化随机游走，界必须同时含现金项与量化项）。V6 复核指出单一种子
    的 `saturated is False` 断言会踩 `nav_cascade_bound > 0.05` 的悬崖（种子 2
    的 drag 略大即饱和）——故这里对**多种子**只断言"零假报警 + 注入必被抓"
    （两者都由精确恒等式保证、与饱和状态无关），把"未饱和"作为**参考信息**
    在固定种子上单独核对。
    """
    import copy

    injected_kinds = set()
    for seed in (11, 13, 7, 2, 18, 19, 23, 42):
        bars = _bars(seed=seed)
        legacy = _run_legacy(bars)
        new = _run_new(bars, slippage_tail=0.001)
        report = attribute_diffs(new, legacy)
        assert report["unexplained"] == [],             f"seed={seed} stage-1 尺度假报警：{report['unexplained'][:2]}"
        assert report["nav_identity_checked_days"] > 0, "恒等式必须真的被校验"
        assert report["nav_identity_residual"] < 1e-12

        injected = copy.deepcopy(new)
        injected["daily_nav"][-1]["equity"] *= 2.0
        out2 = attribute_diffs(injected, legacy)
        assert any(
            u.get("kind") == "nav_identity_broken" for u in out2["unexplained"]
        ), f"seed={seed} +100% 净值错误必须被判超纲"
        injected_kinds.add(tuple(sorted({u.get("kind") for u in out2["unexplained"]})))

    # 固定种子的"未饱和"参考核对（判据精度的锚点；其他种子可能因 drag 略大而饱和，
    # 属如实标注而非缺陷）
    bars = _bars(seed=11)
    base = attribute_diffs(_run_new(bars, slippage_tail=0.001), _run_legacy(bars))
    assert base["saturated"] is False, "种子 11 的 stage-1 run 应当未饱和（判据有效）"
    assert base["nav_cascade_bound"] < 0.05


def test_parity_long_runs_have_no_false_positives_but_are_saturated():
    """长窗口：合法纯尾滑点 run 仍必须零假报警（旧两版都在此误杀数百笔），
    但归因**不再有判别力**——`saturated=True` 且 `attribution_note` 非空，
    该场景的验收判据是 `violations == []` 而非 `unexplained == []`。
    """
    for n_days, tail in ((1200, 0.003), (4000, 0.003), (6000, 0.002)):
        bars = _bars(n_days=n_days, seed=13)
        legacy = _run_legacy(bars)
        new = _run_new(bars, slippage_tail=tail)
        report = attribute_diffs(new, legacy)
        assert report["unexplained"] == [], \
            f"n={n_days} tail={tail} 合法 run 被误判：{len(report['unexplained'])} 条"
        assert report["saturated"] is True, f"n={n_days} 长窗口必须标注饱和"
        assert report["attribution_note"], "饱和必须带可读说明"
        assert report["violations"] == []


def test_parity_identity_checks_catch_fabrication_at_every_horizon():
    """**精确恒等式**在任意窗口长度上都抓得住伪造净值与伪造成交量。

    这是"长窗口下伪造净值与合法偏离不可区分"（R2A/R2/V3 三轮的困境）的反例：
    合法偏离**满足**恒等式（残差浮点级），伪造净值**破坏**恒等式。

    - 每侧 `equity == cash + 持仓市值`；
    - 新侧 `持仓市值 == Σ成交数量 × 收盘价`（收盘价取自旧侧 nav）。
    """
    import copy

    for n_days, tail in ((260, 0.001), (2500, 0.003), (6000, 0.003)):
        bars = _bars(n_days=n_days, seed=11)
        legacy = _run_legacy(bars)
        new = _run_new(bars, slippage_tail=tail)
        base = attribute_diffs(new, legacy)
        assert base["nav_identity_checked_days"] > 0, "恒等式必须真的被校验"
        assert base["nav_identity_residual"] < 1e-12, \
            f"合法 run 的恒等式残差必须浮点级（实际 {base['nav_identity_residual']}）"

        # 伪造净值：+5% 与 +100% 都必须被抓（长窗口也不例外）
        for mult in (1.05, 2.0):
            inj = copy.deepcopy(new)
            inj["daily_nav"][-1]["equity"] *= mult
            out = attribute_diffs(inj, legacy)
            assert any(u.get("kind") == "nav_identity_broken"
                       for u in out["unexplained"]), \
                f"n={n_days} 净值 ×{mult} 未被恒等式抓住"

        # 伪造成交量：破坏"持仓市值 = Σ成交数量 × 收盘价"
        inj2 = copy.deepcopy(new)
        inj2["trades"][-1]["qty"] = int(inj2["trades"][-1]["qty"] * 1.6)
        out2 = attribute_diffs(inj2, legacy)
        assert any(u.get("kind") == "nav_position_identity_broken"
                   for u in out2["unexplained"]), f"n={n_days} 数量 ×1.6 未被恒等式抓住"
