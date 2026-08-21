# -*- coding: utf-8 -*-
"""factors.factor_engine 面板计算与方向化测试（网络无关）。"""
import numpy as np
import pandas as pd

from factors.factor_engine import (
    compute_factor_panel,
    DEFAULT_DIRECTION,
    FACTOR_FUNCS,
)


def _make_closes(n_stocks=3, periods=60, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    return pd.DataFrame(
        {
            f"c{i}": 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, periods))
            for i in range(n_stocks)
        },
        index=dates,
    )


def test_panel_is_multiindex_with_factors():
    closes = _make_closes()
    panel = compute_factor_panel(closes)
    assert isinstance(panel.columns, pd.MultiIndex)
    factors = list(panel.columns.get_level_values(0).unique())
    assert "mom_20" in factors and "lowvol_60" in factors


def test_direction_sign_applied():
    # mom_20 默认 sign=-1 → 面板值应为原始值的负
    closes = _make_closes()
    panel = compute_factor_panel(closes, factors=["mom_20"])
    raw = FACTOR_FUNCS["mom_20"](closes.astype(float))
    got = panel.xs("mom_20", axis=1, level=0)
    pd.testing.assert_frame_equal(got, -raw)


def test_direction_override():
    # 覆盖为 +1：面板应等于原始值
    closes = _make_closes()
    panel = compute_factor_panel(closes, direction={"mom_20": 1}, factors=["mom_20"])
    raw = FACTOR_FUNCS["mom_20"](closes.astype(float))
    got = panel.xs("mom_20", axis=1, level=0)
    pd.testing.assert_frame_equal(got, raw)


def test_excluded_factor_skipped():
    # 方向为 0 的因子应被剔除，而方向非 0 的因子仍保留
    closes = _make_closes()
    panel = compute_factor_panel(
        closes, direction={"mom_20": 0, "lowvol_60": -1}, factors=["mom_20", "lowvol_60"])
    factors = list(panel.columns.get_level_values(0).unique())
    assert "mom_20" not in factors
    assert "lowvol_60" in factors
