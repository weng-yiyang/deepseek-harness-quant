# -*- coding: utf-8 -*-
"""data/repair_phase1.py — Phase 1 数据根因修复编排器（让审计真正转 PASS）

把分散的修复脚本按正确依赖顺序串起来，最后用审计硬闸门判定；
   闸门 PASS（exit 0）即代表数据可信，可继续策略/回测；FAIL（exit 1）→ 仍存在根因未修。

执行顺序（依赖链）：
  0) gen_delisted_list   生成 delisted_list.csv（F-2 数据来源；原仓库缺失）
  1) backfill_delisted   补拉 2019 后退市股（F-2 幸存者偏差）          [需网络]
  2) fix_st_flags        重拉 isST 标记（F-1 ST 失效 → C5 FAIL）        [需网络]
  3) repair_consistency  清 B1/B3/B4/C3 一致性脏行（让硬闸门不误伤）    [本地，不需网络]
  4) recompute_bar_meta  重算 bar_meta.rows 累计口径（F-4）              [本地，不需网络]
  5) (可选) 财报 PIT      tushare 财报含 ann_date（F-5）               [需网络/token, --finance]
  6) 审计闸门            DataAuditor.gate() → PASS/FAIL                [判定]

数据源（--engine，默认 **baostock**）：
  baostock : 免费、免注册、无需 token；日线自带**逐日 isST** 字段，直接对应 daily_bar.is_st
  tushare  : 备份源，需 token（积分制）；ST 走 stock_st 区间接口，财报含 ann_date（PIT）

★自动故障转移（fail-over）：网络步骤在主源失败时会**自动切换到备份源重试**，
  无需人工干预（挂机跑半夜主源挂掉也能自愈）。用 --no-fallback 可关闭该行为。
  默认链路（零 token）：akshare 退市清单 → baostock 退市股 → baostock 逐日 ST，
  任一环失败自动退到 tushare（需 token）。

设计：
- 网络步骤失败不致命（打印警告继续），最终由审计闸门兜底判定；
- 本地步骤（3/4）不依赖任何外部库/网络/token，必定可执行；
- --skip-network：跳过 0/1/5，只跑本地修复 + 闸门（适合"数据已拉但需清洗/复算"场景）；
- --only-gate：只跑审计闸门（快速复检）。
- 全程只写数据/修复文件，不修改审计脚本。

用法：
  python data/repair_phase1.py                      # 默认 baostock（零 token）+ 闸门判定
  python data/repair_phase1.py --engine tushare     # 主源改用 tushare（需 token）
  python data/repair_phase1.py --no-fallback        # 只用主源，不做故障转移
  python data/repair_phase1.py --skip-network       # 只做本地清洗/复算 + 闸门
  python data/repair_phase1.py --only-gate          # 只跑审计闸门
  python data/repair_phase1.py --finance            # 含财报 PIT（F-5，需 tushare token）
"""
import argparse
import os
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
PY = sys.executable

# 引擎 → 脚本映射（F-1 ST 标记 / F-2 退市股补拉）
F1_SCRIPTS = {
    "baostock": "data/fix_st_flags.py",
    "tushare": "data/fix_st_flags_tushare.py",
}
F2_SCRIPTS = {
    "baostock": "data/backfill_delisted.py",
    "tushare": "data/backfill_delisted_tushare.py",
}
LIST_SCRIPT = "data/gen_delisted_list.py"
LOCAL_SCRIPTS = [
    ("3) 清洗一致性脏行（B1/B3/B4/C3）", "data/repair_consistency.py"),
    ("4) 重算 bar_meta.rows 累计口径（F-4）", "data/recompute_bar_meta.py"),
]


def _run(script: str, args: list = None) -> bool:
    """执行一个子脚本，返回是否成功（rc == 0）"""
    cmd = [PY, str(BASE / script)] + (args or [])
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode == 0


def _network_step(title: str, candidates: list) -> str:
    """按候选顺序尝试网络步骤；成功即止；全失败则警告继续（网络步骤非致命）。

    candidates: [(label, script, args), ...]  按优先级排列
    返回最终成功的 label，全失败返回 None。
    """
    for label, script, sargs in candidates:
        print(f"\n{'='*60}\n>>> {title} · 尝试 {label}\n{'='*60}")
        if _run(script, sargs):
            print(f"[OK] {title}（{label}）")
            return label
        print(f"[警告] {title} 在 {label} 下失败 → 尝试下一个源")
    print(f"[跳过/警告] {title} 所有候选源均失败（非致命，继续；最终由审计闸门判定）")
    return None


def _gate():
    """运行审计硬闸门，返回 (ok, result)"""
    from risk.data_audit import DataAuditor, _load_config
    print(f"\n{'='*60}\n>>> [判定] 数据审计硬闸门 DataAuditor.gate()\n{'='*60}")
    auditor = DataAuditor(_load_config())
    ok, r = auditor.gate()
    print(f"闸门结论: {'🟢 放行 PASS' if ok else '🔴 阻断 FAIL'}")
    if not ok:
        print(f"阻断原因: {r['block_reason']}")
        # 列出仍 FAIL 的检查项，便于定位
        for it in r["items"]:
            if it["status"] == "FAIL":
                print(f"  - [FAIL] {it['id']} {it['name']}: {it['detail']}")
    else:
        print(f"健康度 {r['health']}/100（PASS {r['n_pass']} / WARN {r['n_warn']} / FAIL {r['n_fail']}）")
    return ok, r


def run_repair(skip_network: bool = False, only_gate: bool = False,
               finance: bool = False, engine: str = "baostock",
               no_fallback: bool = False) -> dict:
    """执行修复编排，返回汇总 dict（抽成函数便于测试与复用）。"""
    primary = engine
    backup = "tushare" if primary == "baostock" else "baostock"
    engines = [primary] if no_fallback else [primary, backup]

    summary = {
        "engine_primary": primary,
        "engine_backup": None if no_fallback else backup,
        "steps": {},
    }

    print(f"# Phase 1 数据根因修复编排  @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"# 工作区: {BASE}  python: {PY}")
    print(f"# 主源: {primary}" + (f"   备份源: {backup}（主源失败自动切换）"
                                  if not no_fallback else "   （已关闭故障转移）"))

    if only_gate:
        ok, _ = _gate()
        summary["gate_ok"] = ok
        return summary

    # 0) 生成退市清单（F-2 数据来源）
    #    baostock 主源时优先 akshare（免费免 token），tushare 作备
    if not skip_network:
        list_pri = "akshare" if primary == "baostock" else "tushare"
        list_bak = "tushare" if list_pri == "akshare" else "akshare"
        cands = [(f"退市清单/{list_pri}", LIST_SCRIPT, ["--source", list_pri])]
        if not no_fallback:
            cands.append((f"退市清单/{list_bak}", LIST_SCRIPT, ["--source", list_bak]))
        summary["steps"]["0_list"] = _network_step(
            "0) 生成 delisted_list.csv（F-2 数据来源）", cands)

    # 1) 补拉退市股（F-2）
    if not skip_network:
        summary["steps"]["1_backfill"] = _network_step(
            "1) 补拉 2019 后退市股（F-2 幸存者偏差）",
            [(f"F-2/{e}", F2_SCRIPTS[e], None) for e in engines])

    # 2) 重拉 ST 标记（F-1）
    if not skip_network:
        summary["steps"]["2_st"] = _network_step(
            "2) 重拉 isST 标记（F-1 → C5）",
            [(f"F-1/{e}", F1_SCRIPTS[e], None) for e in engines])

    # 3) 4) 本地步骤（不需网络/token，必定可执行）
    for title, script in LOCAL_SCRIPTS:
        print(f"\n{'='*60}\n>>> {title}\n{'='*60}")
        ok = _run(script)
        summary["steps"][script] = ok
        print(f"[OK] {title}" if ok else f"[警告] {title} 返回非零（继续，最终由闸门判定）")

    # 5) 财报 PIT（F-5，可选；需 tushare token —— 没有 token 时失败不致命）
    if finance and not skip_network:
        summary["steps"]["5_finance"] = _network_step(
            "5) 财报 PIT 披露日（F-5，已含 ann_date）",
            [("F-5/tushare", "data/fetch_quality_tushare.py", None)])

    # 6) 审计闸门判定
    ok, _ = _gate()
    summary["gate_ok"] = ok
    print("\n" + ("# ✅ 数据修复完成，审计放行，可继续策略/回测。" if ok
                 else "# ⛔ 审计仍 FAIL，请按上方 [FAIL] 项继续修复后重跑本编排。"))
    return summary


def build_parser():
    """构造命令行解析器（独立成函数，便于测试默认参数）"""
    ap = argparse.ArgumentParser(description="Phase 1 数据根因修复编排器")
    ap.add_argument("--skip-network", action="store_true", help="跳过需网络的步骤(0/1/5)，只跑本地修复+闸门")
    ap.add_argument("--only-gate", action="store_true", help="只跑审计闸门")
    ap.add_argument("--finance", action="store_true", help="含财报 PIT 修复(F-5, 需 tushare token)")
    ap.add_argument("--engine", default="baostock", choices=["baostock", "tushare"],
                   help="F-1/F-2 网络数据源主源（默认 baostock：免费免 token）")
    ap.add_argument("--no-fallback", action="store_true",
                   help="关闭自动故障转移（只用主源，失败不切备份源）")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    s = run_repair(skip_network=args.skip_network, only_gate=args.only_gate,
                   finance=args.finance, engine=args.engine,
                   no_fallback=args.no_fallback)
    sys.exit(0 if s.get("gate_ok") else 1)


if __name__ == "__main__":
    main()
