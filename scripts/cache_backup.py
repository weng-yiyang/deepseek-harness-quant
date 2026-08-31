# -*- coding: utf-8 -*-
"""data/cache 备份与恢复脚本（2026-08-31 事故后新增）。

背景：2026-08-31 合并 PR 时 git 切分支把 data/cache/（.gitignore 忽略目录，
含 1.6G bars.db）删进回收站，差点丢失 2 小时建库成果。本脚本提供：

  python scripts/cache_backup.py backup     # 备份 data/cache → backups/cache_<ts>/
  python scripts/cache_backup.py restore    # 从最新备份恢复 data/cache/
  python scripts/cache_backup.py list       # 列出所有备份
  python scripts/cache_backup.py verify     # 校验 bars.db 完整性（integrity_check + 行数）

用法建议：
  - 每次 git 切分支 / merge / 大操作前：python scripts/cache_backup.py backup
  - 怀疑数据异常时：python scripts/cache_backup.py verify
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
CACHE_DIR = BASE / "data" / "cache"
BACKUP_ROOT = BASE / "backups" / "cache"   # .gitignore 已忽略 backups/
KEY_DBS = ["bars.db", "oms.db", "paper.db", "bars_incr.db"]


def log(msg: str):
    print(f"[cache_backup] {msg}")


def backup() -> int:
    if not CACHE_DIR.exists():
        log(f"错误: {CACHE_DIR} 不存在，无需备份")
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_ROOT / f"cache_{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
            n += 1
    log(f"已备份 {n} 个文件 → {dest}")
    # 报告大小
    total = sum(f.stat().st_size for f in dest.iterdir() if f.is_file())
    log(f"备份总大小: {total / 1024 / 1024:.1f} MB")
    return 0


def _latest_backup() -> Path | None:
    if not BACKUP_ROOT.exists():
        return None
    dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()])
    return dirs[-1] if dirs else None


def restore() -> int:
    src = _latest_backup()
    if not src:
        log("错误: 没有任何备份可恢复")
        return 1
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, CACHE_DIR / f.name)
            n += 1
    log(f"已从 {src} 恢复 {n} 个文件 → {CACHE_DIR}")
    return 0


def list_backups() -> int:
    if not BACKUP_ROOT.exists():
        log("暂无备份")
        return 0
    for d in sorted(BACKUP_ROOT.iterdir()):
        if d.is_dir():
            total = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            log(f"{d.name}  ({total / 1024 / 1024:.1f} MB)")
    return 0


def verify() -> int:
    db = CACHE_DIR / "bars.db"
    if not db.exists():
        log(f"错误: {db} 不存在！数据可能丢失")
        return 1
    try:
        con = sqlite3.connect(str(db))
        r = con.execute("PRAGMA integrity_check").fetchone()
        n = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM bar_meta").fetchone()[0]
        if r[0] != "ok":
            log(f"integrity FAIL: {r[0]}")
            return 1
        log(f"integrity: ok | daily_bar={n:,} 行 | bar_meta={m:,} 行")
        return 0
    except Exception as e:
        log(f"验证失败: {type(e).__name__}: {e}")
        return 1


def main():
    ap = argparse.ArgumentParser(description="data/cache 备份/恢复/校验")
    ap.add_argument("action", choices=["backup", "restore", "list", "verify"])
    args = ap.parse_args()
    if args.action == "backup":
        return backup()
    if args.action == "restore":
        return restore()
    if args.action == "list":
        return list_backups()
    if args.action == "verify":
        return verify()
    return 1


if __name__ == "__main__":
    sys.exit(main())
