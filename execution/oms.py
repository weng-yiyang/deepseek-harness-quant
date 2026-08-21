# -*- coding: utf-8 -*-
"""execution/oms.py — 订单管理系统（Order Management System, Phase 2）

职责：
- 订单生命周期：DRAFT → PENDING_APPROVAL → APPROVED → SENT → FILLED / REJECTED / CANCELLED
- 人工审批闸门（human-in-the-loop）：默认 DRAFT 需显式 approve() 才进入 SENT；未审批绝不下单。
  --auto-approve 仅供仿真自测（明确标注非真实资金）。
- 盘前过滤：ST / 停牌 / 退市 / 涨跌停 / T+1（当日买入不可卖）在 submit 前拦截。
- 接 Phase 1 数据闸门：脏数据（STOP.md 或审计 FAIL）拒绝审批与执行。
- 应用成交：FILLED 后调用 PaperAccount.buy/sell（单一路径，模拟/实盘一致）。
- 持久化：data/cache/oms.db，订单状态全程留痕（合规审计）。

CLI：
  python execution/oms.py list                 # 列出全部订单
  python execution/oms.py pending              # 列出待审批
  python execution/oms.py approve <id> [--by 人工]
  python execution/oms.py reject <id> --reason xxx
  python execution/oms.py cancel <id>
  python execution/oms.py status <id>
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 直接以脚本方式运行（python execution/oms.py）时，把仓库根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.models import (Order, Fill, OrderStatus, Side, RejectReason,
                              MarketContext)

try:
    from strategy.paper_account import PaperAccount
except Exception:  # 账户模块缺失时不影响 OMS 状态机（apply 时再报）
    PaperAccount = None

try:
    from risk.data_audit import DataAuditor, _load_config, AuditBlocked
except Exception:
    DataAuditor = None
    _load_config = lambda *a, **k: {}
    AuditBlocked = Exception

BASE = Path(__file__).resolve().parent.parent
OMS_DB = BASE / "data" / "cache" / "oms.db"

_COLS = ["id", "account", "code", "side", "qty", "date", "reason", "limit_price",
         "status", "created_at", "approved_at", "decided_by", "reject_reason",
         "fill_qty", "fill_price", "fill_fee", "fill_ts", "error"]


class OMS:
    def __init__(self, account: "PaperAccount", db_path=None, allow_auto_approve: bool = False,
                 st_filter: bool = True, data_ok: bool = True):
        self.account = account
        self.db_path = str(db_path or OMS_DB)
        self.allow_auto_approve = allow_auto_approve
        self.st_filter = st_filter          # 是否启用 ST 名称盘前过滤（默认开）
        self.data_ok = data_ok              # 外部（execution_loop）已跑过的数据闸门结果
        self._init_db()

    # ---------- 数据库 ----------
    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        con = self._conn()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT, code TEXT, side TEXT, qty INTEGER, date TEXT,
            reason TEXT, limit_price REAL, status TEXT,
            created_at TEXT, approved_at TEXT, decided_by TEXT,
            reject_reason TEXT, fill_qty INTEGER, fill_price REAL,
            fill_fee REAL, fill_ts TEXT, error TEXT
        );
        """)
        con.commit()
        con.close()

    # ---------- 建单 / 查询 ----------
    def create_order(self, code, side, qty, date, reason="", limit_price=None,
                     account: str = None) -> Order:
        side = Side(side) if not isinstance(side, Side) else side
        acc = account or (self.account.name if self.account else "default")
        con = self._conn()
        cur = con.execute(
            "INSERT INTO orders (account,code,side,qty,date,reason,limit_price,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (acc, code, side.value, qty, date, reason, limit_price,
             OrderStatus.DRAFT.value, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        oid = cur.lastrowid
        con.commit()
        con.close()
        return Order(id=oid, account=acc, code=code, side=side, qty=qty,
                     date=date, reason=reason, limit_price=limit_price,
                     status=OrderStatus.DRAFT)

    def get(self, oid: int) -> Order:
        con = self._conn()
        r = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        con.close()
        if not r:
            raise KeyError(f"订单 {oid} 不存在")
        d = dict(zip(_COLS, r))
        return Order(id=d["id"], account=d["account"], code=d["code"],
                     side=Side(d["side"]), qty=d["qty"], date=d["date"],
                     reason=d["reason"] or "", limit_price=d["limit_price"],
                     status=OrderStatus(d["status"]))

    def list(self, status: Optional[OrderStatus] = None, limit: int = 200) -> list:
        con = self._conn()
        if status:
            rows = con.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?",
                (status.value, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        con.close()
        return [Order(id=d["id"], account=d["account"], code=d["code"],
                      side=Side(d["side"]), qty=d["qty"], date=d["date"],
                      reason=d["reason"] or "", limit_price=d["limit_price"],
                      status=OrderStatus(d["status"]))
                for d in (dict(zip(_COLS, r)) for r in rows)]

    def pending(self, limit: int = 200) -> list:
        return self.list(OrderStatus.PENDING_APPROVAL, limit) + \
               self.list(OrderStatus.DRAFT, limit)

    # ---------- 审批闸门 (HITL) ----------
    def approve(self, oid: int, by: str = "human") -> bool:
        o = self.get(oid)
        if o.status not in (OrderStatus.DRAFT, OrderStatus.PENDING_APPROVAL):
            return False
        if not self.allow_auto_approve and by not in ("human", "人工"):
            # 非人工审批（如 auto）必须显式开启 allow_auto_approve
            return False
        if self._data_blocked():
            raise AuditBlocked(
                f"数据审计未通过 / STOP.md 存在 → 拒绝审批 {oid}（脏数据不下单）")
        con = self._conn()
        con.execute(
            "UPDATE orders SET status=?, approved_at=?, decided_by=? WHERE id=?",
            (OrderStatus.APPROVED.value, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             by, oid))
        con.commit()
        con.close()
        return True

    def reject(self, oid: int, by: str = "human", reason: str = "人工驳回") -> bool:
        o = self.get(oid)
        if o.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return False
        con = self._conn()
        con.execute(
            "UPDATE orders SET status=?, decided_by=?, reject_reason=? WHERE id=?",
            (OrderStatus.REJECTED.value, by, RejectReason.HUMAN_REJECTED.value, oid))
        con.commit()
        con.close()
        return True

    def cancel(self, oid: int) -> bool:
        o = self.get(oid)
        if o.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return False
        con = self._conn()
        con.execute("UPDATE orders SET status=? WHERE id=?",
                    (OrderStatus.CANCELLED.value, oid))
        con.commit()
        con.close()
        return True

    # ---------- 数据闸门 ----------
    def _data_blocked(self) -> bool:
        if not self.data_ok:
            return True
        try:
            if DataAuditor is not None and DataAuditor.is_stop_active():
                return True
        except Exception:
            return True
        return False

    # ---------- 盘前过滤 ----------
    def preflight(self, o: Order, ctx: Optional[MarketContext]) -> Optional[RejectReason]:
        """提交前拦截；None = 通过，否则返回拒单原因。"""
        if self._data_blocked():
            return RejectReason.AUDIT_FAIL
        if ctx is None or ctx.close <= 0:
            return RejectReason.NO_MARKET_DATA
        if ctx.delisted:
            return RejectReason.DELISTED
        if ctx.halted:
            return RejectReason.HALTED
        if ctx.is_st and self.st_filter:
            return RejectReason.ST_FILTER
        if o.side == Side.BUY and ctx.is_limit_up:
            return RejectReason.LIMIT_UP
        if o.side == Side.SELL and ctx.is_limit_down:
            return RejectReason.LIMIT_DOWN
        if o.side == Side.SELL:
            pos = self._position_of(o.code)
            if pos and pos.get("entry_date") == o.date:   # T+1：当日买入不可当日卖
                return RejectReason.T1_SELL
        return None

    def _position_of(self, code: str) -> Optional[dict]:
        if self.account is None:
            return None
        for p in self.account.positions():
            if p["code"] == code:
                return p
        return None

    # ---------- 提交 / 成交应用 ----------
    def _mark(self, oid: int, **fields):
        con = self._conn()
        keys = list(fields.keys())
        sets = ", ".join(f"{k}=?" for k in keys)
        con.execute(f"UPDATE orders SET {sets} WHERE id=?",
                    tuple(fields.values()) + (oid,))
        con.commit()
        con.close()

    def _mark_rejected(self, oid: int, reason: RejectReason):
        self._mark(oid, status=OrderStatus.REJECTED.value, reject_reason=reason.value)

    def _mark_filled(self, oid: int, fill: Fill):
        self._mark(oid, status=OrderStatus.FILLED.value, fill_qty=fill.filled_qty,
                   fill_price=fill.price, fill_fee=fill.fee, fill_ts=fill.ts)

    def submit(self, oid: int, broker, ctx: Optional[MarketContext]) -> Fill:
        """将已审批订单送券商撮合并应用成交。要求 status==APPROVED。"""
        o = self.get(oid)
        if o.status != OrderStatus.APPROVED:
            raise RuntimeError(f"订单 {oid} 状态为 {o.status.value}，未审批不可提交")
        reason = self.preflight(o, ctx)
        if reason is not None:
            self._mark_rejected(oid, reason)
            price = ctx.close if ctx else 0.0
            return Fill(oid, o.code, o.side, 0, price, reject_reason=reason)
        fill = broker.submit(o, ctx)
        if not fill.ok:
            self._mark_rejected(oid, fill.reject_reason or RejectReason.UNKNOWN)
            return fill
        # 应用成交到账户（单一路径：PaperAccount 统一算费/更新持仓）
        if self.account is not None:
            if o.side == Side.BUY:
                self.account.buy(o.code, fill.filled_qty, o.date,
                                 close=fill.price, reason=o.reason)
            else:
                self.account.sell(o.code, fill.filled_qty, o.date,
                                 close=fill.price, reason=o.reason)
        self._mark_filled(oid, fill)
        return fill


# ---------- CLI ----------
def _print_order(o: Order):
    print(f"  #{o.id} [{o.status.value:>16}] {o.side.value:>4} {o.code} "
          f"x{o.qty} @ {o.date}  {o.reason}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2 OMS 订单管理（人工审批闸门）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(cmd="list")
    sub.add_parser("pending").set_defaults(cmd="pending")

    p_app = sub.add_parser("approve")
    p_app.add_argument("id", type=int)
    p_app.add_argument("--by", default="human")
    p_app.add_argument("--auto", action="store_true", help="允许非人工审批（仅仿真）")

    p_rej = sub.add_parser("reject")
    p_rej.add_argument("id", type=int)
    p_rej.add_argument("--reason", default="人工驳回")
    p_rej.add_argument("--by", default="human")

    p_can = sub.add_parser("cancel")
    p_can.add_argument("id", type=int)

    p_st = sub.add_parser("status")
    p_st.add_argument("id", type=int)

    args = ap.parse_args()

    # 账户 / OMS 初始化（默认 demo 账户）
    acc = PaperAccount("demo") if PaperAccount else None
    oms = OMS(acc, allow_auto_approve=getattr(args, "auto", False))

    if args.cmd == "list":
        for o in oms.list():
            _print_order(o)
    elif args.cmd == "pending":
        ps = oms.pending()
        if not ps:
            print("（无待审批订单）")
        for o in ps:
            _print_order(o)
    elif args.cmd == "approve":
        ok = oms.approve(args.id, by=args.by)
        print(f"approve #{args.id} → {'OK' if ok else 'FAILED（状态不可审批或非人工）'}")
    elif args.cmd == "reject":
        ok = oms.reject(args.id, by=args.by, reason=args.reason)
        print(f"reject #{args.id} → {'OK' if ok else 'FAILED'}")
    elif args.cmd == "cancel":
        ok = oms.cancel(args.id)
        print(f"cancel #{args.id} → {'OK' if ok else 'FAILED'}")
    elif args.cmd == "status":
        try:
            o = oms.get(args.id)
            print(o.as_dict())
        except KeyError as e:
            print(e)


if __name__ == "__main__":
    main()
