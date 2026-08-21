# -*- coding: utf-8 -*-
"""data/recompute_bar_meta.py — 数据修复 F-4：重算 bar_meta.rows（累计覆盖口径）

问题（2026-08-07 审计 A2）：旧 put_daily 把 bar_meta.rows 写成"最后写入段行数"而非
      累计覆盖行数 → 16 只漂移 → covers()/覆盖率判断不可信（A2 WARN，且影响回测覆盖统计）。

修复：以 daily_bar 为唯一真相源，按 (code, adjust) 重算：
      rows        = COUNT(*)                 （累计覆盖行数， unambiguous）
      start_date  = MIN(date) / end_date = MAX(date)
      updated_at  = 当前时间
      缺失的 (code,adjust) 则 INSERT，已存在的 UPDATE。

设计：
- ★只读 daily_bar、只写 bar_meta，不碰行情本体。
- 一次性聚合（GROUP BY），不在 828 万行上逐行循环（对齐审计的 SQL 聚合原则）。
- 失败关闭：异常直接抛出。
- 复用 risk/data_audit 的缓存目录解析。

用法：
  python data/recompute_bar_meta.py --dry-run
  python data/recompute_bar_meta.py
"""
import argparse
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


def recompute(db_path: Path, dry_run: bool = False) -> dict:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA busy_timeout=15000")
    cur = con.cursor()
    for t in ("daily_bar", "bar_meta"):
        if not cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            con.close()
            raise RuntimeError(f"{db_path} 缺少表 {t}")
    # 以 daily_bar 为真相源聚合
    rows = cur.execute(
        "SELECT code, adjust, COUNT(*), MIN(date), MAX(date) "
        "FROM daily_bar GROUP BY code, adjust").fetchall()
    print(f"  daily_bar 共 {len(rows)} 个 (code,adjust) 分组")
    if dry_run:
        # 仅对比差异
        diff = 0
        for code, adj, cnt, mn, mx in rows:
            old = cur.execute(
                "SELECT rows, start_date, end_date FROM bar_meta WHERE code=? AND adjust=?",
                (code, adj)).fetchone()
            if not old or old[0] != cnt or old[1] != mn or old[2] != mx:
                diff += 1
        print(f"  [dry-run] {diff} 组 bar_meta 与 daily_bar 不一致（将更新）")
        con.close()
        return {"groups": len(rows), "diff": diff, "dry_run": True}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_upd = n_ins = 0
    for code, adj, cnt, mn, mx in rows:
        exists = cur.execute(
            "SELECT 1 FROM bar_meta WHERE code=? AND adjust=?", (code, adj)).fetchone()
        if exists:
            cur.execute(
                "UPDATE bar_meta SET rows=?, start_date=?, end_date=?, updated_at=? "
                "WHERE code=? AND adjust=?", (cnt, mn, mx, now, code, adj))
            n_upd += 1
        else:
            cur.execute(
                "INSERT INTO bar_meta (code, adjust, rows, start_date, end_date, updated_at) "
                "VALUES (?,?,?,?,?,?)", (code, adj, cnt, mn, mx, now))
            n_ins += 1
    con.commit()
    con.close()
    print(f"  bar_meta 更新 {n_upd} 组 / 新增 {n_ins} 组（rows=累计覆盖，start/end=MIN/MAX date）")
    return {"updated": n_upd, "inserted": n_ins}


def main():
    ap = argparse.ArgumentParser(description="重算 bar_meta.rows（F-4 累计覆盖口径）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    db = Path(args.db) if args.db else _resolve_cache_dir() / "bars.db"
    if not db.exists():
        print(f"[错误] 找不到 {db}")
        sys.exit(2)
    print(f"=== recompute_bar_meta（{'dry-run' if args.dry_run else '执行'}）=== 目标: {db}")
    recompute(db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
