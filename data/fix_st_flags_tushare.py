# -*- coding: utf-8 -*-
"""data/fix_st_flags_tushare.py — 数据修复 F-1（Tushare 版，消除 baostock 停服风险）

背景：daily_bar.is_st 全 0（baostock isST '0'/'1' 映射 bug）→ filter_st 形同虚设 → 审计 C5 FAIL。
原 baostock 版（fix_st_flags.py）依赖 baostock，2024 起社区多次报告停服 → 本地跑不通。
本版改用 Tushare stock_st 接口（需 15000 积分主账户，用户已确认可用）：

方法（一次性全市场 ST 区间，比逐股查询快一个数量级）：
  1) get_stock_st_intervals() 分年度拉取 stock_st，得 {ts_code: [(start,end), ...]}（仅 stock_st==1 区间）
  2) 全局重置 daily_bar.is_st=0（清掉旧 baostock 误标）
  3) 对每个 ST 区间 UPDATE daily_bar SET is_st=1 WHERE code=? AND date BETWEEN s AND e
  4) 双库兼容：bars.db + bars_incr*.db 都修正（环境写保护下数据可能落在增量库）

特性：
- 指数/北交所代码自动跳过（stock_st 不含；daily_bar 也只存股票）
- 幂等：重跑结果一致（先 reset 后 set）
- 不依赖 baostock；需 params.yaml 的 tushare_token（或环境变量 TUSHARE_TOKEN）
- 全程只写 daily_bar.is_st 一列

用法：
  python data/fix_st_flags_tushare.py --dry-run     # 统计将标记多少 ST 行（只读）
  python data/fix_st_flags_tushare.py               # 全量修复
  python data/fix_st_flags_tushare.py --status      # 查看当前 ST 覆盖率
"""
import argparse
import glob
import os
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import get_stock_st_intervals  # 复用：分年度 stock_st → ST 区间

ST_START_YEAR = 2010  # ST 区间起始年（早于数据起点 2019，确保跨年 ST 也能覆盖）


def _db_paths() -> list:
    """双库路径：bars.db + 所有 bars_incr*.db（环境写保护下数据可能落在增量库）"""
    cache = __import__("data.cache", fromlist=["DailyCache"]).DailyCache()
    parent = Path(cache.db_path).parent
    paths = []
    main = parent / "bars.db"
    if main.exists():
        paths.append(main)
    for f in glob.glob(str(parent / "bars_incr*.db")):
        paths.append(Path(f))
    # 去重（保持顺序）
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _apply_intervals(intervals: dict, dry_run: bool = False):
    """把 ST 区间应用到所有库；dry_run 仅统计将标记行数不写库。
    ★每个库保持单连接，结束时统一 commit（避免逐行 close 未提交导致回滚）"""
    db_paths = _db_paths()
    total_set = 0
    for db in db_paths:
        con = sqlite3.connect(str(db), timeout=30)
        try:
            for code, ivs in intervals.items():
                for s, e in ivs:
                    if dry_run:
                        n = con.execute(
                            "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                            (code, s, e)).fetchone()[0]
                        total_set += n
                    else:
                        cur = con.execute(
                            "UPDATE daily_bar SET is_st=1 WHERE code=? AND date>=? AND date<=?",
                            (code, s, e))
                        total_set += cur.rowcount
            if not dry_run:
                con.commit()
        finally:
            con.close()
    return total_set


def _reset_all(dry_run: bool = False):
    """全局重置 is_st=0（清旧误标）；dry_run 跳过"""
    if dry_run:
        return
    for db in _db_paths():
        con = sqlite3.connect(str(db), timeout=30)
        try:
            con.execute("UPDATE daily_bar SET is_st=0 WHERE is_st!=0")
            con.commit()
        finally:
            con.close()


def run(dry_run: bool = False):
    print(f"=== F-1 Tushare 版：拉取全市场 ST 区间（{ST_START_YEAR} 起）===")
    intervals = get_stock_st_intervals(start_year=ST_START_YEAR)
    print(f"  ST 区间覆盖 {len(intervals)} 只股票 / {sum(len(v) for v in intervals.values())} 段")
    if not intervals:
        print("  [警告] 未取到任何 ST 区间（token/网络/接口权限问题）→ 请检查 tushare_token 与 stock_st 接口积分")
        return

    if dry_run:
        _reset_all(dry_run=True)
        n = _apply_intervals(intervals, dry_run=True)
        print(f"  [dry-run] 将标记 is_st=1 约 {n} 行（覆盖 {len(intervals)} 只）。确认后去掉 --dry-run 全量跑")
        return

    print("  重置 daily_bar.is_st=0（清旧误标）...")
    _reset_all(dry_run=False)
    n = _apply_intervals(intervals, dry_run=False)
    print(f"  完成：标记 is_st=1 约 {n} 行（{len(intervals)} 只）。下一步跑 repair_consistency + 审计闸门验证 C5")


def status():
    db_paths = _db_paths()
    tot = st = 0
    for db in db_paths:
        con = sqlite3.connect(str(db), timeout=10)
        try:
            tot += con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] or 0
            st += con.execute("SELECT COUNT(*) FROM daily_bar WHERE is_st!=0").fetchone()[0] or 0
        finally:
            con.close()
    ratio = (st / tot * 100) if tot else 0.0
    print(f"ST 标记: is_st≠0 {st:,} 行 / 共 {tot:,} 行 → 占比 {ratio:.4f}%"
          f"（审计 C5 阈值 1.00%，{'PASS' if ratio >= 1.0 else 'FAIL(<1%)'}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="重拉 isST 标记修复 F-1（Tushare 版）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(dry_run=args.dry_run)
