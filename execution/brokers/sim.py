# -*- coding: utf-8 -*-
"""execution/brokers/sim.py — 仿真券商（A股撮合建模，Phase 2 默认实现）

规则（尽量贴近 A股，确定性可复现）：
- 无行情 / 停牌 / 退市 → 拒单
- 涨停（high >= 涨停价）→ 买入拒单（无卖方）；跌停（low <= 跌停价）→ 卖出拒单（无买方）
- 成交价默认 = 当日收盘价（低频季度调仓，收盘撮合足够；滑点已在成本模型覆盖）
- 现金不足（买入）→ 部分成交到可负担数量；可负担为 0 → 拒单（INSUFFICIENT_CASH）
- 持仓不足（卖出）→ 拒单（INSUFFICIENT_POSITION）
- 不做随机噪声，保证回测 / 仿真可复现（与回测口径一致）

注意：本券商只做"撮合判定 + 返回 Fill"，**不修改账户**；成交由 OMS 应用到
PaperAccount（单一路径）。fee 由 PaperAccount 统一计算，本类返回 fee=0 仅作占位。
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from execution.broker import Broker
from execution.models import Order, Fill, MarketContext, RejectReason, Side

COMMISSION = 0.00026
STAMP_TAX = 0.0005


class SimBroker(Broker):
    def __init__(self, cash_provider: Optional[Callable[[], float]] = None,
                 position_provider: Optional[Callable[[str], int]] = None):
        # cash_provider / position_provider：撮合时读取最新现金/持仓的回调（由 OMS 注入 PaperAccount）
        self._cash = cash_provider
        self._pos = position_provider

    def _avail_cash(self, fallback: float) -> float:
        return self._cash() if callable(self._cash) else fallback

    def _avail_pos(self, code: str, fallback: int) -> int:
        return self._pos(code) if callable(self._pos) else fallback

    def submit(self, order: Order, ctx: MarketContext) -> Fill:
        oid = order.id or 0
        # 1) 基础可交易性
        if ctx is None or ctx.close <= 0:
            return Fill(oid, order.code, order.side, 0, 0.0,
                        reject_reason=RejectReason.NO_MARKET_DATA)
        if ctx.delisted:
            return Fill(oid, order.code, order.side, 0, 0.0,
                        reject_reason=RejectReason.DELISTED)
        if ctx.halted:
            return Fill(oid, order.code, order.side, 0, 0.0,
                        reject_reason=RejectReason.HALTED)
        # 2) 涨跌停
        if order.side == Side.BUY and ctx.is_limit_up:
            return Fill(oid, order.code, order.side, 0, ctx.close,
                        reject_reason=RejectReason.LIMIT_UP)
        if order.side == Side.SELL and ctx.is_limit_down:
            return Fill(oid, order.code, order.side, 0, ctx.close,
                        reject_reason=RejectReason.LIMIT_DOWN)
        # 3) 成交价：默认收盘价；若有限价且未越界则取限价
        price = ctx.close
        if order.limit_price:
            lp = float(order.limit_price)
            if order.side == Side.BUY and lp < price:
                price = lp
            elif order.side == Side.SELL and lp > price:
                price = lp
        # 4) 资金 / 持仓约束（部分成交）
        if order.side == Side.BUY:
            fee_rate = COMMISSION
            avail = self._avail_cash(order.qty * price * 10)
            max_affordable = int(avail // (price * (1 + fee_rate))) if price > 0 else 0
            fill_qty = min(order.qty, max_affordable)
            if fill_qty <= 0:
                return Fill(oid, order.code, order.side, 0, price,
                            reject_reason=RejectReason.INSUFFICIENT_CASH)
            return Fill(oid, order.code, order.side, fill_qty, price,
                        fee=0.0, note="sim-close")
        else:  # SELL
            avail = self._avail_pos(order.code, order.qty)
            fill_qty = min(order.qty, avail)
            if fill_qty <= 0:
                return Fill(oid, order.code, order.side, 0, price,
                            reject_reason=RejectReason.INSUFFICIENT_POSITION)
            return Fill(oid, order.code, order.side, fill_qty, price,
                        fee=0.0, note="sim-close")

    def cancel(self, order_id: int) -> bool:
        return True  # 仿真无待撮合队列，恒成功

    def get_positions(self) -> Dict[str, int]:
        return {}

    def get_cash(self) -> float:
        return self._avail_cash(0.0)
