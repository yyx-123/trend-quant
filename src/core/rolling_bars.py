"""滚动锚定周/月K 与趋势值（2026-09-13 起的研究口径）。

与 ``core.bars.fitted_period_rows``（自然周/月锚定、在途 bar 逐日累计）
不同，本模块把整条 bar 序列**每天重新锚定到当日**：第 k 根 bar
（k=0 为最新）= 截至当日的倒数第 k 个 D 交易日窗口（右闭），
D：周=5、月=22。

口径性质：

- 每根 bar 恒为满 D 个交易日 → 量能/TR 无周期内爬坡；
- 相邻 bar 的 close 间距恒为一个周期 → 无拼接间距 artifact；
- 第 t 日的序列只含 [t-K*D+1, t] 的日K → 严格 PIT，无前视；
- EMA 等递归指标只看最近 K 根 bar（有限记忆），与把同一截断序列喂给
  ``core.trend.calculate_trend_score_series`` 的结果逐点一致（等价性由
  tests/unit/test_rolling_bars.py 钉死）。

趋势值复用 canonical 公式的全部结构（tanh 除数、权重、0.3/0.7 指数、
±100 clip 均不动），仅窗口参数按周期折算：

======  ====  ====
参数     周    月
======  ====  ====
bar 天数 D    5     22
atr_period    8     6
vol_ma_period 8     6
er_period     4     3
======  ====  ====

实现为 (T, K) numpy 矩阵向量化：T=交易日数，K=回看 bar 数（默认 16）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.bars import PERIOD_MONTHLY, PERIOD_WEEKLY, normalize_period

# 滚动 bar 的日K 窗口长度（固定交易日数，不用自然日框定：每根 bar 严格同尺度，
# 避免 20~23 天不等带来的量能/极差摆动）。
ROLLING_BAR_DAYS = {PERIOD_WEEKLY: 5, PERIOD_MONTHLY: 22}

# 周期相关的窗口覆盖（周 ATR8/ER4、月 ATR6/ER3；MA/EMA 3/5/8 与 tanh/权重不动）。
ROLLING_TREND_OVERRIDES = {
    PERIOD_WEEKLY: {"atr_period": 8, "vol_ma_period": 8, "er_period": 4},
    PERIOD_MONTHLY: {"atr_period": 6, "vol_ma_period": 6, "er_period": 3},
}

_DEFAULT_N_BARS = 16


def rolling_trend_cfg(period: str, base: dict | None = None) -> dict:
    """滚动周/月趋势值的 cfg：在日K 默认值上覆盖周期窗口参数。"""
    from core.strategy_config import DEFAULT_STRATEGY_CONFIG

    canonical = normalize_period(period)
    if canonical not in ROLLING_TREND_OVERRIDES:
        raise ValueError(f"rolling trend is only defined for weekly/monthly, got {period!r}")
    cfg = dict(DEFAULT_STRATEGY_CONFIG if base is None else base)
    cfg.update(ROLLING_TREND_OVERRIDES[canonical])
    return cfg


def _clean_daily(daily_df: pd.DataFrame) -> pd.DataFrame:
    """排序 + 丢弃 OHLC 缺失行（与 trend 公式的 calc_df 清洗口径一致）。"""
    df = daily_df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"])
    df["volume"] = df.get("volume", 0.0)
    df["volume"] = df["volume"].fillna(0.0)
    return df.sort_values("time").reset_index(drop=True)


def rolling_period_frame(
    daily_df: pd.DataFrame, end_idx: int, period: str, n_bars: int = _DEFAULT_N_BARS
) -> pd.DataFrame:
    """第 end_idx 个交易日（清洗后位置索引）的滚动周/月 bar 序列。

    返回标准 OHLCV DataFrame（time/open/high/low/close/volume），按时间升序
    （最旧在前），最多 n_bars 根；历史不足时返回实际根数。bar 的 time 标注
    为窗口右端（最后）交易日。供等价性测试与单点抽查使用。
    """
    canonical = normalize_period(period)
    if canonical not in ROLLING_BAR_DAYS:
        raise ValueError(f"rolling bars are only defined for weekly/monthly, got {period!r}")
    d = ROLLING_BAR_DAYS[canonical]
    df = _clean_daily(daily_df)
    if end_idx < 0 or end_idx >= len(df) or df.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    n_available = (end_idx + 1) // d  # 完整窗口数
    n = min(n_available, n_bars)
    rows = []
    for k in range(n - 1, -1, -1):  # 最旧 → 最新
        right = end_idx - d * k
        window = df.iloc[right - d + 1 : right + 1]
        rows.append(
            {
                "time": df["time"].iloc[right],
                "open": window["open"].iloc[0],
                "high": window["high"].max(),
                "low": window["low"].min(),
                "close": window["close"].iloc[-1],
                "volume": window["volume"].sum(),
            }
        )
    return pd.DataFrame(rows)


def _win_mean(mat: np.ndarray, w: int, *, full: bool) -> np.ndarray:
    """沿 k 轴（axis=1，k 增大=更旧）的窗口均值：out[:, k] = mean(mat[:, k:k+w])。

    full=True 要求窗口内 w 个值齐全（对应 rolling(w, min_periods=w)）；
    full=False 允许部分窗口（rolling(w, min_periods=1)），窗口内无有效值时为 NaN。
    """
    t, k_max = mat.shape
    out = np.full_like(mat, np.nan)
    if w > k_max:
        return out
    valid = ~np.isnan(mat)
    cs = np.hstack([np.zeros((t, 1)), np.cumsum(np.nan_to_num(mat), axis=1)])
    cc = np.hstack([np.zeros((t, 1)), np.cumsum(valid.astype(float), axis=1)])
    s = cs[:, w:] - cs[:, :-w]
    n = cc[:, w:] - cc[:, :-w]
    if full:
        mean = np.where(n == w, s / w, np.nan)
    else:
        mean = np.where(n >= 1, s / np.maximum(n, 1.0), np.nan)
    out[:, : k_max - w + 1] = mean
    return out


def _ema_along_k(close: np.ndarray, n: int) -> np.ndarray:
    """ewm(span=n, adjust=False) 沿时间正向（k 从旧到新）的 EMA。

    矩阵布局 k=0 为最新，故递归从最大 k 向 k=0 进行；每行在最旧的有效
    bar 处播种（ema = close），与 pandas 对该截断序列的 ewm 一致。
    """
    alpha = 2.0 / (n + 1.0)
    k_max = close.shape[1]
    ema = np.full_like(close, np.nan)
    ema[:, -1] = close[:, -1]
    for k in range(k_max - 2, -1, -1):
        cur, nxt = close[:, k], ema[:, k + 1]
        ema[:, k] = np.where(
            np.isnan(cur), np.nan, np.where(np.isnan(nxt), cur, alpha * cur + (1.0 - alpha) * nxt)
        )
    return ema


def rolling_period_trend_series(
    daily_df: pd.DataFrame, cfg: dict, *, period: str, n_bars: int | None = None
) -> pd.Series:
    """每个交易日的滚动周/月趋势值（以 time 为索引的 Series，预热期内 NaN）。

    与 ``calculate_trend_score_series`` 同一公式结构；第 t 日的值 = 把当日
    滚动 bar 序列（最近 min(K, 可用) 根）喂给该函数后末行的 trend_score。
    """
    canonical = normalize_period(period)
    if canonical not in ROLLING_BAR_DAYS:
        raise ValueError(f"rolling trend is only defined for weekly/monthly, got {period!r}")
    d = ROLLING_BAR_DAYS[canonical]

    n_short = int(cfg.get("n_short", 3))
    n_mid = int(cfg.get("n_mid", 5))
    n_long = int(cfg.get("n_long", 8))
    atr_period = int(cfg.get("atr_period", 20))
    vol_ma_period = int(cfg.get("vol_ma_period", 20))
    er_period = int(cfg.get("er_period", 10))
    min_bars = max(n_long, atr_period) + 2
    k_max = n_bars if n_bars is not None else max(_DEFAULT_N_BARS, min_bars + 2)

    df = _clean_daily(daily_df)
    times = df["time"]
    t_count = len(df)
    out = pd.Series(np.nan, index=pd.Index(times, name="time"), name="trend_score")
    if t_count < d * min_bars:
        return out

    c = df["close"].to_numpy(dtype=float)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)

    # 窗口聚合（右端 r，窗口 [r-d+1, r]）；r < d-1 处为 NaN（不完整窗口）。
    win_o = np.full(t_count, np.nan)
    win_o[d - 1 :] = o[: t_count - d + 1]
    win_h = pd.Series(h).rolling(d).max().to_numpy()
    win_l = pd.Series(low).rolling(d).min().to_numpy()
    win_v = pd.Series(v).rolling(d).sum().to_numpy()

    # 第 i 日第 k 根 bar = r=i-d*k 处的窗口聚合；r<d-1 无效（历史不足）。
    idx = np.arange(t_count)
    r = idx[:, None] - d * np.arange(k_max)[None, :]
    valid = r >= d - 1
    rc = np.clip(r, 0, t_count - 1)

    def _gather(win: np.ndarray) -> np.ndarray:
        return np.where(valid, win[rc], np.nan)

    close = _gather(c)
    high = _gather(win_h)
    low_ = _gather(win_l)
    volume = _gather(win_v)

    # TR：prev close = 更旧一根（k+1）的 close；最旧一根无 prev → high-low
    # （np.fmax 忽略 NaN，与 pandas concat.max(axis=1) 的 skipna 一致）。
    prev_close = np.full_like(close, np.nan)
    prev_close[:, :-1] = close[:, 1:]
    tr = np.fmax(high - low_, np.fmax(np.abs(high - prev_close), np.abs(low_ - prev_close)))
    atr = _win_mean(tr, atr_period, full=False)

    def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
        out_div = np.where((den > 0) & ~np.isnan(den), num / np.where(den > 0, den, 1.0), np.nan)
        return np.nan_to_num(out_div, nan=0.0)

    wb = [float(cfg.get("w_bias_short", 0.4)), float(cfg.get("w_bias_mid", 0.4)), float(cfg.get("w_bias_long", 0.2))]
    ws = [float(cfg.get("w_slope_short", 0.4)), float(cfg.get("w_slope_mid", 0.4)), float(cfg.get("w_slope_long", 0.2))]

    bias_mix = np.zeros_like(close)
    slope_mix = np.zeros_like(close)
    for n, w_b, w_s in zip((n_short, n_mid, n_long), wb, ws):
        ma_n = _win_mean(close, n, full=True)
        bias_mix += w_b * _safe_div(close - ma_n, atr)
        ema_n = _ema_along_k(close, n)
        ema_diff = np.full_like(close, np.nan)
        ema_diff[:, :-1] = ema_n[:, :-1] - ema_n[:, 1:]
        slope_mix += w_s * _safe_div(ema_diff, atr * n)

    norm_bias = np.tanh(bias_mix / 2.0) * 100.0
    norm_slope = np.tanh(slope_mix) * 100.0
    price_direction = (
        float(cfg.get("w_bias_norm", 0.5)) * norm_bias
        + float(cfg.get("w_slope_norm", 0.5)) * norm_slope
    )

    vol_ma = _win_mean(volume, vol_ma_period, full=False)
    vol_ratio = np.where(vol_ma > 0, volume / np.where(vol_ma > 0, vol_ma, 1.0), np.nan)
    volume_factor = np.nan_to_num(np.clip(vol_ratio / 3.0, 0.0, 1.0), nan=0.0)

    # ER(p)：|close[k]-close[k+p]| / Σ_{j=k}^{k+p-1}|close[j]-close[j+1]|。
    step = np.abs(close[:, :-1] - close[:, 1:])  # (T, K-1)，step[:, j]=bar j 与 j+1 之差
    cs = np.hstack([np.zeros((t_count, 1)), np.cumsum(np.nan_to_num(step), axis=1)])
    cc = np.hstack([np.zeros((t_count, 1)), np.cumsum((~np.isnan(step)).astype(float), axis=1)])
    w = min(er_period, step.shape[1])
    s = cs[:, w:] - cs[:, :-w]
    n = cc[:, w:] - cc[:, :-w]
    vol_sum = np.full_like(close, np.nan)
    vol_sum[:, : step.shape[1] - w + 1] = np.where(n >= 1, s, np.nan)
    change = np.full_like(close, np.nan)
    if er_period < k_max:
        change[:, : k_max - er_period] = np.abs(close[:, : k_max - er_period] - close[:, er_period:])
    er = np.nan_to_num(
        np.where(vol_sum > 0, change / np.where(vol_sum > 0, vol_sum, 1.0), np.nan), nan=0.0
    )
    er = np.clip(er, 0.0, 1.0)

    confidence = (volume_factor ** float(cfg.get("w_vol", 0.3))) * (er ** float(cfg.get("w_er", 0.7)))
    trend = np.clip(price_direction * confidence, -100.0, 100.0)

    # 预热门：完整 bar 数 >= min_bars 且 ATR > 0（只读 k=0 的末行值）。
    n_complete = (idx + 1) // d
    gate = (n_complete >= min_bars) & (atr[:, 0] > 0) & ~np.isnan(atr[:, 0])
    out.iloc[:] = np.where(gate, trend[:, 0], np.nan)
    return out
