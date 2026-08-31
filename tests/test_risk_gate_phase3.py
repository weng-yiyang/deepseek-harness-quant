# -*- coding: utf-8 -*-
"""tests/test_risk_gate_phase3.py — Phase 3 风控闸门接线验证

把 risk/risk_agent.py（早已实现的熔断/回撤/单笔/集中度/总仓位）经 execution/risk_gate.py
接到 OMS。覆盖：正常放行 / 单笔超限缩量(REDUCE) / 总仓超限否决(REJECT) / 回撤控制线 /
熔断 / 卖出放行 / 整手对齐 / fail-closed / OMS 集成（拒单不下单、缩量成交）。

风控是"稳健实盘"的生死线：本文件即越界订单压测。
"""
import os
import tempfile

import pytest

from execution.models import (Order, OrderStatus, Side, RejectReason,
                              MarketContext)
from execution.risk_gate import RiskGate, GateResult, Decision
from execution.oms import OMS
from execution.brokers.sim import SimBroker
from strategy.paper_account import PaperAccount

CODE_A = "600519.SH"
CODE_B = "000858.SZ"


@pytest.fixture(autouse=True)
def _isolate_stop_md(monkeypatch):
    # 隔离：避免其他测试在 data/cache 残留的 STOP.md 影响本模块闸门判定
    monkeypatch.setattr("execution.oms.DataAuditor.is_stop_active",
                        staticmethod(lambda *a, **k: False))


@pytest.fixture
def env():
    tmp = tempfile.mkdtemp()
    acc = PaperAccount("demo", cash=1_000_000, db_path=os.path.join(tmp, "paper.db"))
    return {"tmp": tmp, "acc": acc, "oms_db": os.path.join(tmp, "oms.db")}


def _gate(acc):
    return RiskGate(acc)


def _broker_for(acc):
    return SimBroker(cash_provider=lambda: acc.cash,
                     position_provider=lambda c: next(
                         (p["qty"] for p in acc.positions() if p["code"] == c), 0))


def _mk_drawdown(acc, code, peak_px=100.0, drop_px=50.0, qty=9000):
    """造回撤：建仓→按 peak 计价→按跌后价计价。返回 (peak_equity, dd)"""
    acc.buy(code, qty, "2024-08-01", close=peak_px)
    acc.mark_to_market({code: peak_px}, "2024-08-01")
    acc.mark_to_market({code: drop_px}, "2024-08-02")
    return acc


# ---------- 1) 正常放行 ----------
def test_approve_normal(env):
    g = _gate(env["acc"])
    r = g.check(Order(1, CODE_A, Side.BUY, 500, "2024-08-06"), 100.0)  # 5万 = 5%
    assert r.decision == Decision.APPROVE and r.qty == 500 and r.ok


# ---------- 2) 单笔超限 → REDUCE 缩量 ----------
def test_reduce_on_single_position_limit(env):
    g = _gate(env["acc"])
    r = g.check(Order(1, CODE_A, Side.BUY, 2000, "2024-08-06"), 100.0)  # 20万 = 20% > 10%
    assert r.decision == Decision.REDUCE
    assert r.qty == 1000 and r.ok          # 缩到 10% 上限 = 1000 股
    assert r.detail["size_pct"] > 0.10


# ---------- 3) 整手对齐（A股 100 股） ----------
def test_lot_alignment(env):
    tmp = tempfile.mkdtemp()
    acc = PaperAccount("demo", cash=1_050_000, db_path=os.path.join(tmp, "p.db"))
    g = _gate(acc)
    # 单笔上限 10% × 1,050,000 / 100 = 1050 股 → 向下取整到 1000 股
    r = g.check(Order(1, CODE_A, Side.BUY, 5000, "2024-08-06"), 100.0)
    assert r.decision == Decision.REDUCE and r.qty == 1000
    assert r.qty % 100 == 0


# ---------- 4) 缩量后不足 1 手 → REJECT ----------
def test_reduce_below_one_lot_rejects(env):
    tmp = tempfile.mkdtemp()
    acc = PaperAccount("demo", cash=50_000, db_path=os.path.join(tmp, "p.db"))
    g = _gate(acc)
    # 上限 10% × 50,000 = 5,000 元 / 100 元每股 = 50 股 < 1 手
    r = g.check(Order(1, CODE_A, Side.BUY, 1000, "2024-08-06"), 100.0)
    assert r.decision == Decision.REJECT and r.qty == 0
    assert "不足 1 手" in r.reason


# ---------- 5) 总仓位超限 → REJECT ----------
def test_reject_on_total_exposure(env):
    acc = env["acc"]
    acc.buy(CODE_A, 7500, "2024-08-01", close=100.0)      # 已占用 75% 净值
    acc.mark_to_market({CODE_A: 100.0}, "2024-08-01")
    g = _gate(acc)
    # 900 股 = 9% < 单笔上限 10%（避开"单笔"分支），但总仓 75%+9%=84% > 80% → 总仓否决
    r = g.check(Order(1, CODE_B, Side.BUY, 900, "2024-08-06"), 100.0)
    assert r.decision == Decision.REJECT and "总仓位" in r.reason


# ---------- 6) 回撤超控制线 → REJECT ----------
def test_reject_on_drawdown_control_line(env):
    acc = _mk_drawdown(env["acc"], CODE_A, 100.0, 86.67)   # dd ≈ 12% > 控制线 10%
    g = _gate(acc)
    assert 0.10 < g.current_drawdown() < 0.15
    r = g.check(Order(1, CODE_B, Side.BUY, 100, "2024-08-06"), 100.0)
    assert r.decision == Decision.REJECT and "回撤" in r.reason


# ---------- 7) 熔断：触发 + 后续买入一律拒绝 ----------
def test_circuit_breaker_triggered_and_blocks_buy(env):
    acc = _mk_drawdown(env["acc"], CODE_A, 100.0, 50.0)    # dd ≈ 45% > 熔断线 15%
    g = _gate(acc)
    assert g.current_drawdown() >= 0.15
    r1 = g.check(Order(1, CODE_B, Side.BUY, 100, "2024-08-06"), 100.0)
    assert r1.decision == Decision.REJECT
    assert g.agent.is_circuit_breaker_active is True       # 已触发熔断
    r2 = g.check(Order(2, CODE_B, Side.BUY, 100, "2024-08-06"), 100.0)
    assert r2.decision == Decision.REJECT and "熔断" in r2.reason


# ---------- 8) 卖出（减仓）不受风控限制，熔断期仍放行 ----------
def test_sell_always_allowed_even_in_circuit_breaker(env):
    acc = _mk_drawdown(env["acc"], CODE_A, 100.0, 50.0)
    g = _gate(acc)
    g.agent.is_circuit_breaker_active = True               # 强制熔断态
    r = g.check(Order(1, CODE_A, Side.SELL, 9000, "2024-08-06"), 50.0)
    assert r.decision == Decision.APPROVE and r.qty == 9000


# ---------- 9) fail-closed：风控自身异常 → 拒绝下单 ----------
def test_failclosed_on_risk_error(env, monkeypatch):
    g = _gate(env["acc"])
    monkeypatch.setattr(g.agent, "check_order",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("风控模块炸了")))
    r = g.check(Order(1, CODE_A, Side.BUY, 100, "2024-08-06"), 100.0)
    assert r.decision == Decision.REJECT and r.qty == 0
    assert "fail-closed" in r.reason


# ---------- 10) OMS 集成：风控否决 → 订单拒单且不下单 ----------
def test_oms_risk_reject_blocks_order(env):
    acc = env["acc"]
    acc.buy(CODE_A, 7500, "2024-08-01", close=100.0)       # 占用 75%
    acc.mark_to_market({CODE_A: 100.0}, "2024-08-01")
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=True,
              risk_gate=_gate(acc))
    o = oms.create_order(CODE_B, "BUY", 900, "2024-08-06")    # 9% 避开单笔上限，但总仓 84%>80%
    oms.approve(o.id, by="auto-sim")
    ctx = MarketContext(date="2024-08-06", code=CODE_B,
                        open=100, high=100, low=100, close=100, preclose=100)
    f = oms.submit(o.id, _broker_for(acc), ctx)
    assert not f.ok and f.reject_reason == RejectReason.RISK_REJECT
    assert oms.get(o.id).status == OrderStatus.REJECTED
    codes = [p["code"] for p in acc.positions()]
    assert CODE_B not in codes          # 未建仓


# ---------- 11) OMS 集成：风控缩量 → 按缩小后的股数成交 ----------
def test_oms_risk_reduce_fills_smaller_qty(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=True,
              risk_gate=_gate(acc))
    o = oms.create_order(CODE_A, "BUY", 2000, "2024-08-06")   # 20% → 缩到 10%
    oms.approve(o.id, by="auto-sim")
    ctx = MarketContext(date="2024-08-06", code=CODE_A,
                        open=100, high=100, low=100, close=100, preclose=100)
    f = oms.submit(o.id, _broker_for(acc), ctx)
    assert f.ok and f.filled_qty == 1000                       # 缩量后成交
    assert oms.get(o.id).status == OrderStatus.FILLED
    assert acc.positions()[0]["qty"] == 1000


# ---------- 12) 回归保护：未接风控时行为不变（Phase 2 兼容） ----------
def test_no_risk_gate_keeps_phase2_behavior(env):
    acc = env["acc"]
    oms = OMS(acc, db_path=env["oms_db"], allow_auto_approve=True, risk_gate=None)
    o = oms.create_order(CODE_A, "BUY", 2000, "2024-08-06")    # 20% 但不缩量
    oms.approve(o.id, by="auto-sim")
    ctx = MarketContext(date="2024-08-06", code=CODE_A,
                        open=100, high=100, low=100, close=100, preclose=100)
    f = oms.submit(o.id, _broker_for(acc), ctx)
    assert f.ok and f.filled_qty == 2000


# ---------- 13) 越界压测：一笔买光账户被拦 ----------
def test_extreme_order_blocked(env):
    acc = env["acc"]
    g = _gate(acc)
    # 试图用全部现金买入（100% 净值）→ 必须被 REDUCE 到单笔上限 10%
    r = g.check(Order(1, CODE_A, Side.BUY, 10000, "2024-08-06"), 100.0)
    assert r.decision == Decision.REDUCE
    assert r.qty == 1000                    # 10% × 100万 / 100 = 1000 股
    assert r.qty * 100 <= 0.10 * g.equity() + 1     # 不超过单笔上限
