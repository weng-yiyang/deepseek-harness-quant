# -*- coding: utf-8 -*-
"""execution/execution_loop.py — 执行闭环（Phase 2 串联）

全链路：
  读订单计划(手动JSON / deck_decisions.json) → 建单(DRAFT) → 人工审批闸门
  → 盘前过滤 → 送仿真券商撮合 → 应用成交到 PaperAccount → 盘后 mark_to_market
  → 全流程接 Phase 1 数据闸门（脏数据 STOP.md / 审计 FAIL 一律不下单）。

设计：
- human-in-the-loop：默认订单建好后停在 PENDING_APPROVAL，需显式 approve 才成交；
  --auto-approve 仅用于仿真自测（明确标注非真实资金）。
- 不接任何真实券商、不碰真实资金（Phase 6 才接）。
- 仿真时段也要求数据可信：开头跑 DataAuditor 闸门，FAIL 直接中止。

用法：
  python execution/execution_loop.py --plan plan.json [--auto-approve] [--date 2024-08-06] [--account demo]
  python execution/execution_loop.py --from-deck [--auto-approve]   # 读 logs/deck_decisions.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

# 直接以脚本方式运行（python execution/execution_loop.py）时，把仓库根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.models import Order, OrderStatus, Side, MarketContext
from execution.oms import OMS
from execution.brokers.sim import SimBroker
from execution.positions import PositionBook
from execution.risk_gate import RiskGate

from strategy.paper_account import PaperAccount
try:
    from risk.data_audit import DataAuditor, _load_config, AuditBlocked
except Exception:
    DataAuditor = None
    _load_config = lambda *a, **k: {}
    AuditBlocked = Exception

BASE = Path(__file__).resolve().parent.parent


# ---------- 数据闸门 ----------
def data_is_ok() -> bool:
    """Phase 1 闸门：STOP.md 存在或审计 FAIL → 脏数据，不下单。fail-closed。"""
    try:
        if DataAuditor is not None and DataAuditor.is_stop_active():
            return False
        try:
            DataAuditor(_load_config()).require_clean_data(quick=True, context="执行下单")
            return True
        except Exception:
            return False
    except Exception:
        return False


# ---------- 行情上下文 ----------
def _bars_db_path() -> Path:
    cd = os.environ.get("LWQUANT_CACHE_DIR")
    base = Path(cd) if cd else (BASE / "data" / "cache")
    return base / "bars.db"


def build_ctx_from_db(code: str, date: str) -> Optional[MarketContext]:
    """从 daily_bar 构造当日 MarketContext（无行→delisted/无行情）。"""
    db = _bars_db_path()
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    row = None
    for adj in ("none", "qfq"):
        row = con.execute(
            "SELECT open,high,low,close,preclose,is_st FROM daily_bar "
            "WHERE code=? AND date=? AND adjust=? ORDER BY id DESC LIMIT 1",
            (code, date, adj)).fetchone()
        if row:
            break
    con.close()
    if not row:
        # ★P2-1：无该日行情 ≠ 退市（可能是数据缺失 / 停牌 / 未上市）。
        # 返回 close=0 → preflight 判 NO_MARKET_DATA（语义正确，提示去查数据）；
        # delisted 应由退市名单判定，不在此处臆断，避免日志把"数据没拉到"写成"退市"。
        return MarketContext(date=date, code=code, close=0.0)
    is_st = bool(row[5]) if row[5] is not None else False
    return MarketContext.from_bar(code, date, {
        "open": row[0], "high": row[1], "low": row[2], "close": row[3],
        "preclose": row[4], "adjust": "qfq"}, is_st=is_st)


# ---------- 计划解析 ----------
def load_plan_from_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_plan_from_deck(deck_path: str = None) -> dict:
    """读 logs/deck_decisions.json（人工审批产物）→ 订单计划。"""
    p = Path(deck_path) if deck_path else (BASE / "logs" / "deck_decisions.json")
    if not p.exists():
        raise FileNotFoundError(f"未找到人工审批文件: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    orders = []
    items = data if isinstance(data, list) else data.get("decisions", data.get("orders", []))
    for it in items:
        action = str(it.get("action", "")).lower()
        if action not in ("buy", "买入", "BUY"):
            continue
        code = it.get("code")
        if not code:
            continue
        qty = it.get("qty") or it.get("weight")
        if not qty or int(qty) <= 0:
            # 决策未给数量：跳过并提示（Phase 4 编排阶段再补推导）
            print(f"  [跳过] {code}: deck 决策未给 qty，本阶段不自动推导")
            continue
        orders.append({"code": code, "side": "BUY", "qty": int(qty),
                       "reason": it.get("reason") or it.get("note") or "deck审批买入"})
    return {"account": "demo", "as_of_date": None, "orders": orders}


# ---------- 主流程 ----------
def run_plan(plan: dict, *, account: PaperAccount, ctx_provider: Callable[[str, str], Optional[MarketContext]] = None,
             auto_approve: bool = False, data_ok: bool = True, st_filter: bool = True,
             oms_db=None, risk_config: Optional[dict] = None, enable_risk: bool = True) -> dict:
    """执行一份订单计划，返回汇总 dict。"""
    as_of = plan.get("as_of_date")
    orders_in = plan.get("orders", [])
    summary = {"created": 0, "approved": 0, "filled": 0, "rejected": 0,
               "pending_human": 0, "reduced": 0, "fills": [], "blocked": False}

    if not data_ok:
        print("⛔ 数据审计未通过 / STOP.md 存在 → 拒绝执行（脏数据不下单）")
        summary["blocked"] = True
        return summary

    # ★Phase 3 风控闸门（RiskAgent 经 RiskGate 适配：股数↔净值占比）
    gate = RiskGate(account, risk_config) if enable_risk else None
    if gate is not None:
        # 预置当日行情（持仓 + 本次计划标的），供风控估算净值/组合权重
        codes = {p["code"] for p in account.positions()} | {o["code"] for o in orders_in}
        prices = {}
        for c in codes:
            cx = ctx_provider(c, as_of or "") if ctx_provider else build_ctx_from_db(c, as_of or "")
            if cx and cx.close > 0:
                prices[c] = cx.close
        gate.prices = prices
        summary["risk"] = {"equity": round(gate.equity(prices), 2),
                           "drawdown": round(gate.current_drawdown(), 4)}

    oms = OMS(account, db_path=oms_db, allow_auto_approve=auto_approve,
              st_filter=st_filter, data_ok=data_ok, risk_gate=gate)
    pb = PositionBook(account)
    # 现金/持仓回调（供 SimBroker 做部分成交判定）
    broker = SimBroker(
        cash_provider=lambda: account.cash,
        position_provider=lambda c: next((p["qty"] for p in account.positions()
                                          if p["code"] == c), 0))

    created = []
    for o in orders_in:
        side = o.get("side", "BUY")
        order = oms.create_order(
            code=o["code"], side=side, qty=int(o["qty"]),
            date=as_of or o.get("date", datetime.now().strftime("%Y-%m-%d")),
            reason=o.get("reason", ""), limit_price=o.get("limit_price"))
        created.append(order)
        summary["created"] += 1

    if auto_approve:
        for order in created:
            oms.approve(order.id, by="auto-sim")
            summary["approved"] += 1
    else:
        # human-in-the-loop：停在待审批，打印清单，交由人工 approve
        pend = oms.pending()
        print(f"\n📋 待人工审批订单（{len(pend)}）：")
        for o in pend:
            print(f"   #{o.id} {o.side.value} {o.code} x{o.qty} @ {o.date}  {o.reason}")
        print("→ 用 `python execution/oms.py approve <id>` 逐笔审批，或执行层统一审批后重跑。")
        summary["pending_human"] = len(pend)
        return summary

    # 已审批 → 送券商撮合 → 应用成交
    for order in created:
        o = oms.get(order.id)
        if o.status != OrderStatus.APPROVED:
            continue
        ctx = ctx_provider(o.code, o.date) if ctx_provider else build_ctx_from_db(o.code, o.date)
        fill = oms.submit(o.id, broker, ctx)
        if fill.ok:
            summary["filled"] += 1
            rec = {"id": o.id, "code": o.code, "side": o.side.value,
                   "qty": fill.filled_qty, "price": fill.price}
            if fill.filled_qty < o.qty:      # 被风控缩量或券商部分成交
                summary["reduced"] += 1
                rec["reduced_from"] = o.qty
            summary["fills"].append(rec)
        else:
            summary["rejected"] += 1
            summary["fills"].append({"id": o.id, "code": o.code, "side": o.side.value,
                                     "reject": fill.reject_reason.value if fill.reject_reason else "UNKNOWN"})

    # 盘后净值（用当日收盘价）
    if as_of and summary["fills"]:
        prices = {}
        for f in summary["fills"]:
            if ctx_provider:
                c = ctx_provider(f["code"], as_of)
                if c: prices[f["code"]] = c.close
        if prices:
            nav = pb.mark_to_market(prices, as_of)
            summary["nav"] = round(nav, 2)
    return summary


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Phase 2 执行闭环（仿真 + human-in-the-loop）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--plan", help="手动订单计划 JSON")
    src.add_argument("--from-deck", action="store_true", help="读 logs/deck_decisions.json")
    ap.add_argument("--auto-approve", action="store_true", help="仿真自测：自动审批（非真实资金）")
    ap.add_argument("--date", help="交易日 as_of_date（覆盖计划）")
    ap.add_argument("--account", default="demo")
    ap.add_argument("--no-st-filter", action="store_true", help="关闭 ST 名称盘前过滤")
    ap.add_argument("--no-risk", action="store_true",
                    help="关闭风控闸门（★仅调试用，实盘禁止）")
    ap.add_argument("--deck-path", help="自定义 deck_decisions.json 路径")
    args = ap.parse_args()

    ok = data_is_ok()
    if not ok:
        print("⛔ 数据审计未通过 / STOP.md 存在 → 拒绝执行（脏数据不下单）")
        return

    account = PaperAccount(args.account)
    if args.from_deck:
        plan = load_plan_from_deck(args.deck_path)
    else:
        plan = load_plan_from_json(args.plan)
    if args.date:
        plan["as_of_date"] = args.date

    if args.no_risk:
        print("⚠ --no-risk：已关闭风控闸门（仅调试用，实盘禁止）")

    summary = run_plan(plan, account=account, auto_approve=args.auto_approve,
                       data_ok=True, st_filter=not args.no_st_filter,
                       enable_risk=not args.no_risk)
    print("\n==== 执行汇总 ====")
    print(json.dumps({k: v for k, v in summary.items() if k != "fills"},
                     ensure_ascii=False, indent=1))
    if summary.get("fills"):
        print("成交/拒单明细:")
        for f in summary["fills"]:
            print("  ", f)


if __name__ == "__main__":
    main()
