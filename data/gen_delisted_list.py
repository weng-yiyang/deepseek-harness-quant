# -*- coding: utf-8 -*-
"""data/gen_delisted_list.py — 生成 delisted_list.csv（F-2 数据来源，原仓库缺失）

为什么需要：data/backfill_delisted.py 与审计 A3 都依赖 cache/delisted_list.csv，
但仓库里没有任何脚本生成它 —— 这是 F-2 一直无法执行的根因。

输出：<cache>/delisted_list.csv，列与 backfill_delisted.py 读取键对齐：
      code, 证券代码, 公司简称, 上市日期, 终止上市日期, 暂停上市日期

数据源（按可用性自动选择）：
  1) tushare：stock_basic(list_status='D' 终止 / 'P' 暂停) —— 干净含 delist_date（推荐）
  2) akshare：stock_zh_a_stop_em（备用）
  3) 均无 → 写出带表头的空模板并提示手动填，不中断编排（后续步骤会跳过 0 只）

用法：
  python data/gen_delisted_list.py                 # 自动选源生成
  python data/gen_delisted_list.py --source tushare
  python data/gen_delisted_list.py --source akshare
"""
import argparse
import csv
import os
import sys
import warnings
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


OUT_COLUMNS = ["code", "证券代码", "公司简称", "上市日期", "终止上市日期", "暂停上市日期"]


def _from_tushare(token: str):
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    out = []
    for status in ("D", "P"):  # D=终止上市 P=暂停上市
        df = pro.stock_basic(exchange="", list_status=status,
                             fields="ts_code,symbol,name,list_date,delist_date")
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            code = str(r["symbol"]).zfill(6)  # '600068'
            ts_code = str(r["ts_code"])        # '600068.SH'
            out.append({
                "code": code, "证券代码": ts_code, "公司简称": r["name"] or "",
                "上市日期": (r["list_date"] or "")[:10],
                "终止上市日期": (r["delist_date"] or "")[:10] if status == "D" else "",
                "暂停上市日期": (r["delist_date"] or "")[:10] if status == "P" else "",
            })
    return out


def _from_akshare():
    import akshare as ak
    df = ak.stock_zh_a_stop_em()
    out = []
    # 列名随 akshare 版本变化，做容错映射
    col = {c: c for c in df.columns}
    code_key = next((c for c in df.columns if "代码" in c), None)
    name_key = next((c for c in df.columns if "名称" in c or "简称" in c), None)
    delist_key = next((c for c in df.columns if "终止" in c or "退市" in c or "摘牌" in c), None)
    pause_key = next((c for c in df.columns if "暂停" in c), None)
    if not code_key:
        raise RuntimeError("akshare stock_zh_a_stop_em 未识别到代码列")
    for _, r in df.iterrows():
        code = str(r[code_key]).zfill(6)
        out.append({
            "code": code, "证券代码": f"{code}.SH" if code[0] in "69" else f"{code}.SZ",
            "公司简称": str(r[name_key]) if name_key else "",
            "上市日期": "", "终止上市日期": str(r[delist_key]) if delist_key else "",
            "暂停上市日期": str(r[pause_key]) if pause_key else "",
        })
    return out


def generate(source: str = "auto", token: str = None) -> int:
    cache = _resolve_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    out_path = cache / "delisted_list.csv"

    rows = []
    tried = []
    if source in ("auto", "tushare"):
        try:
            tk = token or _read_tushare_token()
            if tk:
                rows = _from_tushare(tk)
                tried.append("tushare")
        except Exception as e:
            print(f"  [tushare] 失败: {e}")
    if not rows and source in ("auto", "akshare"):
        try:
            rows = _from_akshare()
            tried.append("akshare")
        except Exception as e:
            print(f"  [akshare] 失败: {e}")

    if not rows:
        print("[警告] 两个数据源都不可用（未装库/无 token/无网络）→ 写出空模板，F-2 后续将跳过 0 只")
        print(f"         请手动填充 {out_path}（列：{OUT_COLUMNS}）后重跑本脚本")
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=OUT_COLUMNS).writeheader()
        return 0

    # 去重（同一 code 可能 D/P 都出现）
    seen, uniq = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        uniq.append(r)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        w.writeheader()
        w.writerows(uniq)
    print(f"  数据源: {tried} → 写出 {len(uniq)} 只（含终止/暂停上市）至 {out_path}")
    return len(uniq)


def _read_tushare_token() -> str:
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
        return (cfg or {}).get("data", {}).get("tushare_token") or ""
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description="生成 delisted_list.csv（F-2 数据来源）")
    ap.add_argument("--source", default="auto", choices=["auto", "tushare", "akshare"])
    ap.add_argument("--token", default=None, help="tushare token（优先于 params.yaml）")
    args = ap.parse_args()
    print("=== gen_delisted_list ===")
    n = generate(source=args.source, token=args.token)
    print(f"完成：{n} 只写入 delisted_list.csv")
    if n <= 0:
        # ★不要静默成功：取到 0 条时以非零退出码标记失败，
        # 上层编排（repair_phase1 的 _network_step）才能感知并自动切换到备份源。
        print("[FAIL] 未取到任何退市标的（接口可能变更 / 返回为空 / 网络异常）")
        sys.exit(1)


if __name__ == "__main__":
    main()
