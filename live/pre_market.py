# -*- coding: utf-8 -*-
"""live/pre_market.py — 盘前编排（T+1 开盘前运行）

链路：数据闸门 → 交易日校验 → 读人工审批(deck_decisions.json) → 与次日计划交叉校验
      → 盘前过滤(ST/停牌/退市/涨停/T+1) → 风控 → 执行

★human-in-the-loop 的位置：机器**只执行人已在 Deck 审批的标的**，不代替人做买入决策。
  - 未经审批 → 不下单（打印待审批提示后退出）
  - 审批了但不在盘后候选清单内 → 跳过并警告（防止审批了非候选标的）
  - 数据闸门未过（STOP.md / 审计 FAIL）→ 中止（脏数据不下单）

用法：
  python live/pre_market.py                  # 执行已审批订单
  python live/pre_market.py --dry-run        # 只列出将执行的订单，不下单
  python live/pre_market.py --date 2026-08-31
  python live/pre_market.py --no-risk        # 关闭风控（★仅调试，实盘禁止）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from live.trade_calendar import is_trade_date, previous_trade_date  # noqa: E402
from execution import execution_loop as el  # noqa: E402
from execution.models import MarketContext, OrderStatus  # noqa: E402
from strategy.paper_account import PaperAccount  # noqa: E402

STATE_FILE = BASE / "logs" / "phase4_state.json"
DECK = BASE / "logs" / "deck_decisions.json"


def _write_state(rec: dict):
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("pre_market", []).append(rec)
    data["pre_market"] = data["pre_market"][-50:]
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _candidate_codes(as_of: str) -> tuple:
    """取盘后生成的次日计划候选代码集合；返回 (set|None, 计划文件名)"""
    prev = previous_trade_date(as_of)
    pdate = prev.get("date") or ""
    if pdate:
        p = BASE / "logs" / f"next_day_plan_{pdate}.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return {c.get("code") for c in (d.get("candidates") or []) if c.get("code")}, p.name
            except Exception:
                return None, p.name
    # 兜底：扫最近生成的计划文件
    fs = sorted((BASE / "logs").glob("next_day_plan_*.json"), key=lambda x: x.stat().st_mtime)
    if fs:
        try:
            d = json.loads(fs[-1].read_text(encoding="utf-8"))
            return {c.get("code") for c in (d.get("candidates") or []) if c.get("code")}, fs[-1].name
        except Exception:
            return None, fs[-1].name
    return None, None


def run(date: str = None, dry_run: bool = False, no_risk: bool = False,
        account_name: str = "demo", ctx_provider=None, oms_db=None) -> dict:
    """执行盘前编排，返回摘要 dict。"""
    as_of = (date or datetime.now().strftime("%Y-%m-%d"))[:10]
    summary = {"stage": "pre_market", "as_of_date": as_of, "blocked": False,
               "block_reason": None, "n_approved": 0, "n_executed": 0, "skipped": []}

    # ① 数据闸门（脏数据不下单）
    if not el.data_is_ok():
        summary["blocked"] = True
        summary["block_reason"] = "数据审计未通过 / STOP.md 存在"
        print("⛔ 数据审计未通过 / STOP.md 存在 → 盘前中止（脏数据不下单）")
        _write_state(summary)
        return summary

    # ② 交易日校验
    if not is_trade_date(as_of):
        summary["blocked"] = True
        summary["block_reason"] = f"{as_of} 非交易日"
        print(f"⛔ {as_of} 非交易日 → 盘前中止（周末/节假日不执行）")
        _write_state(summary)
        return summary

    # ③ 读人工审批（human-in-the-loop：没有审批就不下单）
    if not DECK.exists():
        summary["blocked"] = True
        summary["block_reason"] = "无人工审批文件"
        print(f"⛔ 未找到人工审批文件 {DECK} → 盘前中止（未审批不下单）")
        _write_state(summary)
        return summary
    try:
        plan = el.load_plan_from_deck(str(DECK))
    except Exception as e:
        summary["blocked"] = True
        summary["block_reason"] = f"审批文件解析失败: {e}"
        print(f"⛔ 审批文件解析失败 → 盘前中止：{e}")
        _write_state(summary)
        return summary

    orders = plan.get("orders") or []
    if not orders:
        print("ℹ 审批文件中没有待买入条目（可能全部放弃）→ 无需执行")
        _write_state(summary)
        return summary
    summary["n_approved"] = len(orders)

    # ④ 与盘后候选清单交叉校验（防审批了非候选标的）
    codes, plan_file = _candidate_codes(as_of)
    summary["cross_check_plan"] = plan_file
    if codes is None:
        print("⚠ 未找到盘后候选计划 → 跳过交叉校验，信任审批清单（建议先跑 live/post_close.py）")
    else:
        kept, dropped = [], []
        for o in orders:
            (kept if o["code"] in codes else dropped).append(o)
        if dropped:
            for o in dropped:
                summary["skipped"].append({"code": o["code"], "reason": "不在盘后候选清单内"})
                print(f"  [跳过] {o['code']} 不在盘后候选清单（{plan_file}）内")
        orders = kept
        if not orders:
            print("ℹ 所有审批条目均不在候选清单内 → 无需执行")
            _write_state(summary)
            return summary

    plan["as_of_date"] = as_of
    plan["orders"] = orders

    # ⑤ 执行（dry-run 只列单）
    if dry_run:
        print(f"\n[dry-run] 将执行 {len(orders)} 笔（as_of={as_of}）：")
        for o in orders:
            print(f"   BUY {o['code']} x{o['qty']}  {o.get('reason','')}")
        summary["dry_run"] = True
        _write_state(summary)
        return summary

    account = PaperAccount(account_name)
    # auto_approve=True：这些订单**人已在 Deck 审批**；approve_by 留痕为 deck-approved
    res = el.run_plan(plan, account=account, ctx_provider=ctx_provider,
                      auto_approve=True, data_ok=True,
                      enable_risk=not no_risk, approve_by="deck-approved",
                      oms_db=oms_db)
    summary["n_executed"] = res.get("filled", 0)
    summary["n_rejected"] = res.get("rejected", 0)
    summary["n_reduced"] = res.get("reduced", 0)
    summary["fills"] = res.get("fills", [])
    summary["risk"] = res.get("risk")
    _write_state({k: v for k, v in summary.items() if k != "fills"})
    return summary


def main():
    ap = argparse.ArgumentParser(description="Phase 4 盘前编排（执行已审批订单）")
    ap.add_argument("--date", help="执行日 as_of_date（默认今天）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将执行的订单，不下单")
    ap.add_argument("--no-risk", action="store_true", help="关闭风控（★仅调试，实盘禁止）")
    ap.add_argument("--account", default="demo")
    args = ap.parse_args()

    if args.no_risk:
        print("⚠ --no-risk：已关闭风控闸门（仅调试用，实盘禁止）")

    s = run(date=args.date, dry_run=args.dry_run, no_risk=args.no_risk,
            account_name=args.account)

    print(f"\n==== 盘前编排 @ {s['as_of_date']} ====")
    if s.get("blocked"):
        print(f"  ⛔ 中止：{s['block_reason']}")
        return
    print(f"  审批条目 {s.get('n_approved', 0)} 笔")
    if s.get("dry_run"):
        return
    print(f"  成交 {s.get('n_executed', 0)} / 拒单 {s.get('n_rejected', 0)} / 缩量 {s.get('n_reduced', 0)}")
    for f in s.get("fills", []):
        print("   ", f)


if __name__ == "__main__":
    main()
