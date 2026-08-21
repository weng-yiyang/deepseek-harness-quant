# -*- coding: utf-8 -*-
"""execution/models.py — 订单/成交/市场上下文数据模型（执行层通用）

Phase 2：仿真券商 + OMS。所有执行层模块共用此处定义，确保
"模拟一套 / 实盘一套" 接口一致（Broker 抽象形态与真实券商对齐）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderStatus(str, Enum):
    DRAFT = "DRAFT"                            # 草稿（已建单，待预审）
    PENDING_APPROVAL = "PENDING_APPROVAL"      # 待人工审批
    APPROVED = "APPROVED"                      # 已审批（可送券商）
    SENT = "SENT"                              # 已送券商（撮合中）
    FILLED = "FILLED"                          # 已成交（含部分成交）
    REJECTED = "REJECTED"                      # 被拒（预审/风控/券商）
    CANCELLED = "CANCELLED"                    # 已撤单


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RejectReason(str, Enum):
    NO_MARKET_DATA = "NO_MARKET_DATA"          # 无当日行情
    HALTED = "HALTED"                          # 停牌
    DELISTED = "DELISTED"                      # 退市
    ST_FILTER = "ST_FILTER"                    # ST 名称盘前过滤
    LIMIT_UP = "LIMIT_UP"                      # 涨停无法买入（无卖方）
    LIMIT_DOWN = "LIMIT_DOWN"                  # 跌停无法卖出（无买方）
    T1_SELL = "T1_SELL"                        # T+1 当日买入不可卖
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"    # 现金不足（部分成交后仍为 0）
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"  # 持仓不足
    AUDIT_FAIL = "AUDIT_FAIL"                  # 数据审计未通过（脏数据不下单）
    HUMAN_REJECTED = "HUMAN_REJECTED"          # 人工驳回
    UNKNOWN = "UNKNOWN"


@dataclass
class Order:
    account: str
    code: str
    side: Side
    qty: int
    date: str
    reason: str = ""
    limit_price: Optional[float] = None
    id: Optional[int] = None
    status: OrderStatus = OrderStatus.DRAFT

    def as_dict(self) -> dict:
        return {
            "id": self.id, "account": self.account, "code": self.code,
            "side": self.side.value, "qty": self.qty, "date": self.date,
            "reason": self.reason, "limit_price": self.limit_price,
            "status": self.status.value,
        }


@dataclass
class Fill:
    order_id: int
    code: str
    side: Side
    filled_qty: int
    price: float
    fee: float = 0.0
    reject_reason: Optional[RejectReason] = None
    ts: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.reject_reason is None and self.filled_qty > 0


@dataclass
class MarketContext:
    """当日市场上下文（仿真券商撮合所需），可由 daily_bar 行构造。"""
    date: str
    code: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    preclose: float = 0.0
    is_st: bool = False
    halted: bool = False
    delisted: bool = False
    adjust: str = "qfq"

    @property
    def limit_up_price(self) -> float:
        cap = 0.05 if self.is_st else 0.10
        return round(self.preclose * (1 + cap), 2)

    @property
    def limit_down_price(self) -> float:
        cap = 0.05 if self.is_st else 0.10
        return round(self.preclose * (1 - cap), 2)

    @property
    def is_limit_up(self) -> bool:
        return self.high >= self.limit_up_price - 1e-6

    @property
    def is_limit_down(self) -> bool:
        return self.low <= self.limit_down_price + 1e-6

    @classmethod
    def from_bar(cls, code: str, date: str, row: dict, is_st: bool = False,
                 halted: bool = False, delisted: bool = False) -> "MarketContext":
        return cls(
            date=date, code=code,
            open=row.get("open", 0.0), high=row.get("high", 0.0),
            low=row.get("low", 0.0), close=row.get("close", 0.0),
            preclose=row.get("preclose", 0.0), is_st=is_st,
            halted=halted, delisted=delisted, adjust=row.get("adjust", "qfq"),
        )
