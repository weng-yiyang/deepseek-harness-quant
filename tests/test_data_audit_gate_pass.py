# -*- coding: utf-8 -*-
"""tests/test_data_audit_gate_pass.py — 验证"数据修复 → 审计 PASS"全链路（无需网络）

证明：
  (1) 构造一个各方面都合规的合成 bars.db → DataAuditor.gate() 必须返回 True（PASS），
      且没有任何 FAIL 项 → 证明审计闸门逻辑本身正确（真实数据修好后必然能 PASS）。
  (2) 构造一个脏库（B1 重复 / B3 周末 / B4 未来 / C3 负量额 / C5 ST 全 0）→ 闸门必须 FAIL
      → 证明硬闸门能拦住脏数据（与 test_data_audit 的 broken_db 互补，覆盖 ST 维度）。
  (3) 脏库经本地修复链（repair_consistency + recompute_bar_meta + 模拟 fix_st 置 3% is_st=1）
      → 闸门必须转 PASS → 证明 Phase 1 的"本地修复 → 放行"链路真实有效。

(1)(2)(3) 全部用合成 SQLite，不依赖 Tushare/baostock/网络，可在沙箱稳定复跑。
F-1/F-2/gen_delisted_list 的"真实数据拉取"部分仍需用户在本地用 token 执行 data/repair_phase1.py。
"""
import os
import random
import sqlite3
from datetime import date, timedelta

import pytest

from risk.data_audit import DataAuditor
from data.repair_consistency import repair
from data.recompute_bar_meta import recompute


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
    preclose REAL, volume REAL, amount REAL, turn REAL, pct_chg REAL, is_st INTEGER,
    adjust TEXT NOT NULL, source TEXT NOT NULL);
-- 注意：故意不设 PRIMARY KEY，以真实复现"重复行"脏数据（审计 B1 才能检出）；
-- 生产库若设了 PK，则需改为 INSERT OR REPLACE 才可能累积重复，本测试聚焦闸门判定逻辑。
CREATE TABLE IF NOT EXISTS bar_meta (
    code TEXT NOT NULL, adjust TEXT NOT NULL, start_date TEXT, end_date TEXT,
    rows INTEGER, updated_at TEXT, PRIMARY KEY (code, adjust));
"""


def _weekday_dates(start: date, n: int):
    """从 start 起取 n 个交易日（跳过周末）"""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:  # 0=Mon..4=Fri
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _build_valid_db(path, n_stocks=30, n_days=250, st_ratio=0.03, seed=42):
    """合规库：无重复/周末/未来/负值，OHLC 自洽，amount=volume*close，约 st_ratio 行为 ST。"""
    random.seed(seed)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    dates = _weekday_dates(date(2024, 1, 2), n_days)
    rows, meta = [], {}
    for i in range(n_stocks):
        code = f"{600000 + i}.SH"
        prev = 10.0 + random.uniform(0, 90)
        for j, dt in enumerate(dates):
            close = max(1.0, prev * (1 + random.uniform(-0.03, 0.03)))
            open_ = close * (1 + random.uniform(-0.01, 0.01))
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            preclose = prev
            volume = random.uniform(1e5, 5e6)
            amount = volume * close           # D1 量价比 = 1.0 ∈ [0.9,1.5]
            pct = (close / preclose - 1) * 100 if preclose > 0 else 0.0
            is_st = 1 if random.random() < st_ratio else 0
            rows.append((code, dt, round(open_, 2), round(high, 2), round(low, 2),
                         round(close, 2), round(preclose, 2), volume, amount,
                         round(random.uniform(0.5, 5.0), 2), round(pct, 2), is_st, "qfq", "baostock"))
            prev = close
        meta[code] = (len(dates), dates[0], dates[-1])
    con.executemany(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for code, (n, s, e) in meta.items():
        con.execute("INSERT INTO bar_meta (code,adjust,rows,start_date,end_date,updated_at) "
                    "VALUES (?,?,?,?,?,?)", (code, "qfq", n, s, e, "2024-12-31"))
    con.commit()
    con.close()


def test_gate_passes_on_valid_db(tmp_path, monkeypatch):
    """合规库 → 闸门必须 PASS，且无任何 FAIL 项"""
    db = tmp_path / "bars.db"
    _build_valid_db(db)
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    ok, res = DataAuditor({}).gate()
    assert ok is True, {i["id"]: i["status"] for i in res["items"] if i["status"] != "PASS"}
    assert res["n_fail"] == 0
    assert res["blocked"] is False


def _build_dirty_db(path):
    """脏库：含 B1 重复 / B3 周末 / B4 未来 / C3 负量额 / C5 ST 全 0"""
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    # 正常基底（10 只 × 20 交易日，OHLC 自洽合规）
    dates = _weekday_dates(date(2024, 1, 2), 20)
    base = []
    for i in range(10):
        code = f"{600000 + i}.SH"
        prev = 10.0
        for dt in dates:
            close = max(1.0, prev * 1.01)
            open_ = close * 0.999
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            base.append((code, dt, round(open_, 2), round(high, 2), round(low, 2),
                         close, prev, 1000.0, close * 1000.0,
                         1.0, 1.0, 0, "qfq", "baostock"))
            prev = close
    con.executemany(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", base)
    # B1 重复：再插一行完全相同的 (600000.SH, 第一天)
    con.execute(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES ('600000.SH',?,10.0,10.5,9.8,10.0,10.0,1000.0,10000.0,1.0,1.0,0,'qfq','baostock')",
        (dates[0],))
    # B3 周末：插一个周六
    sat = date(2024, 1, 6)  # 2024-01-06 是周六
    con.execute(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES ('600000.SH',?,10.0,10.5,9.8,10.0,10.0,1000.0,10000.0,1.0,1.0,0,'qfq','baostock')",
        (sat.isoformat(),))
    # B4 未来：插一个远未来日期
    fut = date(2099, 1, 1)
    con.execute(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES ('600000.SH',?,10.0,10.5,9.8,10.0,10.0,1000.0,10000.0,1.0,1.0,0,'qfq','baostock')",
        (fut.isoformat(),))
    # C3 负量额：插一行价格合规但 volume<0（审计 C3 仅查 volume/amount/turn 负值）
    con.execute(
        "INSERT INTO daily_bar "
        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
        "VALUES ('600000.SH','2024-03-01',10.0,10.5,9.8,10.0,10.0,-1000.0,10000.0,1.0,1.0,0,'qfq','baostock')")
    con.commit()
    con.close()


def test_gate_fails_on_dirty_db(tmp_path, monkeypatch):
    """脏库（B1/B3/B4/C3/C5）→ 闸门必须 FAIL"""
    db = tmp_path / "bars.db"
    _build_dirty_db(db)
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))
    ok, res = DataAuditor({}).gate()
    assert ok is False
    fail_ids = [i["id"] for i in res["items"] if i["status"] == "FAIL"]
    assert "B1" in fail_ids and "B3" in fail_ids and "B4" in fail_ids and "C3" in fail_ids and "C5" in fail_ids


def test_dirty_db_repaired_to_pass(tmp_path, monkeypatch):
    """脏库 → 本地修复链 → 闸门转 PASS（证明 Phase 1 修复链路有效）"""
    db = tmp_path / "bars.db"
    _build_dirty_db(db)
    monkeypatch.setenv("LWQUANT_CACHE_DIR", str(tmp_path))

    # 修复前必 FAIL
    assert DataAuditor({}).gate()[0] is False

    # 本地修复：清一致性脏行 + 重算 bar_meta
    repair(db, dry_run=False)
    recompute(db, dry_run=False)

    # 模拟 fix_st_flags（F-1）：将约 3% 行置 is_st=1（真实环境由 baostock 拉取）
    con = sqlite3.connect(str(db))
    total = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    target = max(1, int(total * 0.03))
    # 随机选 target 行置 ST（确定性：取前 target 只各取首日）
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar").fetchall()]
    done = 0
    for code in codes:
        if done >= target:
            break
        d0 = con.execute("SELECT MIN(date) FROM daily_bar WHERE code=?", (code,)).fetchone()[0]
        con.execute("UPDATE daily_bar SET is_st=1 WHERE code=? AND date=?", (code, d0))
        done += 1
    con.commit()
    con.close()

    # 修复后必须 PASS
    ok, res = DataAuditor({}).gate()
    assert ok is True, {i["id"]: (i["status"], i["detail"]) for i in res["items"] if i["status"] != "PASS"}
    assert res["n_fail"] == 0
