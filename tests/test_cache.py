# -*- coding: utf-8 -*-
"""data.cache.DailyCache 往返与单位归一测试（网络无关，使用临时单库）。"""
import pandas as pd
import pytest

from data.cache import DailyCache, _f, normalize_units


def _sample_df():
    return pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "open": [10.0, 10.5],
        "high": [10.5, 11.0],
        "low": [9.8, 10.2],
        "close": [10.2, 10.8],
        "preclose": [10.0, 10.2],
        "volume": [1000.0, 1100.0],
        "amount": [10200.0, 11880.0],
        "turn": [1.0, 1.1],
        "pct_chg": [2.0, 5.88],
        "is_st": [0, 0],
    })


def test_put_get_roundtrip(tmp_path):
    db = tmp_path / "bars.db"
    c = DailyCache(db_path=db)  # 单库模式
    n = c.put_daily("600519.SH", _sample_df(), adjust="qfq", source="baostock")
    assert n == 2
    out = c.get_daily("600519.SH", adjust="qfq")
    assert out is not None and len(out) == 2
    assert abs(out.iloc[0]["close"] - 10.2) < 1e-6
    assert abs(out.iloc[1]["pct_chg"] - 5.88) < 1e-6


def test_nan_becomes_none():
    assert _f(float("nan")) is None
    assert _f(None) is None
    assert _f(1.5) == 1.5


def test_normalize_units_tushare(tmp_path):
    # tushare 源：amount 千元→元、volume 手→股
    df = pd.DataFrame({
        "date": ["2024-01-02"], "open": [10.0], "high": [10.5], "low": [9.8],
        "close": [10.2], "preclose": [10.0], "volume": [100.0], "amount": [1020.0],
        "turn": [1.0], "pct_chg": [2.0], "is_st": [0],
        "source": ["tushare"],
    })
    norm = normalize_units(df)
    assert norm.iloc[0]["amount"] == 1020.0 * 1000.0
    assert norm.iloc[0]["volume"] == 100.0 * 100.0


def test_normalize_units_no_source_unchanged():
    df = pd.DataFrame({
        "volume": [100.0], "amount": [1000.0],
    })  # 无 source 列 → 保守不转换
    norm = normalize_units(df)
    assert norm.iloc[0]["volume"] == 100.0
