# -*- coding: utf-8 -*-
"""risk.data_audit 审计器测试（网络无关，使用合成 SQLite）。

构造一个"足够干净"的 bars.db（含一只 ST 股以满足 ST 标记有效性检查），
断言 DataAuditor 能正常运行且不产生 FAIL（即闸门放行）。
同时验证故意破损的数据库会触发 FAIL（闸门阻断）——这是实盘前置安全网。
"""
import sqlite3

import pytest

from risk.data_audit import DataAuditor, AuditBlocked, STOP_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
    preclose REAL, volume REAL, amount REAL, turn REAL, pct_chg REAL, is_st INTEGER,
    adjust TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (code, date, adjust));
CREATE TABLE IF NOT EXISTS bar_meta (
    code TEXT NOT NULL, adjust TEXT NOT NULL, start_date TEXT, end_date TEXT,
    rows INTEGER, updated_at TEXT, PRIMARY KEY (code, adjust));
"""


def _build_clean_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    rows = [
        # 600519.SH 正常股，两天
        ("600519.SH", "2024-01-02", 10.0, 10.5, 9.8, 10.2, 10.0, 1000.0, 10200.0, 1.0, 2.0, 0, "qfq", "baostock"),
        ("600519.SH", "2024-01-03", 10.5, 11.0, 10.2, 10.8, 10.2, 1100.0, 11880.0, 1.1, 5.88, 0, "qfq", "baostock"),
        # 600001.SH ST 股（is_st=1），满足 ST 标记有效性
        ("600001.SH", "2024-01-02", 5.0, 5.2, 4.8, 5.0, 4.9, 200.0, 1000.0, 1.0, 2.0, 1, "qfq", "baostock"),
    ]
    con.executemany(
        "INSERT OR REPLACE INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO bar_meta (code,adjust,start_date,end_date,rows,updated_at) "
                "VALUES ('600519.SH','qfq','2024-01-02','2024-01-03',2,'2024-01-03')")
    con.execute("INSERT OR REPLACE INTO bar_meta (code,adjust,start_date,end_date,rows,updated_at) "
                "VALUES ('600001.SH','qfq','2024-01-02','2024-01-02',1,'2024-01-02')")
    con.commit()
    con.close()


def _build_broken_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    # 重复主键（B1 FAIL）+ 周末日期（B3 FAIL）+ 价格为负（C1 FAIL）
    rows = [
        ("600519.SH", "2024-01-06", 10.0, 10.5, 9.8, 10.2, 10.0, 1000.0, 10200.0, 1.0, 2.0, 0, "qfq", "baostock"),
        ("600519.SH", "2024-01-06", 10.0, 10.5, 9.8, 10.2, 10.0, 1000.0, 10200.0, 1.0, 2.0, 0, "qfq", "baostock"),
        ("600519.SH", "2024-01-07", -1.0, -0.5, -2.0, -1.0, 10.0, 1000.0, 10200.0, 1.0, 2.0, 0, "qfq", "baostock"),
    ]
    con.executemany(
        "INSERT OR REPLACE INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO bar_meta (code,adjust,start_date,end_date,rows,updated_at) "
                "VALUES ('600519.SH','qfq','2024-01-06','2024-01-07',3,'2024-01-07')")
    con.commit()
    con.close()


def test_auditor_clean_db_passes(tmp_path, monkeypatch):
    _build_clean_db(tmp_path / "bars.db")
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    res = DataAuditor({}).run(quick=True)
    assert res["n_fail"] == 0, {i["id"]: i["status"] for i in res["items"] if i["status"] != "PASS"}
    assert res["blocked"] is False


def test_auditor_broken_db_blocks(tmp_path, monkeypatch):
    _build_broken_db(tmp_path / "bars.db")
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    res = DataAuditor({}).run(quick=True)
    assert res["n_fail"] > 0
    assert res["blocked"] is True


def test_gate_blocks_on_broken_db(tmp_path, monkeypatch):
    _build_broken_db(tmp_path / "bars.db")
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    ok, res = DataAuditor({}).gate()
    assert ok is False
    assert res["blocked"] is True


def test_assert_audit_passes_raises_on_broken_db(tmp_path, monkeypatch):
    _build_broken_db(tmp_path / "bars.db")
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    with pytest.raises(AuditBlocked):
        DataAuditor({}).assert_audit_passes(quick=True, context="单测")


def test_stop_md_written_on_broken_db(tmp_path, monkeypatch):
    _build_broken_db(tmp_path / "bars.db")
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    try:
        DataAuditor({}).run(quick=True)
        assert STOP_FILE.exists(), "审计 FAIL 时应写入 STOP.md 熔断文件"
        content = STOP_FILE.read_text(encoding="utf-8")
        assert "阻断" in content or "STOP" in content
        # STOP.md 新鲜 → is_stop_active 应为 True
        assert DataAuditor.is_stop_active() is True
    finally:
        STOP_FILE.unlink(missing_ok=True)


def test_f3_alignment_detects_mismatch(tmp_path, monkeypatch):
    """F-3：backtest.start 早于数据实际起点 → 应产生 F3 对齐告警（WARN/FAIL）。"""
    _build_clean_db(tmp_path / "bars.db")  # 数据起点 2024-01-02
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    # backtest.start 设为 2010-01-01（早于数据起点约 14 年）→ FAIL（>365 天按配置错误计）
    res = DataAuditor({"backtest_start": "2010-01-01"}).run(quick=True)
    f3 = [i for i in res["items"] if i["id"] == "F3"]
    assert f3, "应存在 F3 回测起点对齐检查项"
    assert f3[0]["status"] in ("WARN", "FAIL"), f3[0]
    # 未配置 backtest_start 时不应产生 F3（默认跳过）
    res2 = DataAuditor({}).run(quick=True)
    assert not [i for i in res2["items"] if i["id"] == "F3"]
