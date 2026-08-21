# -*- coding: utf-8 -*-
"""tests/test_execution_phase2.py — Phase 2 执行层（仿真券商 + OMS）验证

覆盖：仿真券商撮合规则、OMS 生命周期、human-in-the-loop 审批闸门、盘前过滤、
接 Phase 1 数据闸门、全链路 auto_approve 闭环。全部用合成数据，无需网络/真实库。
"""
import os
import tempfile

import pytest

from execution.models import (Order, Fill, OrderStatus, Side, RejectReason,
                              MarketContext)
from execution.broker import Broker
from execution.brokers.sim import SimBroker
from execution.oms import OMS
from strategy.paper_account import PaperAccount


# ---------- 辅助：临时账户 + OMS ----------
@pytest.fixture(autouse=True)
def _isolate_stop_md(monkeypatch):
    # 隔离：避免其他测试在 data/cache 残留的 STOP.md 影响本模块闸门判定
    monkeypatch.setattr("execution.oms.DataAuditor.is_stop_active",
                        staticmethod(lambda *a, **k: False))


@pytest.fixture
def env():
    tmp = tempfile.mkdtemp()
    acc_db = os.path.join(tmp, "paper.db")
    oms_db = os.path.join(tmp, "oms.db")
    acc = PaperAccount("demo", cash=1_000_000, db_path=acc_db)
    return {"tmp": tmp, "acc": acc, "oms_db": oms_db}


def _broker_for(acc):
    return SimBroker(
        cash_provider=lambda: acc.cash,
        position_provider=lambda c: next(
            (p["qty"] for p in acc.positions() if p["code"] == c), 0))


# ---------- 1) 仿真券商：涨跌停 / 停牌 / 退市 ----------
def test_sim_limit_up_buy_rejected():
    b = SimBroker()
    ctx = MarketContext(date="2024-08-06", code="600000.SH", open=10, high=11,
                        low=9.9, close=11, preclose=10.0)  # 涨停（非ST +10%）
    assert ctx.is_limit_up
    f = b.submit(Order(1, "600000.SH", Side.BUY, 100, "2024-08-06"), ctx)
    assert not f.ok and f.reject_reason == RejectReason.LIMIT_UP


def test_sim_limit_down_sell_rejected():
    b = SimBroker()
    ctx = MarketContext(date="2024-08-06", code="600000.SH", open=9, high=9.1,
                        low=9.0, close=9.0, preclose=10.0)  # 跌停
    assert ctx.is_limit_down
    f = b.submit(Order(1, "600000.SH", Side.SELL, 100, "2024-08-06"), ctx)
    assert not f.ok and f.reject_reason == RejectReason.LIMIT_DOWN


def test_sim_halted_rejected():
    b = SimBroker()
    ctx = MarketContext(date="2024-08-06", code="600000.SH", close=10,
                        preclose=10, halted=True)
    f = b.submit(Order(1, "600000.SH", Side.BUY, 100, "2024-08-06"), ctx)
    assert not f.ok and f.reject_reason == RejectReason.HALTED


def test_sim_delisted_rejected():
    b = SimBroker()
    ctx = MarketContext(date="2024-08-06", code="600000.SH", close=10,
                        preclose=10, delisted=True)
    f = b.submit(Order(1, "600000.SH", Side.BUY, 100, "2024-08-06"), ctx)
    assert not f.ok and f.reject_reason == RejectReason.DELISTED


def test_sim_no_market_rejected():
    b = SimBroker()
    f = b.submit(Order(1, "600000.SH", Side.BUY, 100, "2024-08-06"),
                 MarketContext(date="2024-08-06", code="600000.SH", close=0.0))
    assert not f.ok and f.reject_reason == RejectReason.NO_MARKET_DATA


# ---------- 2) 仿真券商：正常成交 / 现金不足部分成交 ----------
def test_sim_normal_buy_fills_full(env):
    acc = env["acc"]
    b = _broker_for(acc)
    ctx = MarketContext(date="2024-08-06", code="600519.SH", open=100, high=105,
                        low=99, close=102, preclose=100)
    f = b.submit(Order(1, "600519.SH", Side.BUY, 100, "2024-08-06"), ctx)
    assert f.ok and f.filled_qty == 100 and f.price == 102


def test_sim_cash_partial_fill(env):
    acc = env["acc"]
    b = _broker_for(acc)
    # 账户 100 万，但 cash_provider 故意返回极小现金 → 部分成交
    b._cash = lambda: 500.0
    ctx = MarketContext(date="2024-08-06", code="600519.SH", close=100.0, preclose=100)
    f = b.submit(Order(1, "600519.SH", Side.BUY, 100, "2024-08-06"), ctx)
    assert f.ok and 0 < f.filled_qty < 100


# ---------- 3) OMS 生命周期 + human-in-the-loop ----------
def test_oms_draft_not_executed_without_approve(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=False)
    o = oms.create_order("600519.SH", "BUY", 100, "2024-08-06")
    assert o.status == OrderStatus.DRAFT
    # 未审批直接 submit 应被拒（状态非 APPROVED）
    ctx = MarketContext(date="2024-08-06", code="600519.SH", close=100, preclose=100)
    with pytest.raises(RuntimeError):
        oms.submit(o.id, _broker_for(acc), ctx)
    # 账户仍无持仓
    assert acc.positions() == []


def test_oms_human_approve_then_fill(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=False)
    o = oms.create_order("600519.SH", "BUY", 100, "2024-08-06")
    assert oms.approve(o.id, by="human") is True          # HITL 显式审批
    assert oms.get(o.id).status == OrderStatus.APPROVED
    ctx = MarketContext(date="2024-08-06", code="600519.SH", close=100, preclose=100)
    f = oms.submit(o.id, _broker_for(acc), ctx)
    assert f.ok and f.filled_qty == 100
    assert oms.get(o.id).status == OrderStatus.FILLED
    assert len(acc.positions()) == 1 and acc.positions()[0]["qty"] == 100


def test_oms_human_reject_not_executed(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=False)
    o = oms.create_order("600519.SH", "BUY", 100, "2024-08-06")
    assert oms.reject(o.id, by="human", reason="不在清单") is True
    assert oms.get(o.id).status == OrderStatus.REJECTED
    ctx = MarketContext(date="2024-08-06", code="600519.SH", close=100, preclose=100)
    with pytest.raises(RuntimeError):
        oms.submit(o.id, _broker_for(acc), ctx)


def test_oms_st_filter_preflight(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], st_filter=True)
    o = oms.create_order("600001.SH", "BUY", 100, "2024-08-06")
    ctx = MarketContext(date="2024-08-06", code="600001.SH", close=10,
                        preclose=9.5, is_st=True)
    assert oms.preflight(o, ctx) == RejectReason.ST_FILTER


# ---------- 4) OMS T+1 盘前过滤 ----------
def test_oms_t1_sell_rejected(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=True)
    # 当日买入
    oms.create_order("600519.SH", "BUY", 100, "2024-08-06")
    # 直接经由 account 建持仓（模拟买入成交）
    acc.buy("600519.SH", 100, "2024-08-06", close=100.0)
    # 当日卖出 → T+1 拦截
    o2 = oms.create_order("600519.SH", "SELL", 100, "2024-08-06")
    oms.approve(o2.id, by="auto-sim")
    ctx = MarketContext(date="2024-08-06", code="600519.SH",
                        open=100, high=100, low=100, close=100, preclose=100)
    f = oms.submit(o2.id, _broker_for(acc), ctx)
    assert not f.ok and f.reject_reason == RejectReason.T1_SELL


# ---------- 5) 接 Phase 1 数据闸门 ----------
def test_oms_blocked_when_stop_active(env, monkeypatch):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=True)
    monkeypatch.setattr("execution.oms.DataAuditor.is_stop_active",
                        staticmethod(lambda *a, **k: True))
    o = oms.create_order("600519.SH", "BUY", 100, "2024-08-06")
    with pytest.raises(Exception):   # AuditBlocked
        oms.approve(o.id, by="auto-sim")


def test_execution_loop_blocked_on_dirty_data(env, monkeypatch):
    from execution import execution_loop as el
    monkeypatch.setattr(el, "data_is_ok", lambda: False)
    acc = env["acc"]
    plan = {"account": "demo", "as_of_date": "2024-08-06",
            "orders": [{"code": "600519.SH", "side": "BUY", "qty": 100}]}
    s = el.run_plan(plan, account=acc, auto_approve=True, data_ok=False)
    assert s["blocked"] is True and s["filled"] == 0


# ---------- 6) 全链路 auto_approve（仿真自测） ----------
def test_execution_loop_auto_approve_end_to_end(env, monkeypatch):
    from execution import execution_loop as el
    monkeypatch.setattr(el, "data_is_ok", lambda: True)

    acc = env["acc"]
    # 注入行情上下文，避免读真实 bars.db
    def ctx_provider(code, date):
        base = {"date": date, "code": code, "open": 100, "high": 105,
                "low": 99, "close": 102, "preclose": 100}
        return MarketContext(**base)

    plan = {"account": "demo", "as_of_date": "2024-08-06", "orders": [
        {"code": "600519.SH", "side": "BUY", "qty": 100, "reason": "测试买入"},
        {"code": "000858.SZ", "side": "BUY", "qty": 200, "reason": "测试买入2"},
    ]}
    s = el.run_plan(plan, account=acc, ctx_provider=ctx_provider,
                    auto_approve=True, data_ok=True, oms_db=env["oms_db"])
    assert s["created"] == 2 and s["filled"] == 2
    assert len(acc.positions()) == 2
    # 净值已写入
    assert len(acc.equity_curve()) >= 1


# ---------- 7) Broker 抽象契约 ----------
def test_broker_is_abstract():
    with pytest.raises(TypeError):
        Broker()
