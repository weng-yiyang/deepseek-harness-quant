# -*- coding: utf-8 -*-
"""live/post_close.py — 盘后编排（T 日收盘后运行）

链路：数据刷新 → 机会扫描 → Pitch → 生成次日(T+1)候选订单计划

产物：
  logs/next_day_plan_{T}.json  次日候选订单计划（as_of_date = 下一交易日）
  logs/phase4_state.json       编排状态（每次运行结果留痕，便于排查）

设计：
- **幂等**：同一交易日只生成一次计划（--force 覆盖），可安全被调度器重复调用。
- 各步骤用子进程调用既有 CLI（复用成熟逻辑，不重造），单步失败不阻塞后续，
  但会记入状态文件并在摘要里标出。
- 盘后**只生成候选，不下单**；真正的买入决策由人在 Deck 审批（human-in-the-loop），
  次日盘前由 live/pre_market.py 执行"人已批准"的订单。

用法：
  python live/post_close.py                  # 完整盘后链路
  python live/post_close.py --skip-refresh   # 跳过数据刷新（已手动跑过管道）
  python live/post_close.py --dry-run        # 只打印将执行的步骤
  python live/post_close.py --force          # 强制重生成当日计划
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from live.trade_calendar import next_trade_date  # noqa: E402

STATE_FILE = BASE / "logs" / "phase4_state.json"
PLAN_DIR = BASE / "logs"


def _latest_deck():
    """取最新的 pitch_deck_*.json（mtime 最新）"""
    fs = sorted(glob.glob(str(BASE / "logs" / "pitch_deck_*.json")), key=os.path.getmtime)
    if not fs:
        return None, None
    p = Path(fs[-1])
    try:
        return json.loads(p.read_text(encoding="utf-8")), p.name
    except Exception:
        return None, p.name


def _run_step(name: str, rel_script: str, args=None, timeout: int = 1800,
              dry_run: bool = False) -> dict:
    """子进程执行一步；dry_run 只返回计划不真跑"""
    script = BASE / rel_script
    if not script.exists():
        return {"step": name, "ok": False, "error": f"脚本不存在: {rel_script}"}
    if dry_run:
        return {"step": name, "ok": True, "dry_run": True, "cmd": f"{rel_script} {' '.join(args or [])}"}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE)
    try:
        p = subprocess.run([sys.executable, str(script), *(args or [])],
                           cwd=str(BASE), env=env, capture_output=True,
                           text=True, timeout=timeout)
        return {"step": name, "ok": p.returncode == 0, "returncode": p.returncode,
                "stdout_tail": (p.stdout or "")[-400:].strip(),
                "stderr_tail": (p.stderr or "")[-400:].strip()}
    except subprocess.TimeoutExpired:
        return {"step": name, "ok": False, "error": f"超时 {timeout}s"}
    except Exception as e:
        return {"step": name, "ok": False, "error": str(e)}


def _write_state(rec: dict):
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("post_close", []).append(rec)
    data["post_close"] = data["post_close"][-50:]
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def run(date: str = None, skip_refresh: bool = False, force: bool = False,
        dry_run: bool = False) -> dict:
    """执行盘后编排，返回摘要 dict。"""
    T = (date or datetime.now().strftime("%Y-%m-%d"))[:10]
    plan_path = PLAN_DIR / f"next_day_plan_{T}.json"

    summary = {"stage": "post_close", "trade_date": T, "steps": [],
               "plan_path": str(plan_path), "skipped_idempotent": False}

    # 幂等：同一交易日已有计划则跳过（--force 覆盖）
    if plan_path.exists() and not force:
        summary["skipped_idempotent"] = True
        print(f"ℹ 当日计划已存在（{plan_path.name}）→ 跳过；需重生成加 --force")
        return summary

    nxt = next_trade_date(T)
    summary["next_trade_date"] = nxt
    if not nxt.get("date"):
        print("⚠ 无法确定下一交易日（行情数据不足）→ 仍生成计划，但 as_of_date 置空，"
              "请盘前用 --date 显式指定")

    # ① 数据刷新
    if skip_refresh:
        summary["steps"].append({"step": "数据刷新", "ok": True, "skipped": True})
    else:
        summary["steps"].append(
            _run_step("数据刷新", "data/daily_pipeline.py", dry_run=dry_run))

    # ② 机会扫描
    summary["steps"].append(
        _run_step("机会扫描", "factors/opportunities/scan.py", ["--pitch"], dry_run=dry_run))

    # ③ Pitch 生成（不传 --force，复用 ② 的扫描结果）
    summary["steps"].append(
        _run_step("Pitch生成", "strategy/pitch_v2.py", dry_run=dry_run))

    # ④ 收集候选 → 写次日计划
    deck, deck_file = _latest_deck()
    candidates = []
    if deck:
        for it in (deck.get("deck") or []):
            candidates.append({
                "code": it.get("code"), "name": it.get("name", ""),
                "otype": it.get("otype", ""), "score": it.get("score"),
                "industry": it.get("industry", ""),
            })
    plan = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stage": "post_close",
        "trade_date": T,                       # 生成日（T）
        "as_of_date": nxt.get("date") or None,  # 计划执行日（T+1）
        "source_deck": deck_file,
        "n_candidates": len(candidates),
        "candidates": candidates,
        "note": "候选清单，非订单。请人工在 Deck 审批 → logs/deck_decisions.json；"
                "次日盘前由 live/pre_market.py 执行已审批条目。",
    }
    summary["n_candidates"] = len(candidates)
    summary["as_of_date"] = plan["as_of_date"]

    if dry_run:
        summary["steps"].append({"step": "写次日计划", "ok": True, "dry_run": True})
    else:
        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        summary["steps"].append({"step": "写次日计划", "ok": True})

    _write_state({k: v for k, v in summary.items() if k != "steps"} |
                 {"n_steps_ok": sum(1 for s in summary["steps"] if s.get("ok"))})
    return summary


def main():
    ap = argparse.ArgumentParser(description="Phase 4 盘后编排（数据刷新→扫描→Pitch→次日计划）")
    ap.add_argument("--date", help="交易日 T（默认今天）")
    ap.add_argument("--skip-refresh", action="store_true", help="跳过数据刷新")
    ap.add_argument("--force", action="store_true", help="强制重生成当日计划")
    ap.add_argument("--dry-run", action="store_true", help="只打印步骤，不执行")
    args = ap.parse_args()

    s = run(date=args.date, skip_refresh=args.skip_refresh,
            force=args.force, dry_run=args.dry_run)

    print(f"\n==== 盘后编排 @ {s['trade_date']} ====")
    for st in s["steps"]:
        flag = "✓" if st.get("ok") else "✗"
        extra = st.get("error") or st.get("cmd") or ("跳过" if st.get("skipped") else "")
        print(f"  {flag} {st.get('step')} {extra}")
    print(f"  候选 {s.get('n_candidates', 0)} 只 → 计划执行日 {s.get('as_of_date')}")
    if not args.dry_run:
        print(f"  计划文件：{s['plan_path']}")
        print("  → 请人工在 Deck 审批（写入 logs/deck_decisions.json），"
              "次日盘前运行：python live/pre_market.py")


if __name__ == "__main__":
    main()
