# -*- coding: utf-8 -*-
"""risk.risk_agent 审核逻辑测试（需 scipy；网络无关）。"""
import pytest

from risk.risk_agent import RiskAgent, Decision


@pytest.fixture
def agent():
    return RiskAgent({
        "max_position_pct": 0.10,
        "max_symbol_exposure": 0.20,
        "max_industry_pct": 0.30,
        "max_total_exposure": 0.80,
        "drawdown_levels": {"warning": 0.05, "control": 0.10, "circuit_breaker": 0.15},
    })


def test_normal_approve(agent):
    r = agent.check_order("A", 0.10, {}, 0.02)
    assert r.decision == Decision.APPROVE


def test_oversize_reduce(agent):
    r = agent.check_order("A", 0.15, {}, 0.02)
    assert r.decision == Decision.REDUCE
    assert r.adjusted_size == 0.10


def test_symbol_concentration_reduce(agent):
    r = agent.check_order("A", 0.10, {"A": 0.15}, 0.02)
    assert r.decision == Decision.REDUCE
    assert r.adjusted_size == pytest.approx(0.05)


def test_circuit_breaker_reject(agent):
    r = agent.check_order("A", 0.05, {}, 0.16)
    assert r.decision == Decision.REJECT


def test_drawdown_state_machine(agent):
    assert agent.check_drawdown(0.02) == "normal"
    assert agent.check_drawdown(0.06) == "reduce_risk"
    assert agent.check_drawdown(0.12) == "stop_new_positions"
    assert agent.check_drawdown(0.16) == "circuit_breaker"
