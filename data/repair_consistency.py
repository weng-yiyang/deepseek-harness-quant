# -*- coding: utf-8 -*-
"""data/repair_consistency.py — 数据修复（一致性）Phase 1 兜底清洗

清除会让审计硬闸门 FAIL 的脏行（这些行来自拉取/合并过程的脏数据，并非真实行情）：
  B1 重复行 (code,date,adjust) 重复      → 保留 rowid 最小的一条
  B3 周末日期                            → 删除（行情不含周末）
  B4 未来日期（晚于今天）                → 删除（拉取越界/时钟异常）
  C3 非正价格 / 负量额（open/high/low/close<=0，volume/amount/turn<0）→ 删除

设计：
- ★只写 daily_bar（删除行），不动其他表；与审计"只读"职责分离，本脚本是"修复"职责。
- 先统计各类脏行数量，再执行删除；支持 --dry-run 只统计不删。
- 失败关闭：单步异常即抛出，绝不静默吞掉（避免"以为清干净了其实没清"）。
- 复用 risk/data_audit 的缓存目录解析（LWQUANT_CACHE_DIR / params.data.cache_dir / data/cache）。

用法：
  python data/repair_consistency.py --dry-run     # 统计各类脏行
  python data/repair_consistency.py               # 执行清洗
"""
import argparse
import os
import sqlite3
import sys
import warnings
from datetime import datetime, date
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _resolve_cache_dir() -> Path:
    env = os.environ.get("LWQUANT_CACHE_DIR")
    if env:
        return Path(env)
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
        d = (cfg or {}).get("data", {}).get("cache_dir")
        if d:
            p = Path(str(d))
            return p if p.is_absolute() else BASE / p
    except Exception:
        pass
    return BASE / "data" / "cache"


# 各脏行类型的 (检查项, 统计SQL, 删除SQL)
_RULES = [
    ("B1", "重复行 (code,date,adjust)",
     "SELECT COUNT(*) FROM daily_bar WHERE rowid NOT IN "
     "(SELECT MIN(rowid) FROM daily_bar GROUP BY code,date,adjust)",
     "DELETE FROM daily_bar WHERE rowid NOT IN "
     "(SELECT MIN(rowid) FROM daily_bar GROUP BY code,date,adjust)"),
    ("B3", "周末日期",
     "SELECT COUNT(*) FROM daily_bar WHERE strftime('%w', date) IN ('0','6')",
     "DELETE FROM daily_bar WHERE strftime('%w', date) IN ('0','6')"),
    ("B4", "未来日期(>今天)",
     "SELECT COUNT(*) FROM daily_bar WHERE date > ?",
     "DELETE FROM daily_bar WHERE date > ?"),
    ("C3", "非正价格/负量额",
     "SELECT COUNT(*) FROM daily_bar WHERE open<=0 OR high<=0 OR low<=0 OR close<=0 "
     "OR volume<0 OR amount<0 OR turn<0",
     "DELETE FROM daily_bar WHERE open<=0 OR high<=0 OR low<=0 OR close<=0 "
     "OR volume<0 OR amount<0 OR turn<0"),
]


def repair(db_path: Path, dry_run: bool = False) -> dict:
    """清洗 daily_bar 一致性脏行。返回每类 {检查项: 删除行数}。

    注意：B4 的删除 SQL 需要参数（今天），由调用方传入 today 作为 args。
    """
    today = date.today().isoformat()
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA busy_timeout=15000")
    cur = con.cursor()
    # 确认表存在
    has = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_bar'").fetchone()
    if not has:
        con.close()
        raise RuntimeError(f"{db_path} 无 daily_bar 表 —— 请先跑完基础数据拉取")
    result = {}
    total_rows_before = cur.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    for cid, desc, cnt_sql, del_sql in _RULES:
        # B4 需要参数
        args = (today,) if cid == "B4" else ()
        n = cur.execute(cnt_sql, args).fetchone()[0]
        result[cid] = {"desc": desc, "dirty": n, "deleted": 0}
        if dry_run:
            print(f"  [{cid}] {desc}: {n} 行（dry-run 不删除）")
        else:
            if n:
                cur.execute(del_sql, args)
                result[cid]["deleted"] = cur.rowcount
                print(f"  [{cid}] {desc}: 删除 {cur.rowcount} 行")
            else:
                print(f"  [{cid}] {desc}: 0 行，无需处理")
    if not dry_run:
        con.commit()
    total_rows_after = cur.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    con.close()
    result["_summary"] = {
        "before": total_rows_before, "after": total_rows_after,
        "removed": total_rows_before - total_rows_after, "dry_run": dry_run,
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="清洗 daily_bar 一致性脏行（B1/B3/B4/C3）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不删除")
    ap.add_argument("--db", default=None, help="bars.db 路径（默认走缓存目录解析）")
    args = ap.parse_args()
    db = Path(args.db) if args.db else _resolve_cache_dir() / "bars.db"
    if not db.exists():
        print(f"[错误] 找不到 {db} —— 请先跑完基础数据拉取")
        sys.exit(2)
    print(f"=== repair_consistency（{'dry-run' if args.dry_run else '执行'}）===")
    print(f"目标: {db}")
    r = repair(db, dry_run=args.dry_run)
    s = r["_summary"]
    print(f"行数: {s['before']:,} → {s['after']:,}（移除 {s['removed']:,}）")
    print("建议随后运行 data/recompute_bar_meta.py 同步 bar_meta.rows，再跑审计闸门验证。")


if __name__ == "__main__":
    main()
