# -*- coding: utf-8 -*-
"""execution/risk_gate.py — 风控闸门适配层（Phase 3：把 RiskAgent 接到 OMS）

背景：risk/risk_agent.py 早已实现完整风控（熔断/回撤/单笔/集中度/行业/总仓位 + Kelly/
Van Tharp/ATR），但它工作在**净值占比**口径（size=0.10 表示净值 10%）；而 OMS 的订单是
**股数**。本模块负责两者翻译，并把 APPROVE/REDUCE/REJECT 映射回可执行的股数。

职责：
- 股数 → 占比：size = qty × price / equity
- 组合权重：current_portfolio = {code: 市值/净值}
- 当前回撤：从 equity_curve 峰值回撤计算
- 决策映射：APPROVE→原股数；REDUCE→缩量后股数（按整手向下取整）；REJECT→0
- **SELL 放行**：减仓不受仓位/集中度限制（熔断时也只可减仓），避免误伤卖出
- fail-closed：风控自身异常 → 按 REJECT 处理（风控故障不能跳过风控，原文原则4）

A股细节：买入按 100 股（1 手）整手对齐，缩量后不足 1 手 → REJECT。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from execution.models import Order, Side
from risk.risk_agent import RiskAgent, Decision

LOT_SIZE = 100  # A股 1 手 = 100 股

DEFAULT_RISK_CFG = {
    "max_position_pct": 0.10,        # 单笔上限 10%
    "max_symbol_exposure": 0.20,     # 单标的集中度 20%
    "max_industry_pct": 0.30,        # 行业 30%
    "max_total_exposure": 0.80,      # 总仓位 80%
    "drawdown_levels": {"warning": 0.05, "control": 0.10, "circuit_breaker": 0.15},
    "circuit_breaker_position": 0.30,
    "circuit_breaker_cooldown_days": 5,
}


@dataclass
class GateResult:
    decision: Decision
    reason: str
    qty: int = 0                      # 可执行股数（REJECT 时为 0）
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.decision in (Decision.APPROVE, Decision.REDUCE) and self.qty > 0


class RiskGate:
    """风控闸门：OMS 在送券商撮合前调用 check()，决定放行/缩量/否决。"""

    def __init__(self, account, config: Optional[dict] = None):
        self.account = account
        self.config = {**DEFAULT_RISK_CFG, **(config or {})}
        self.agent = RiskAgent(self.config)
        # 当日行情快照 {code: close}：用于估算持仓市值/净值（由 execution_loop 注入），
        # 缺失的标的会回退用成本价近似
        self.prices: Dict[str, float] = {}

    # ---------- 组合快照 ----------
    def equity(self, prices: Optional[Dict[str, float]] = None) -> float:
        """净值 = 现金 + 持仓市值（无价则用成本价近似）。"""
        try:
            cash = float(self.account.cash)
        except Exception:
            cash = 0.0
        pos_val = 0.0
        for p in self.account.positions():
            px = (prices or {}).get(p["code"])
            if px is None:
                px = p.get("avg_cost") or 0.0
            pos_val += float(px) * float(p["qty"])
        return cash + pos_val

    def portfolio_weights(self, prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """{code: 占净值比例}（不含本次订单）。"""
        eq = self.equity(prices)
        if eq <= 0:
            return {}
        w = {}
        for p in self.account.positions():
            px = (prices or {}).get(p["code"])
            if px is None:
                px = p.get("avg_cost") or 0.0
            w[p["code"]] = float(px) * float(p["qty"]) / eq
        return w

    def current_drawdown(self) -> float:
        """当前回撤 = (峰值净值 - 最新净值) / 峰值净值；无曲线→0。"""
        try:
            curve = self.account.equity_curve()
        except Exception:
            return 0.0
        if not curve:
            return 0.0
        vals = [float(c.get("total") or 0.0) for c in curve]
        vals = [v for v in vals if v > 0]
        if not vals:
            return 0.0
        peak, last = max(vals), vals[-1]
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - last) / peak)

    # ---------- 主入口 ----------
    def check(self, order: Order, price: float,
              prices: Optional[Dict[str, float]] = None) -> GateResult:
        """审核一笔订单，返回 GateResult（放行/缩量/否决）。"""
        # 卖出 = 减仓，风控不限制（熔断期只可减仓），由 OMS/broker 校验持仓是否足够
        if order.side == Side.SELL:
            return GateResult(Decision.APPROVE, "减仓放行（风控不限制卖出）",
                              order.qty, {"side": "SELL"})

        try:
            self.agent.check_cooldown()          # 刷新熔断冷静期状态
            prices = {**(prices or self.prices), order.code: price}
            eq = self.equity(prices)
            if eq <= 0 or price <= 0:
                return GateResult(Decision.REJECT, "净值或价格异常，风控拒绝（fail-closed）", 0)

            size = (order.qty * price) / eq
            portfolio = self.portfolio_weights(prices)
            dd = self.current_drawdown()

            res = self.agent.check_order(order.code, size, portfolio, dd,
                                         sector=order.reason or "")
            detail = {"size_pct": round(size, 4), "equity": round(eq, 2),
                      "drawdown": round(dd, 4), "portfolio": {k: round(v, 4) for k, v in portfolio.items()}}

            if res.decision == Decision.APPROVE:
                return GateResult(Decision.APPROVE, res.reason, order.qty, detail)

            if res.decision == Decision.REDUCE:
                adj_size = res.adjusted_size
                if adj_size is None or adj_size <= 0:
                    return GateResult(Decision.REJECT, f"{res.reason}（缩量后为 0，按否决处理）", 0, detail)
                raw_qty = int(adj_size * eq / price)
                adj_qty = self._to_lot(min(raw_qty, order.qty))
                if adj_qty <= 0:
                    return GateResult(Decision.REJECT,
                                      f"{res.reason}（缩量后不足 1 手 {LOT_SIZE} 股，按否决处理）",
                                      0, {**detail, "raw_qty": raw_qty})
                return GateResult(Decision.REDUCE, res.reason, adj_qty,
                                  {**detail, "raw_qty": raw_qty})

            return GateResult(Decision.REJECT, res.reason, 0, detail)
        except Exception as e:
            # 风控自身故障 → 安全模式：禁止新开仓（原文原则4）
            return GateResult(Decision.REJECT, f"风控异常，fail-closed 拒绝下单：{e}", 0)

    @staticmethod
    def _to_lot(qty: int, lot: int = LOT_SIZE) -> int:
        """向下取整到整手（A股买入最小 100 股）。"""
        if qty <= 0:
            return 0
        return (int(qty) // lot) * lot
