# -*- coding: utf-8 -*-
"""data/backfill_delisted_tushare.py — 数据修复 F-2（Tushare 版，消除 baostock 停服风险）

背景（审计 A3）：delisted_list.csv 中 2019 后终止上市共 ~148 只，缓存覆盖 0 只
      → 回测池只含"活着的股票" → 幸存者偏差（收益虚高/回撤低估）。
原 baostock 版（backfill_delisted.py）依赖 baostock，2024 起多次停服 → 本地跑不通。

本版改用 Tushare：
  - pro_bar(ts_code, adj='qfq') 拉取退市股历史前复权日线（含停复牌前后，退市股历史数据可拉）
  - is_st 由 get_stock_st_intervals() 同源标注（与 F-1 一致）
  - 写 daily_bar（source='tushare'，cache.normalize_units 在读取时统一 千元/手 → 元/股）

特性：
- 断点续传：logs/backfill_delisted_tushare_progress.txt
- dry-run 只读验证前 10 只
- 不依赖 baostock；需 params.yaml 的 tushare_token
- 复用 DailyCache.put_daily（双库写保护路由已内置）

用法：
  python data/backfill_delisted_tushare.py --dry-run    # 前 10 只验证（只读）
  python data/backfill_delisted_tushare.py --limit 20   # 部分
  python data/backfill_delisted_tushare.py              # 全量 ~148 只
  python data/backfill_delisted_tushare.py --status
"""
import argparse
import csv
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
from data.cache import DailyCache
from data.fetcher_tushare import _pro, _call, get_stock_st_intervals

LOG_DIR = BASE / "logs"
PROGRESS_FILE = LOG_DIR / "backfill_delisted_tushare_progress.txt"
FAILED_FILE = LOG_DIR / "backfill_delisted_tushare_failed.csv"
START_BASE = "2019-01-01"  # 与主数据起点一致


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "backfill_delisted_tushare.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def to_std(code6: str) -> str:
    """6 位数字 → '600068.SH'/'000004.SZ'；北交所 4/8 开头 → None（Tushare 退市表多为沪/深）"""
    c = str(code6).strip().zfill(6)
    if c[0] in ("6", "9"):
        return f"{c}.SH"
    if c[0] in ("0", "2", "3"):
        return f"{c}.SZ"
    return None


def load_targets(limit=None):
    """读 delisted_list.csv → [(std_code, name, start, end)]，仅 2019 后终止/暂停上市"""
    cache = DailyCache()
    csv_path = Path(cache.db_path).parent / "delisted_list.csv"
    if not csv_path.exists():
        print(f"  [错误] 未找到 {csv_path} → 请先运行 gen_delisted_list.py 生成")
        return []
    targets = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            end = (row.get("终止上市日期") or "").strip()
            if not end:
                end = (row.get("暂停上市日期") or "").strip()
            if not end or end < START_BASE:
                continue
            std = to_std(row.get("code") or row.get("证券代码") or "")
            if std is None:
                continue
            start = (row.get("上市日期") or "").strip()
            start = max(start, START_BASE) if start >= "2000-01-01" else START_BASE
            targets.append((std, (row.get("公司简称") or row.get("证券简称") or "").strip(),
                           start, end))
    seen, out = set(), []
    for t in targets:
        if t[0] not in seen:
            seen.add(t[0])
            out.append(t)
    if limit:
        out = out[:limit]
    return out


def _build_df(pro, std_code, start, end, st_intervals):
    """拉 qfq 日线并组装标准 columns；返回 DataFrame 或 None"""
    df = _call(pro.pro_bar, ts_code=std_code, adj="qfq",
               start_date=start.replace("-", ""), end_date=end.replace("-", ""))
    if df is None or df.empty:
        return None
    out = pd.DataFrame()
    out["date"] = df["trade_date"].astype(str) if "trade_date" in df else df.get("trade_date")
    out["open"] = pd.to_numeric(df.get("open"), errors="coerce")
    out["high"] = pd.to_numeric(df.get("high"), errors="coerce")
    out["low"] = pd.to_numeric(df.get("low"), errors="coerce")
    out["close"] = pd.to_numeric(df.get("close"), errors="coerce")
    out["preclose"] = pd.to_numeric(df.get("pre_close"), errors="coerce")
    # Tushare vol=手 amount=千元；source='tushare' → cache.normalize_units 读取时统一为 股/元
    out["volume"] = pd.to_numeric(df.get("vol"), errors="coerce")
    out["amount"] = pd.to_numeric(df.get("amount"), errors="coerce")
    if "pct_change" in df.columns:
        out["pct_chg"] = pd.to_numeric(df["pct_change"], errors="coerce")
    elif "pct_chg" in df.columns:
        out["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    else:
        out["pct_chg"] = (out["close"] / out["preclose"] - 1) * 100
    out["turn"] = pd.to_numeric(df.get("turn"), errors="coerce") if "turn" in df else None
    # is_st 同源标注
    ivs = st_intervals.get(std_code, [])
    out["is_st"] = [1 if any(s <= d <= e for s, e in ivs) else 0 for d in out["date"].astype(str)]
    return out


def run(dry_run=False, limit=None):
    targets = load_targets(limit=limit)
    if not targets:
        print("  无待补拉目标（delisted_list.csv 为空或全早于 2019）→ 跳过")
        return
    log(f"2019 后退市股待补拉 {len(targets)} 只（dry_run={dry_run}）")

    pro = _pro()
    st_intervals = get_stock_st_intervals(start_year=2010)
    cache = DailyCache()
    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [t for t in targets if t[0] not in done]

    if dry_run:
        log("=== dry-run 验证（前 10 只，只读不写）===")
        tot_rows = tot_st = 0
        for std, name, start, end in todo[:10]:
            df = _build_df(pro, std, start, end, st_intervals)
            n = 0 if df is None else len(df)
            st = 0 if df is None else int(df["is_st"].sum())
            tot_rows += n
            tot_st += st
            log(f"  {std} {name}: [{start}~{end}] {n} 行，is_st=1 {st} 天")
        log(f"dry-run: 前 10 只共 {tot_rows} 行 / {tot_st} 天 ST → 确认后全量跑")
        return

    log(f"=== 开始全量补拉 {len(todo)} 只 ===")
    start_ts = time.time()
    n_ok = n_rows = n_st_days = 0
    failed = []
    for std, name, start, end in todo:
        try:
            df = _build_df(pro, std, start, end, st_intervals)
            if df is None or df.empty:
                failed.append((std, name, "empty"))
                log(f"  {std} 空数据（可能已无历史行情）")
                continue
            cache.put_daily(std, df, adjust="qfq", source="tushare")
            n = len(df)
            st = int(df["is_st"].sum())
            n_ok += 1
            n_rows += n
            n_st_days += st
            with open(PROGRESS_FILE, "a", encoding="utf-8") as pf:
                pf.write(std + "\n")
            if n_ok % 20 == 0:
                log(f"进度 {n_ok}/{len(todo)}，已入库 {n_rows} 行（耗时 {(time.time()-start_ts)/60:.1f} 分钟）")
        except Exception as e:
            failed.append((std, name, str(e)[:120]))
            log(f"  {std} 失败: {e}")

    el = (time.time() - start_ts) / 60
    log(f"完成: 成功 {n_ok} 只 / {n_rows} 行 / ST {n_st_days} 天，耗时 {el:.1f} 分钟")
    if failed:
        with open(FAILED_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "name", "reason"])
            w.writerows(failed)
        log(f"失败清单: {FAILED_FILE}（{len(failed)} 只，可重跑续传）")


def status():
    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    total = len(load_targets())
    print(f"进度: {len(done)} / {total}（剩余 {total - len(done)}）")
    if FAILED_FILE.exists():
        print(f"失败清单 {FAILED_FILE} 存在（{sum(1 for _ in open(FAILED_FILE, encoding='utf-8')) - 1} 只）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="补拉 2019 后退市股（F-2 Tushare 版）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(dry_run=args.dry_run, limit=args.limit)
