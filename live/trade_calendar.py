# -*- coding: utf-8 -*-
"""live/trade_calendar.py — 交易日历（下一交易日 / 是否交易日）

优先从 daily_bar 的真实日期序列推导（**数据驱动，天然含节假日**）；
数据缺失（如 bars.db 为空壳）时回退"跳过周末"的朴素规则，并在返回值中标注来源，
避免误把周末当交易日导致空跑。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _bars_db_path() -> Path:
    cd = os.environ.get("LWQUANT_CACHE_DIR")
    return (Path(cd) if cd else (BASE / "data" / "cache")) / "bars.db"


def trade_dates() -> list:
    """daily_bar 中已存在的去重升序交易日列表（无数据/异常 → []）"""
    db = _bars_db_path()
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date").fetchall()
        con.close()
        return [str(r[0])[:10] for r in rows if r[0]]
    except Exception:
        return []


def next_trade_date(d: str = None) -> dict:
    """下一交易日。返回 {date, source}；source=calendar(来自行情数据) / weekday(回退) / unknown"""
    d = (d or date.today().isoformat())[:10]
    ds = trade_dates()
    if ds:
        for x in ds:
            if x > d:
                return {"date": x, "source": "calendar"}
        return {"date": "", "source": "unknown"}      # 数据里没有更晚的交易日
    dt = _add_days(d, 1)
    return {"date": dt.isoformat(), "source": "weekday"}


def previous_trade_date(d: str = None) -> dict:
    """上一交易日（盘前定位昨日计划用），返回 {date, source}"""
    d = (d or date.today().isoformat())[:10]
    ds = trade_dates()
    if ds:
        prev = [x for x in ds if x < d]
        if prev:
            return {"date": prev[-1], "source": "calendar"}
        return {"date": "", "source": "unknown"}
    dt = _add_days(d, -1)
    return {"date": dt.isoformat(), "source": "weekday"}


def is_trade_date(d: str = None) -> bool:
    d = (d or date.today().isoformat())[:10]
    ds = trade_dates()
    if ds:
        return d in set(ds)
    try:
        return date.fromisoformat(d).weekday() < 5
    except Exception:
        return False


def _add_days(d: str, n: int) -> date:
    dt = date.fromisoformat(d) + timedelta(days=n)
    if n > 0:                       # 向后：跳过周末
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
    else:                           # 向前：跳过周末
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
    return dt
