# -*- coding: utf-8 -*-
"""data/repair_phase1.py — Phase 1 数据根因修复编排器（让审计真正转 PASS）

把分散的修复脚本按正确依赖顺序串起来，最后用审计硬闸门判定；
   闸门 PASS（exit 0）即代表数据可信，可继续策略/回测；FAIL（exit 1）→ 仍存在根因未修。

执行顺序（依赖链）：
  0) gen_delisted_list   生成 delisted_list.csv（F-2 数据来源；原仓库缺失）
  1) backfill_delisted   补拉 2019 后退市股（F-2 幸存者偏差）          [需网络，--engine 选源]
  2) fix_st_flags        重拉 isST 标记（F-1 ST 失效 → C5 FAIL）        [需网络，--engine 选源]
  3) repair_consistency  清 B1/B3/B4/C3 一致性脏行（让硬闸门不误伤）    [本地，不需网络]
  4) recompute_bar_meta  重算 bar_meta.rows 累计口径（F-4）              [本地，不需网络]
  5) (可选) 财报 PIT      tushare 财报含 ann_date（F-5）               [需网络/tushare, --finance]
  6) 审计闸门            DataAuditor.gate() → PASS/FAIL                [判定]

数据源（--engine，默认 tushare）：
  tushare  : 用 data/backfill_delisted_tushare.py + data/fix_st_flags_tushare.py
             （推荐 — baostock 2024 起多次停服，tushare 主账户已确认可用）
  baostock : 用原 data/backfill_delisted.py + data/fix_st_flags.py（仅作降级兜底）

设计：
- 网络步骤失败不致命（打印警告继续），最终由审计闸门兜底判定；
- 本地步骤（3/4）不依赖任何外部库/网络，必定可执行；
- --skip-network：跳过 0/1/5，只跑本地修复 + 闸门（适合"数据已拉但需清洗/复算"场景）；
- --only-gate：只跑审计闸门（快速复检）。
- 全程只写数据/修复文件，不修改审计脚本。

用法：
  python data/repair_phase1.py                      # 全量修复（tushare 源）+ 闸门判定
  python data/repair_phase1.py --engine baostock   # 降级用 baostock 源
  python data/repair_phase1.py --skip-network      # 只做本地清洗/复算 + 闸门
  python data/repair_phase1.py --only-gate         # 只跑审计闸门
  python data/repair_phase1.py --finance           # 含财报 PIT（F-5）
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


def _step(title: str, script: str = None, args: list = None, optional: bool = False,
          allow_fail: bool = True):
    print(f"\n{'='*60}\n>>> {title}\n{'='*60}")
    if script is None:
        return True  # 纯说明步骤
    cmd = [PY, str(BASE / script)] + (args or [])
    print(f"$ {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"[OK] {title}")
        return True
    if optional:
        print(f"[跳过/警告] {title} 返回 {rc}（非致命，继续；最终由审计闸门判定）")
        return True
    if allow_fail:
        print(f"[警告] {title} 返回 {rc}，继续；若数据仍未修好，审计闸门会 FAIL")
        return False
    print(f"[失败] {title} 返回 {rc}，停止")
    sys.exit(rc)


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


def main():
    ap = argparse.ArgumentParser(description="Phase 1 数据根因修复编排器")
    ap.add_argument("--skip-network", action="store_true", help="跳过需网络的步骤(0/1/5)，只跑本地修复+闸门")
    ap.add_argument("--only-gate", action="store_true", help="只跑审计闸门")
    ap.add_argument("--finance", action="store_true", help="含财报 PIT 修复(F-5, 需 tushare)")
    ap.add_argument("--engine", default="tushare", choices=["tushare", "baostock"],
                   help="F-1/F-2 网络数据源（默认 tushare；baostock 仅降级兜底）")
    args = ap.parse_args()

    engine = args.engine
    # 引擎映射（tushare 默认，消除 baostock 停服风险）
    f2_script = "data/backfill_delisted_tushare.py" if engine == "tushare" else "data/backfill_delisted.py"
    f1_script = "data/fix_st_flags_tushare.py" if engine == "tushare" else "data/fix_st_flags.py"

    print(f"# Phase 1 数据根因修复编排  @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"# 工作区: {BASE}  python: {PY}  数据源: {engine}")

    if args.only_gate:
        ok, _ = _gate()
        sys.exit(0 if ok else 1)

    # 0) 生成退市清单（F-2 数据来源）
    if not args.skip_network:
        _step("0) 生成 delisted_list.csv（F-2 数据来源）",
              "data/gen_delisted_list.py", optional=True)

    # 1) 补拉退市股（F-2）
    if not args.skip_network:
        _step(f"1) 补拉 2019 后退市股（F-2 幸存者偏差 · {engine}）",
              f2_script, optional=True)

    # 2) 重拉 ST 标记（F-1）
    if not args.skip_network:
        _step(f"2) 重拉 isST 标记（F-1 → C5 · {engine}）",
              f1_script, optional=True)

    # 3) 本地一致性清洗（B1/B3/B4/C3）
    _step("3) 清洗一致性脏行（B1/B3/B4/C3）", "data/repair_consistency.py")

    # 4) 重算 bar_meta.rows（F-4）
    _step("4) 重算 bar_meta.rows 累计口径（F-4）", "data/recompute_bar_meta.py")

    # 5) 财报 PIT（F-5，可选；fetch_quality_tushare 已含 ann_date 披露日）
    if args.finance and not args.skip_network:
        _step("5) 财报 PIT 披露日（F-5，已含 ann_date）",
              "data/fetch_quality_tushare.py", optional=True)

    # 6) 审计闸门判定
    ok, _ = _gate()
    print("\n" + ("# ✅ 数据修复完成，审计放行，可继续策略/回测。" if ok
                 else "# ⛔ 审计仍 FAIL，请按上方 [FAIL] 项继续修复后重跑本编排。"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
