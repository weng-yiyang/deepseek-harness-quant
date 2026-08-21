# -*- coding: utf-8 -*-
"""tests/test_fix_st_tushare.py — 验证 Tushare 版 F-1/F-2 修复逻辑（mock tushare，无需真实网络/token）

证明：
  (1) 构造一个 daily_bar.is_st 全 0（复现 baostock bug）且缺退市股的合成库
      → 审计闸门 C5(ST) FAIL / A3(退市覆盖) FAIL（符合现状）。
  (2) mock fetcher_tushare._pro 返回 stock_st / pro_bar 假数据，跑：
        data/fix_st_flags_tushare.py   （F-1：重置+按 ST 区间置 is_st=1）
        data/backfill_delisted_tushare.py（F-2：pro_bar 回填退市股 + 同源 is_st 标注）
      → 审计闸门整体 PASS，且 C5 / A3 专项均 PASS
      → 证明 tushare 链路在"逻辑正确"前提下能让审计真正转 PASS（真实数据只需替换 mock 为真实接口）。

全程 mock，不依赖 Tushare/网络/ token，沙箱稳定复跑。
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.cache as cache_mod
from data.cache import DailyCache
import data.fix_st_flags_tushare as f1
import data.backfill_delisted_tushare as f2
from risk.data_audit import DataAuditor


def _point_cache_to(tmp_path: Path):
    """data/cache.py 在 import 时冻结 CACHE_DIR/DEFAULT_DB；测试中需把缓存根指向临时目录。
    （生产环境因环境变量在进程启动前已设置，无需此操作）"""
    cache_mod.CACHE_DIR = tmp_path
    cache_mod.DEFAULT_DB = tmp_path / "bars.db"
    cache_mod.INC_DB = tmp_path / "bars_incr.db"


def _weekday_dates(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _stock_df(code, dates, st_days=()):
    rows = []
    prev = 10.0
    for i, dt in enumerate(dates):
        close = max(1.0, prev * 1.01)
        open_ = close * 0.999
        high = max(open_, close) * 1.01
        low = min(open_, close) * 0.99
        vol = 1_000_000.0
        rows.append({
            "date": dt, "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2),
            "close": round(close, 2), "preclose": round(prev, 2), "volume": vol,
            "amount": vol * close, "turn": 1.0, "pct_chg": round((close / prev - 1) * 100, 2),
            "is_st": 1 if i in st_days else 0,
        })
        prev = close
    return pd.DataFrame(rows)


def _build_synthetic_db(cache_dir: Path):
    """3 只普通股票（is_st 全 0，复现 baostock bug）+ 无退市股；写到 cache_dir/bars.db"""
    cache = DailyCache(str(cache_dir / "bars.db"))
    dates = _weekday_dates(date(2024, 1, 2), 20)
    for i in range(3):
        code = f"60000{i}.SH"
        df = _stock_df(code, dates)  # 初始 is_st 全 0
        cache.put_daily(code, df, adjust="qfq", source="baostock")
    # delisted_list.csv：1 只 2019 后退市股（600068.SH，2021-09-13 终止）
    csv_path = cache_dir / "delisted_list.csv"
    csv_path.write_text(
        "code,证券代码,公司简称,上市日期,终止上市日期,暂停上市日期\n"
        "600068,600068.SH,葛洲坝,19970915,2021-09-13,\n",
        encoding="utf-8-sig")
    return dates


def _make_mock_pro():
    del_dates = ["2021-09-01", "2021-09-02", "2021-09-03",
                 "2021-09-06", "2021-09-07", "2021-09-08"]
    del_df = pd.DataFrame({
        "trade_date": del_dates,
        "open": [9.0, 9.1, 9.2, 9.3, 9.4, 9.5],
        "high": [9.5, 9.6, 9.7, 9.8, 9.9, 10.0],
        "low": [8.5, 8.6, 8.7, 8.8, 8.9, 9.0],
        "close": [9.2, 9.3, 9.4, 9.5, 9.6, 9.7],
        "pre_close": [9.1, 9.2, 9.3, 9.4, 9.5, 9.6],
        "vol": [100.0, 110.0, 120.0, 130.0, 140.0, 150.0],
        "amount": [920.0, 1023.0, 1128.0, 1235.0, 1344.0, 1455.0],
        "pct_change": [1.1, 1.08, 1.07, 1.06, 1.05, 1.04],
        "turn": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
    })
    st_df = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "stock_st": [1],
        "start_date": ["2024-01-10"],
        "end_date": ["2024-01-20"],
        "reason": ["实施其他风险警示"],
    })
    pro = MagicMock()
    pro.stock_st = lambda **kw: st_df
    pro.pro_bar = lambda **kw: del_df
    return pro


def test_tushare_f1_f2_turns_gate_pass(tmp_path, monkeypatch):
    cache_dir = tmp_path
    _point_cache_to(cache_dir)
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(cache_dir))
    _build_synthetic_db(cache_dir)

    # 修复前：C5(ST 全 0) FAIL、A3(退市股未入库) FAIL → 闸门 FAIL
    ok_before, res_before = DataAuditor({}).gate()
    assert ok_before is False
    before_ids = {i["id"]: i["status"] for i in res_before["items"]}
    assert before_ids.get("C5") == "FAIL", before_ids

    pro = _make_mock_pro()
    # 把 f2 的共享进度/失败文件重定向到临时目录（否则跨 run 续传会把本测试目标当成已完成）
    f2.PROGRESS_FILE = cache_dir / "backfill_progress.txt"
    f2.FAILED_FILE = cache_dir / "backfill_failed.csv"
    with patch.object(f1, "get_stock_st_intervals", return_value={"600000.SH": [("2024-01-10", "2024-01-20")]}), \
         patch.object(f2, "_pro", return_value=pro), \
         patch.object(f2, "get_stock_st_intervals", return_value={"600000.SH": [("2024-01-10", "2024-01-20")]}):
        f1.run(dry_run=False)
        f2.run(dry_run=False)

    # 修复后：整体闸门必须 PASS，且 C5 / A3 专项 PASS
    ok, res = DataAuditor({}).gate()
    status_map = {i["id"]: i["status"] for i in res["items"]}
    assert ok is True, {i["id"]: (i["status"], i["detail"]) for i in res["items"] if i["status"] != "PASS"}
    assert status_map["C5"] == "PASS", status_map
    assert status_map["A3"] == "PASS", status_map
    assert res["n_fail"] == 0


def test_tushare_f1_dry_run_counts(monkeypatch):
    d = Path(tempfile.mkdtemp())
    try:
        _point_cache_to(d)
        monkeypatch.setenv("LWQUANT_CACHE_DIR", str(d))
        _build_synthetic_db(d)
        with patch.object(f1, "get_stock_st_intervals", return_value={"600000.SH": [("2024-01-10", "2024-01-20")]}):
            f1.run(dry_run=True)  # 不应抛错，且不修改 is_st
        con = __import__("sqlite3").connect(str(d / "bars.db"))
        st = con.execute("SELECT COUNT(*) FROM daily_bar WHERE is_st!=0").fetchone()[0]
        con.close()
        assert st == 0, "dry-run 不应修改 is_st"
    finally:
        shutil.rmtree(d, ignore_errors=True)
