"""长期记忆用到的时间解析与格式化。"""
from __future__ import annotations

import re
import time
from datetime import datetime

_FULL_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_MD_RE = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_HM_RE = re.compile(r"^\s*(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?\s*$")


def parse_time(text: str, now: float | None = None) -> float:
    """把 “2026-11-13 09:00”、“11-13 09:00”、“09:00” 解析为时间戳（本地时区）。

    只给时分时按今天算；只给月日时按今年算。格式不对时抛出 ValueError。
    """
    today = datetime.fromtimestamp(now if now is not None else time.time())
    s = str(text).strip()
    if s in ("24:00", "24:00:00"):
        return datetime(today.year, today.month, today.day).timestamp() + 86400
    m = _FULL_RE.match(s)
    if m:
        y, mo, d, h, mi, sec = (int(v or 0) for v in m.groups())
        return datetime(y, mo, d, h, mi, sec).timestamp()
    m = _MD_RE.match(s)
    if m:
        mo, d, h, mi, sec = (int(v or 0) for v in m.groups())
        return datetime(today.year, mo, d, h, mi, sec).timestamp()
    m = _HM_RE.match(s)
    if m:
        h, mi, sec = (int(v or 0) for v in m.groups())
        return datetime(today.year, today.month, today.day, h, mi, sec).timestamp()
    raise ValueError(f"无法识别的时间：{text}（请使用 YYYY-MM-DD HH:MM）")


def fmt_dt(ts: float, now: float | None = None) -> str:
    """今天的时间只显示时分秒，其他日期带上月日。"""
    t = time.localtime(ts)
    n = time.localtime(now if now is not None else time.time())
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        return time.strftime("%H:%M:%S", t)
    return time.strftime("%m-%d %H:%M:%S", t)


def window_start(ts: float, minutes: int) -> float:
    """ts 所在时段的起点（按本地时间对齐到 minutes 分钟的整数倍）。"""
    t = time.localtime(ts)
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    step = minutes * 60
    return midnight + (ts - midnight) // step * step
