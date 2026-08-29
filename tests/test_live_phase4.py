# -*- coding: utf-8 -*-
"""tests/test_live_phase4.py — Phase 4 盘前盘后编排验证

覆盖：deck 审批文件解析（JSONL / 数组 / 单对象 / 损坏）、交易日历（数据驱动 + 周末回退）、
盘后编排（dry-run / 幂等）、盘前编排（数据闸门阻断 / 非交易日 / 未审批不下单 /
只执行已审批且属候选的标的）。全部用临时文件与注入行情，不依赖真实数据与网络。
"""
import json
import os
import tempfile
from datetime import datetime

import pytest

from execution import execution_loop as el
from execution.models import MarketContext, OrderStatus
from live import trade_calendar as tc
from live import post_close as pc
from live import pre_market as pm
from strategy.paper_account import PaperAccount


@pytest.fixture(autouse=True)
def _isolate_stop_md(monkeypatch):
    # 隔离：避免 data/cache 残留 STOP.md 影响 OMS 审批
    monkeypatch.setattr("execution.oms.DataAuditor.is_stop_active",
                        staticmethod(lambda *a, **k: False))


def _ctx(code, date):
    return MarketContext(date=date, code=code, open=100, high=100,
                         low=100, close=100, preclose=100)


# ==================== 1) deck 审批文件解析（真实格式为 JSONL） ====================
def test_parse_deck_jsonl(tmp_path):
    """pitch_v2/Deck 实际写入格式：每行一个 JSON 对象"""
    f = tmp_path / "deck_decisions.json"
    f.write_text(
        '{"action":"buy","code":"600519.SH","qty":100,"reason":"审批买入"}\n'
        '{"action":"abandon","code":"000001.SZ"}\n', encoding="utf-8")
    plan = el.load_plan_from_deck(str(f))
    assert len(plan["orders"]) == 1
    assert plan["orders"][0]["code"] == "600519.SH"
    assert plan["orders"][0]["qty"] == 100


def test_parse_deck_json_array(tmp_path):
    f = tmp_path / "deck.json"
    f.write_text(json.dumps([
        {"action": "buy", "code": "600519.SH", "qty": 200},
        {"action": "buy", "code": "000858.SZ", "qty": 300},
    ]), encoding="utf-8")
    plan = el.load_plan_from_deck(str(f))
    assert len(plan["orders"]) == 2


def test_parse_deck_single_object(tmp_path):
    """单行 JSONL（只有一条决策）不能被误判为空"""
    f = tmp_path / "deck.json"
    f.write_text('{"action":"buy","code":"600519.SH","qty":100}', encoding="utf-8")
    plan = el.load_plan_from_deck(str(f))
    assert len(plan["orders"]) == 1


def test_parse_deck_wrapped_object(tmp_path):
    f = tmp_path / "deck.json"
    f.write_text(json.dumps({"decisions": [
        {"action": "buy", "code": "600519.SH", "qty": 100}]}), encoding="utf-8")
    assert len(el.load_plan_from_deck(str(f))["orders"]) == 1


def test_parse_deck_empty_and_broken(tmp_path):
    f1 = tmp_path / "empty.json"; f1.write_text("", encoding="utf-8")
    assert el.load_plan_from_deck(str(f1))["orders"] == []
    f2 = tmp_path / "broken.json"; f2.write_text("{not json at all", encoding="utf-8")
    assert el.load_plan_from_deck(str(f2))["orders"] == []


def test_parse_deck_missing_file():
    with pytest.raises(FileNotFoundError):
        el.load_plan_from_deck(str(tempfile.mkdtemp() + "/__none__.json"))


# ==================== 2) 交易日历 ====================
def test_next_trade_date_weekend_fallback(monkeypatch):
    """无行情数据 → 回退跳周末：周五(2024-08-02) → 周一(2024-08-05)"""
    monkeypatch.setattr(tc, "trade_dates", lambda: [])
    r = tc.next_trade_date("2024-08-02")
    assert r["date"] == "2024-08-05" and r["source"] == "weekday"


def test_next_trade_date_from_calendar(monkeypatch):
    """有行情数据 → 用真实交易日序列（天然含节假日）"""
    monkeypatch.setattr(tc, "trade_dates",
                        lambda: ["2024-08-01", "2024-08-02", "2024-08-05"])
    r = tc.next_trade_date("2024-08-02")
    assert r["date"] == "2024-08-05" and r["source"] == "calendar"


def test_next_trade_date_unknown_when_no_later(monkeypatch):
    monkeypatch.setattr(tc, "trade_dates", lambda: ["2024-08-01", "2024-08-02"])
    assert tc.next_trade_date("2024-08-05")["source"] == "unknown"


def test_is_trade_date(monkeypatch):
    monkeypatch.setattr(tc, "trade_dates", lambda: ["2024-08-01", "2024-08-02"])
    assert tc.is_trade_date("2024-08-02") is True
    assert tc.is_trade_date("2024-08-03") is False      # 周六，不在行情序列中


def test_previous_trade_date(monkeypatch):
    monkeypatch.setattr(tc, "trade_dates",
                        lambda: ["2024-08-01", "2024-08-02", "2024-08-05"])
    assert tc.previous_trade_date("2024-08-05")["date"] == "2024-08-02"


# ==================== 3) 盘后编排 ====================
def test_post_close_dry_run_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pc, "STATE_FILE", tmp_path / "state.json")
    s = pc.run(date="2024-08-02", dry_run=True)
    assert s["trade_date"] == "2024-08-02"
    assert all(st.get("dry_run") or st.get("skipped") for st in s["steps"])
    assert not (tmp_path / "next_day_plan_2024-08-02.json").exists()


def test_post_close_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pc, "STATE_FILE", tmp_path / "state.json")
    # 预置当日计划 → 幂等跳过（不重跑）
    (tmp_path / "next_day_plan_2024-08-02.json").write_text("{}", encoding="utf-8")
    s = pc.run(date="2024-08-02")
    assert s["skipped_idempotent"] is True


def test_post_close_force_regenerates(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pc, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "next_day_plan_2024-08-02.json").write_text("{}", encoding="utf-8")
    s = pc.run(date="2024-08-02", skip_refresh=True, force=True)
    assert s["skipped_idempotent"] is False
    assert (tmp_path / "next_day_plan_2024-08-02.json").exists()


# ==================== 4) 盘前编排 ====================
def _setup_pre_market(monkeypatch, tmp_path, deck_text, candidates, trade=True):
    deck = tmp_path / "deck_decisions.json"
    deck.write_text(deck_text, encoding="utf-8")
    monkeypatch.setattr(pm, "DECK", deck)
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pm, "is_trade_date", lambda d=None: trade)
    monkeypatch.setattr(pm, "_candidate_codes", lambda as_of: (candidates, "plan.json"))
    monkeypatch.setattr(el, "data_is_ok", lambda: True)
    acc_db = str(tmp_path / "paper.db")
    monkeypatch.setattr(pm, "PaperAccount",
                        lambda name: PaperAccount(name, cash=1_000_000, db_path=acc_db))
    return tmp_path / "oms.db"


def test_pre_market_blocked_by_data_gate(monkeypatch, tmp_path):
    _setup_pre_market(monkeypatch, tmp_path, '{"action":"buy","code":"600519.SH","qty":100}',
                      {"600519.SH"})
    monkeypatch.setattr(el, "data_is_ok", lambda: False)     # 审计未通过
    s = pm.run(date="2024-08-06", oms_db=str(tmp_path / "oms.db"))
    assert s["blocked"] is True and "审计" in s["block_reason"]


def test_pre_market_blocked_on_non_trade_day(monkeypatch, tmp_path):
    _setup_pre_market(monkeypatch, tmp_path, '{"action":"buy","code":"600519.SH","qty":100}',
                      {"600519.SH"}, trade=False)
    s = pm.run(date="2024-08-03", oms_db=str(tmp_path / "oms.db"))   # 周六
    assert s["blocked"] is True and "非交易日" in s["block_reason"]


def test_pre_market_blocked_without_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pm, "DECK", tmp_path / "__none__.json")      # 审批文件不存在
    monkeypatch.setattr(pm, "is_trade_date", lambda d=None: True)
    monkeypatch.setattr(el, "data_is_ok", lambda: True)
    s = pm.run(date="2024-08-06", oms_db=str(tmp_path / "oms.db"))
    assert s["blocked"] is True and "审批" in s["block_reason"]


def test_pre_market_executes_approved_candidate(monkeypatch, tmp_path):
    oms_db = _setup_pre_market(
        monkeypatch, tmp_path,
        '{"action":"buy","code":"600519.SH","qty":100,"reason":"审批"}\n'
        '{"action":"abandon","code":"000001.SZ"}\n',
        {"600519.SH"})
    s = pm.run(date="2024-08-06", ctx_provider=_ctx, oms_db=str(oms_db))
    assert s["blocked"] is False
    assert s["n_approved"] == 1            # abandon 被忽略
    assert s["n_executed"] == 1            # 已审批且属候选 → 成交


def test_pre_market_skips_non_candidate(monkeypatch, tmp_path):
    """审批了但不在盘后候选清单内 → 跳过（防审批了非候选标的）"""
    oms_db = _setup_pre_market(
        monkeypatch, tmp_path,
        '{"action":"buy","code":"600519.SH","qty":100}',
        {"000858.SZ"})                      # 候选里没有 600519
    s = pm.run(date="2024-08-06", ctx_provider=_ctx, oms_db=str(oms_db))
    assert s["n_executed"] == 0
    assert any(x["code"] == "600519.SH" for x in s["skipped"])


def test_pre_market_dry_run_does_not_trade(monkeypatch, tmp_path):
    oms_db = _setup_pre_market(
        monkeypatch, tmp_path,
        '{"action":"buy","code":"600519.SH","qty":100}', {"600519.SH"})
    s = pm.run(date="2024-08-06", dry_run=True, ctx_provider=_ctx, oms_db=str(oms_db))
    assert s["dry_run"] is True
    assert s["n_executed"] == 0             # 只列单，未真正下单
    assert "fills" not in s
