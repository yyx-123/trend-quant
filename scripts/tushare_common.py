"""tushare 季度临时账号脚本的公共部分（token、限流、报告期推算）。

仅在 scripts/ 内使用；应用代码对 tushare 零依赖（方案 §5）。
token 通过环境变量 TUSHARE_TOKEN 注入，不入库、不进 git、不写 config。
"""

from __future__ import annotations

import os
from datetime import date, datetime

QUARTER_ENDS = ("0331", "0630", "0930", "1231")


def get_pro_api():
    """构造 tushare pro_api；缺依赖/缺 token 时给出可操作的报错。

    镜像站账号（如 tuaremax.top 等灰产渠道）需同时设置环境变量
    TUSHARE_HTTP_URL —— 卖家要求覆盖请求地址才能用。
    """
    try:
        import tushare as ts
    except ImportError as exc:
        raise SystemExit(
            "[tushare] 缺少依赖：.venv/Scripts/pip install tushare"
            "（或 pip install -e \".[tushare]\"，见 pyproject optional-dependencies）"
        ) from exc
    token = str(os.getenv("TUSHARE_TOKEN", "") or "").strip()
    if not token:
        raise SystemExit("[tushare] 缺少环境变量 TUSHARE_TOKEN（季度临时账号）")
    pro = ts.pro_api(token)
    mirror = str(os.getenv("TUSHARE_HTTP_URL", "") or "").strip()
    if mirror:
        pro._DataApi__token = token
        pro._DataApi__http_url = mirror
    return pro


def call_with_retry(fn, *args, attempts: int = 4, sleep_seconds: float = 3.0, **kwargs):
    """带重试的接口调用 —— 镜像站偶发 SSL 断连/限流，重试几乎总能成功。

    最后一次失败照常抛异常，由调用方决定记失败还是中止。
    """
    import time

    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt >= attempts:
                raise
            time.sleep(sleep_seconds)


def all_periods() -> list[str]:
    years = range(date.today().year - 1, date.today().year + 1)
    return sorted(f"{year}{md}" for year in years for md in QUARTER_ENDS)


def target_period(today: date | None = None) -> str:
    """推算目标报告期：最近已结束季度末；距季末不足 20 天（披露窗口）则再退一期。"""
    today = today or date.today()
    today_str = today.strftime("%Y%m%d")
    ended = [p for p in all_periods() if p <= today_str]
    latest = ended[-1]
    qend = datetime.strptime(latest, "%Y%m%d").date()
    if (today - qend).days < 20:
        return ended[-2]
    return latest


def prev_period(period: str) -> str:
    periods = all_periods()
    idx = periods.index(period)
    if idx == 0:
        raise ValueError(f"period 超出范围: {period}")
    return periods[idx - 1]
