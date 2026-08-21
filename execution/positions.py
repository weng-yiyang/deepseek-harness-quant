# -*- coding: utf-8 -*-
"""execution/positions.py — 持仓对账（Phase 2）

复用 strategy/paper_account.PaperAccount 作为现金/持仓/净值唯一真相源
（"模拟一套 / 实盘一套" 防护）。本模块仅做查询/对账封装，未来接真实券商时
可用 broker.get_positions() 与本地持仓做日终核对（reconcile）。
"""
from __future__ import annotations

from typing import Dict


class PositionBook:
    def __init__(self, account):
        self.account = account

    def snapshot(self) -> dict:
        return self.account.snapshot()

    def positions(self) -> list:
        return self.account.positions()

    def equity_curve(self) -> list:
        return self.account.equity_curve()

    def mark_to_market(self, prices: Dict[str, float], date: str) -> float:
        return self.account.mark_to_market(prices, date)

    def reconcile(self, broker_positions: Dict[str, int]) -> Dict[str, tuple]:
        """与券商持仓对账，返回差异 {code: (本地qty, 券商qty)}。"""
        local = {p["code"]: p["qty"] for p in self.account.positions()}
        diffs = {}
        for code, q in broker_positions.items():
            if local.get(code, 0) != q:
                diffs[code] = (local.get(code, 0), q)
        for code, q in local.items():
            if code not in broker_positions:
                diffs[code] = (q, 0)
        return diffs
