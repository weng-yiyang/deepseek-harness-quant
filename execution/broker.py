# -*- coding: utf-8 -*-
"""execution/broker.py — Broker 抽象接口（Phase 2 仿真券商 / 未来真实券商共用）

设计：抽象形态与真实券商一致，Phase 6 接真实券商时只需实现 submit/cancel/
get_positions/get_cash，OMS/execution_loop 不变（单一路径，防模拟/实盘两套逻辑）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

from execution.models import Order, Fill, MarketContext


class Broker(ABC):
    @abstractmethod
    def submit(self, order: Order, ctx: MarketContext) -> Fill:
        """撮合一笔订单，返回 Fill（成交或拒单）。不修改账户，由 OMS 应用成交。"""
        ...

    @abstractmethod
    def cancel(self, order_id: int) -> bool:
        """撤单（仿真无待撮合队列，恒成功；真实券商按 API）。"""
        ...

    @abstractmethod
    def get_positions(self) -> Dict[str, int]:
        """当前持仓 {code: qty}。"""
        ...

    @abstractmethod
    def get_cash(self) -> float:
        """当前可用现金。"""
        ...
