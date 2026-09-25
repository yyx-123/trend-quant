"""L1.5 gateway 单元测试（详设 §3，验收：PIT 测试——as_of 之后数据不可见）。

覆盖：as_of 必填、historical 模式严格 ≤ as_of、live 模式 provisional 标记、
受限句柄越权拒绝 + 留痕、可交易性推导（板块规则/分位舍入/除权基准/
停牌/新股无涨跌幅）、请求留痕。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest

from gateway import Gateway, GatewayViolation, build_panel
from gateway.panel import PanelRequestError
from gateway.tradability import board_limit_pct, compute_tradability

pytestmark = pytest.mark.unit


def _save_bars(db, symbol: str, rows: list[dict], price_mode="qfq"):
    df = pd.DataFrame(rows)
    db.save_market_data(symbol, df, price_mode=price_mode)


def _bars(dates, closes):
    return [
        {
            "time": d.isoformat(),
            "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": 1_000_000, "amount": c * 1_000_000,
        }
        for d, c in zip(dates, closes)
    ]


DAYS = [(pd.Timestamp(2024, 3, 11) + pd.Timedelta(days=i)).date() for i in range(5)]  # 周一~周五


@pytest.fixture
def db_with_bars(test_db):
    _save_bars(test_db, "510300.SS", _bars(DAYS, [4.0, 4.1, 4.2, 4.3, 4.4]))
    _save_bars(test_db, "600519.SS", _bars(DAYS, [10.0, 10.5, 11.0, 11.5, 12.0]))
    return test_db


# ----------------------------------------------------------------------
# 面板读取（as-of 强制）
# ----------------------------------------------------------------------


def test_as_of_required(db_with_bars):
    with pytest.raises(PanelRequestError):
        build_panel(db_with_bars, symbols=["510300.SS"], start=None, end=None,
                    fields=["close"], as_of=None)


def test_historical_mode_never_returns_beyond_as_of(db_with_bars):
    panel = build_panel(
        db_with_bars, symbols=["510300.SS", "600519.SS"],
        start="2024-03-11", end="2024-03-15", fields=["close"],
        as_of=datetime(2024, 3, 13, 15, 0),  # as_of 卡在 3-13
    )
    assert panel.dates[-1] == date(2024, 3, 13)
    assert all(d <= date(2024, 3, 13) for d in panel.dates)
    assert panel.field("close").shape == (3, 2)
    assert panel.bar_at("510300.SS", date(2024, 3, 13))["close"] == 4.2
    assert panel.bar_at("510300.SS", date(2024, 3, 14)) is None


def test_historical_mode_no_provisional_rows(db_with_bars):
    panel = build_panel(
        db_with_bars, symbols=["510300.SS"], start=None, end=None, fields=["close"],
        as_of=datetime(2024, 3, 15, 23, 59),
        mode="historical",
        live_overlay=lambda symbols, as_of: {"510300.SS": {"close": 9.9, "time": "2024-03-15"}},
    )
    # historical 模式忽略 live overlay
    assert panel.field("close")[-1, 0] == 4.4
    assert not panel.provisional.any()


def test_live_mode_marks_provisional(db_with_bars):
    overlay_day = date(2024, 3, 16)  # as_of 当日（周六也允许——live 模式只看 overlay）
    panel = build_panel(
        db_with_bars, symbols=["510300.SS"], start=None, end=None,
        fields=["close", "volume"], as_of=datetime(2024, 3, 16, 14, 0),
        mode="live",
        live_overlay=lambda symbols, as_of: {
            "510300.SS": {"close": 4.45, "open": 4.4, "high": 4.5, "low": 4.38,
                          "volume": 500_000, "time": "2024-03-16"}
        },
    )
    assert panel.dates[-1] == overlay_day
    assert panel.provisional[-1, 0] is True or bool(panel.provisional[-1, 0])
    assert panel.bar_at("510300.SS", overlay_day)["close"] == 4.45
    assert panel.bar_at("510300.SS", overlay_day)["provisional"] is True


def test_panel_field_validation(db_with_bars):
    with pytest.raises(PanelRequestError):
        build_panel(db_with_bars, symbols=["510300.SS"], start=None, end=None,
                    fields=["close", "future_eps"], as_of=date(2024, 3, 15))
    with pytest.raises(PanelRequestError):
        build_panel(db_with_bars, symbols=[], start=None, end=None,
                    fields=["close"], as_of=date(2024, 3, 15))


def test_panel_alignment_and_pre_close(db_with_bars):
    panel = build_panel(
        db_with_bars, symbols=["510300.SS", "600519.SS"],
        start="2024-03-11", end="2024-03-15", fields=["close", "pre_close"],
        as_of=date(2024, 3, 15),
    )
    closes = panel.field("close")
    pre = panel.field("pre_close")
    assert closes.shape == (5, 2)
    assert np_isnan(pre[0, 0])  # 首行无前收
    assert pre[1, 0] == 4.0 and pre[2, 1] == 10.5


def np_isnan(x) -> bool:
    return bool(pd.isna(x))


# ----------------------------------------------------------------------
# 受限句柄（§6.7 句柄约束）
# ----------------------------------------------------------------------


def test_bound_gateway_blocks_as_of_override(db_with_bars):
    gw = Gateway(db_with_bars)
    bound = gw.bind(as_of=date(2024, 3, 13), caller_layer="portfolio", run_id="R-t")
    panel = bound.get_panel(symbols=["510300.SS"], start=None, end=None, fields=["close"])
    assert panel.dates[-1] == date(2024, 3, 13)

    with pytest.raises(GatewayViolation):
        bound.get_panel(symbols=["510300.SS"], start=None, end=None,
                        fields=["close"], as_of=date(2024, 3, 15))
    gw.flush_audit()
    with db_with_bars.connect() as conn:
        rows = conn.execute(
            "SELECT method FROM gateway_audit ORDER BY id"
        ).fetchall()
    methods = [r["method"] for r in rows]
    assert "get_panel" in methods
    assert "violation:get_panel" in methods  # 越权留痕


def test_bound_gateway_data_version_snapshot(db_with_bars):
    gw = Gateway(db_with_bars)
    bound = gw.bind(as_of=date(2024, 3, 13), caller_layer="portfolio")
    v0 = bound.data_version
    assert v0 > 0
    # 绑定后再写数据，句柄锚定版本不变
    _save_bars(db_with_bars, "510300.SS", _bars([date(2024, 3, 18)], [4.5]))
    assert bound.data_version == v0


def test_audit_records_calls(db_with_bars):
    gw = Gateway(db_with_bars)
    gw.get_panel(symbols=["510300.SS"], start="2024-03-11", end="2024-03-13",
                 fields=["close"], as_of=date(2024, 3, 13), caller_layer="research",
                 run_id="R-x")
    gw.flush_audit()
    with db_with_bars.connect() as conn:
        row = conn.execute(
            "SELECT * FROM gateway_audit WHERE run_id = 'R-x'"
        ).fetchone()
    assert row is not None
    assert row["caller_layer"] == "research"
    assert row["as_of"] == "2024-03-13"
    assert row["data_version"] > 0


# ----------------------------------------------------------------------
# 可交易性标注（§3.2）
# ----------------------------------------------------------------------


def test_board_limit_rules():
    assert board_limit_pct("600519.SS") == 0.10   # 主板沪
    assert board_limit_pct("000001.SZ") == 0.10   # 主板深
    assert board_limit_pct("688981.SS") == 0.20   # 科创板
    assert board_limit_pct("300750.SZ") == 0.20   # 创业板
    assert board_limit_pct("510300.SS") == 0.10   # ETF 按主板幅度


def _closes(entries):
    return {"X.SS": pd.Series({d: c for d, c in entries})}


def test_tradability_limit_prices_and_rounding():
    days = [date(2024, 3, 11), date(2024, 3, 12)]
    closes = _closes([(date(2024, 3, 11), 10.005), (date(2024, 3, 12), 11.01)])
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=days,
        raw_closes={"600519.SS": pd.Series({date(2024, 3, 11): 10.005, date(2024, 3, 12): 11.01})},
        ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": None},
    )
    row = frame[frame["date"] == date(2024, 3, 12)].iloc[0]
    # 基准 10.005 × 1.1 = 11.0055 → 分位四舍五入 11.01；收盘 11.01 顶格 → 涨停
    assert row["limit_up_price"] == 11.01
    assert row["limit_down_price"] == 9.0
    assert bool(row["is_limit_up"]) is True
    assert bool(row["is_limit_down"]) is False
    assert bool(row["suspended"]) is False


def test_tradability_ex_dividend_base_price():
    """除权基准价：因子存储日 E 是**除权前**最后一根 bar，价格在 E 的**后一根
    bar（除权除息日 D）** 跳水——交易所参考前收只在 D 上做 ÷f。

    口径依据（loop-review-ds4f R1-P1-1，真实库 107/107 无歧义样本实证：跌幅
    落在 E+1，落在 E 的为 0；与 core/adjustment.py 的
    qfq(t)=raw(t)/Π_{ex_date≥t}f 语义自洽）。旧钉子把"跌幅与因子同一天"当
    既定前提，反而把错误口径钉死，历轮审查全绿。
    """
    days = [date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)]
    # E=2024-03-12（登记日，除权前价 10.0）；D=2024-03-13（跳水到 6.6 ≈ 10/1.5）
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=days,
        raw_closes={"600519.SS": pd.Series(
            {date(2024, 3, 11): 9.8, date(2024, 3, 12): 10.0, date(2024, 3, 13): 6.6}
        )},
        ex_factors={"600519.SS": [("2024-03-12", 1.5)]},
        listing_dates={"600519.SS": None},
    )
    row_e = frame[frame["date"] == date(2024, 3, 12)].iloc[0]
    # 登记日无除权修正：基准 = 9.8 → 涨停 10.78 / 跌停 8.82；收盘 10.0 在带内
    assert row_e["limit_up_price"] == 10.78
    assert row_e["limit_down_price"] == 8.82
    assert bool(row_e["is_limit_up"]) is False
    assert bool(row_e["is_limit_down"]) is False
    row_d = frame[frame["date"] == date(2024, 3, 13)].iloc[0]
    # 除权除息日：10.0 / 1.5 = 6.6667 → 涨停 7.33 / 跌停 6.0；收盘 6.6 在带内
    assert row_d["limit_up_price"] == 7.33
    assert row_d["limit_down_price"] == 6.0
    assert bool(row_d["is_limit_up"]) is False
    # 关键反向断言：修复前该日会被判成跌停（收盘 6.6 ≤ 10.0×0.9=9.0）→ 卖不出
    assert bool(row_d["is_limit_down"]) is False


def test_tradability_ex_dividend_real_call_form(test_db):
    """真实调用形态（Gateway 门面 + 真库 raw_close/ex_factors 取数路径）：
    登记日不误判涨停、除权日不误判跌停。

    loop-review-ds4f R1-P1-1 的钉子复刻教训：可交易性的四个旧用例全部手搓
    raw_closes/ex_factors，绕过 `_load_raw_closes` / `load_ex_factors` /
    元数据派生三条真实分支——错误口径因此在所有既有用例里都看不见。
    """
    from datetime import timedelta

    from gateway.service import Gateway

    d_e, d_d = date(2024, 3, 12), date(2024, 3, 13)
    rows = [
        (date(2024, 3, 11), 10.0),
        (d_e, 10.0),
        (d_d, 6.6),  # 10送5 → 因子 1.5 的跳水
    ]
    df = pd.DataFrame([
        {"time": f"{d.isoformat()} 00:00:00", "open": c, "high": c, "low": c,
         "close": c, "volume": 1000, "amount": c * 1000}
        for d, c in rows
    ])
    test_db.save_market_data("600519.SS", df, price_mode="raw", period="1d")
    test_db.replace_ex_factors("600519.SS", [("2024-03-12", 1.5)], provider="test")

    frame = Gateway(test_db).get_tradability(
        symbols=["600519.SS"], dates=[d_e, d_d],
        as_of=datetime.combine(d_d, time(15, 0)), caller_layer="test",
    )
    day_e = frame[frame["date"] == d_e].iloc[0]
    day_d = frame[frame["date"] == d_d].iloc[0]
    # 登记日（E）：基准价用 03-11 的 10.0，无除权修正
    assert day_e["limit_up_price"] == 11.0 and day_e["limit_down_price"] == 9.0
    # 除权除息日（D）：参考前收 10.0 / 1.5
    assert day_d["limit_up_price"] == pytest.approx(7.33, abs=0.01)
    assert day_d["limit_down_price"] == pytest.approx(6.0, abs=0.01)
    assert bool(day_e["is_limit_up"]) is False
    assert bool(day_d["is_limit_down"]) is False
    _ = timedelta  # 垫片期取前收由 Gateway 内部完成（见 compute_tradability 注释）


def test_tradability_suspension_on_trading_day():
    days = [date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)]
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=days,
        raw_closes={"600519.SS": pd.Series({date(2024, 3, 11): 10.0, date(2024, 3, 13): 10.5})},
        ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": None},
    )
    row = frame[frame["date"] == date(2024, 3, 12)].iloc[0]
    assert bool(row["suspended"]) is True
    # 停牌日仍用最近可得前收推导涨跌停价（复牌日与次日的判定都靠它）
    assert row["limit_up_price"] == 11.0
    # 3-13 复牌：基准价取最近可得前收（3-11 的 10.0）
    row13 = frame[frame["date"] == date(2024, 3, 13)].iloc[0]
    assert row13["limit_up_price"] == 11.0


def test_tradability_ipo_no_limit_window():
    days = [date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)]
    frame = compute_tradability(
        None, symbols=["001234.SZ"], dates=days,
        raw_closes={"001234.SZ": pd.Series({d: 10.0 for d in days})},
        ex_factors={"001234.SZ": []},
        listing_dates={"001234.SZ": "2024-03-11"},
    )
    # 上市起 5 个交易日内无涨跌幅限制
    for d in days:
        row = frame[frame["date"] == d].iloc[0]
        assert bool(row["no_limit"]) is True
        assert pd.isna(row["limit_up_price"])


def test_tradability_non_trading_day_skipped():
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=[date(2024, 3, 16)],  # 周六
        raw_closes={"600519.SS": pd.Series({date(2024, 3, 15): 10.0})},
        ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": None},
    )
    assert frame.empty
