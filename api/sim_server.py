# -*- coding: utf-8 -*-
"""api/sim_server.py — 仿真交易控制台 API 服务（Phase 5）

把 Phase 2-4 的「盘后生成计划 → 人工审批 → 盘前执行 → 持仓/绩效」链路
以 JSON API 暴露给 UI v2 控制台页面，浏览器点鼠标即可操作。

设计约束：
- **零新依赖**：仅 Python 标准库（http.server + json + sqlite3），与 launcher.py 一致，内网可跑。
- **复用不重造**：审批走 execution/oms.py 同一套逻辑；数据读 oms.db / paper.db；编排调 live 的 run()。
- **安全默认（fail-closed）**：
  - 所有写操作（approve/reject/run_*) 要求 `by` 操作人参数留痕。
  - 盘前执行前检查数据闸门（STOP.md / 审计），FAIL 时拒绝执行。
  - 明文标注"仅仿真"，不接真实资金。

用法：
  python api/sim_server.py [--port 8100] [--host 127.0.0.1]
  浏览器开 http://127.0.0.1:8100/sim_control.html
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    from execution.oms import OMS
    from strategy.paper_account import PaperAccount
    from live import post_close, pre_market
    _IMPORT_OK = True
except Exception as e:  # pragma: no cover - 导入失败时降级
    _IMPORT_OK = False
    _IMPORT_ERR = f"{type(e).__name__}: {e}"

OMS_DB = BASE / "data" / "cache" / "oms.db"
PAPER_DB = BASE / "data" / "cache" / "paper.db"
UI_DIR = BASE / "ui_v2" / "pages"
STATE_FILE = BASE / "logs" / "phase4_state.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(d, status=200):
    body = json.dumps(d, ensure_ascii=False, default=str).encode("utf-8")
    return body, status


# ---------------------------------------------------------------- 数据读取
def _read_orders(status: str | None = None, limit: int = 200) -> dict:
    """读 oms.db 订单列表（不依赖 OMS 类，只读快照）。"""
    if not OMS_DB.exists():
        return {"ok": True, "orders": [], "note": "oms.db 不存在"}
    con = sqlite3.connect(str(OMS_DB))
    con.row_factory = sqlite3.Row
    sql = "SELECT * FROM orders"
    args = []
    if status and status != "ALL":
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return {"ok": True, "orders": rows, "count": len(rows)}


def _read_positions() -> dict:
    """读 paper.db 当前持仓 + 现金。"""
    if not PAPER_DB.exists():
        return {"ok": True, "positions": [], "cash": None}
    con = sqlite3.connect(str(PAPER_DB))
    con.row_factory = sqlite3.Row
    try:
        acc = con.execute(
            "SELECT name, cash, initial_cash FROM accounts ORDER BY created_at LIMIT 1"
        ).fetchone()
        cash = dict(acc) if acc else None
        pos = [dict(r) for r in con.execute(
            "SELECT code, qty, avg_cost, entry_date FROM positions ORDER BY code").fetchall()]
    finally:
        con.close()
    return {"ok": True, "positions": pos, "cash": cash}


def _read_plan() -> dict:
    """读最新次日候选计划（logs/next_day_plan_*.json）。"""
    fs = sorted(BASE.glob("logs/next_day_plan_*.json"), key=lambda p: p.stat().st_mtime)
    if not fs:
        return {"ok": True, "plan": None, "note": "尚无次日计划（先跑盘后链路）"}
    p = fs[-1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"ok": True, "plan": data, "plan_file": p.name,
                "plan_mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return {"ok": False, "error": f"解析 {p.name} 失败: {e}"}


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"ok": True, "state": None}
    try:
        return {"ok": True, "state": json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except Exception:
        return {"ok": True, "state": None}


def _read_equity() -> dict:
    """从 paper.db 读净值序列（最近 60 条）。"""
    if not PAPER_DB.exists():
        return {"ok": True, "equity": []}
    con = sqlite3.connect(str(PAPER_DB))
    con.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "equity_curve" not in tables:
            return {"ok": True, "equity": [], "note": "无净值表（尚无成交）"}
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM equity_curve ORDER BY date DESC LIMIT 60").fetchall()]
        rows.reverse()
    finally:
        con.close()
    return {"ok": True, "equity": rows}


def _audit_gate_ok() -> tuple[bool, str]:
    """数据闸门：STOP.md 存在或审计 FAIL → 阻断。返回 (ok, reason)。"""
    try:
        from risk.data_audit import DataAuditor, _load_config
        a = DataAuditor(_load_config())
        ok, r = a.gate()
        if not ok:
            return False, r.get("block_reason") or "数据审计 FAIL"
        return True, ""
    except Exception as e:
        return False, f"审计调用异常: {type(e).__name__}: {e}"


# ---------------------------------------------------------------- 写操作
def _do_approve(oid: int, by: str) -> dict:
    if not _IMPORT_OK:
        return {"ok": False, "error": f"后端导入失败: {_IMPORT_ERR}"}
    pa = PaperAccount(db_path=str(PAPER_DB))
    om = OMS(account=pa, db_path=str(OMS_DB))
    try:
        ok = om.approve(oid, by=by)
    except Exception as e:
        return {"ok": False, "error": f"审批异常: {type(e).__name__}: {e}"}
    if ok:
        return {"ok": True, "message": f"订单 #{oid} 已批准（by {by}）"}
    try:
        o = om.get(oid)
        return {"ok": False, "error": f"审批失败: 订单 #{oid} 状态={o.status.value if o else '不存在'}"}
    except Exception:
        return {"ok": False, "error": f"审批失败: 订单 #{oid} 不存在"}


def _do_reject(oid: int, by: str, reason: str) -> dict:
    if not _IMPORT_OK:
        return {"ok": False, "error": f"后端导入失败: {_IMPORT_ERR}"}
    pa = PaperAccount(db_path=str(PAPER_DB))
    om = OMS(account=pa, db_path=str(OMS_DB))
    try:
        ok = om.reject(oid, by=by, reason=reason or "控制台驳回")
    except Exception as e:
        return {"ok": False, "error": f"驳回异常: {type(e).__name__}: {e}"}
    if ok:
        return {"ok": True, "message": f"订单 #{oid} 已驳回（by {by}）"}
    return {"ok": False, "error": f"驳回失败: 订单 #{oid}"}


def _do_run_post_close(by: str, force: bool) -> dict:
    if not _IMPORT_OK:
        return {"ok": False, "error": f"后端导入失败: {_IMPORT_ERR}"}
    # 幂等保护：当日已有计划且非 force → 拒绝（路径与 live 编排保持一致）
    today = datetime.now().strftime("%Y-%m-%d")
    plan = post_close.PLAN_DIR / f"next_day_plan_{today}.json"
    if plan.exists() and not force:
        return {"ok": False, "error": f"当日计划已存在（{plan.name}），需 force=true 重生成",
                "hint": "post_close 幂等"}
    try:
        summary = post_close.run(force=force)
        summary["triggered_by"] = by
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": f"盘后链路异常: {type(e).__name__}: {e}"}


def _do_run_pre_market(by: str, dry_run: bool) -> dict:
    if not _IMPORT_OK:
        return {"ok": False, "error": f"后端导入失败: {_IMPORT_ERR}"}
    # 数据闸门（fail-closed）
    ok, reason = _audit_gate_ok()
    if not ok:
        return {"ok": False, "error": f"数据闸门阻断: {reason}"}
    try:
        summary = pre_market.run(dry_run=dry_run, account_name="default")
        summary["triggered_by"] = by
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": f"盘前链路异常: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------- HTTP
class SimHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        return

    def _send(self, body, status=200, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_post(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)

        # 静态页面
        if path in ("/", "/sim_control.html", "/sim_control"):
            page = UI_DIR / "sim_control.html"
            if page.exists():
                self._send(page.read_bytes(), 200, "text/html; charset=utf-8")
            else:
                self._send("sim_control.html 未生成（Phase5 前端待部署）".encode("utf-8"),
                           404, "text/plain; charset=utf-8")
            return

        # API
        if path == "/api/sim/orders":
            status = q.get("status", ["ALL"])[0]
            limit = int(q.get("limit", ["200"])[0])
            self._send(*_json(_read_orders(status, limit)))
            return
        if path == "/api/sim/positions":
            self._send(*_json(_read_positions()))
            return
        if path == "/api/sim/plan":
            self._send(*_json(_read_plan()))
            return
        if path == "/api/sim/state":
            self._send(*_json(_read_state()))
            return
        if path == "/api/sim/equity":
            self._send(*_json(_read_equity()))
            return
        if path == "/api/sim/status":
            self._send(*_json({
                "ok": True, "service": "sim_server", "version": "phase5",
                "import_ok": _IMPORT_OK, "ts": _now(),
                "oms_db": str(OMS_DB), "paper_db": str(PAPER_DB),
            }))
            return

        self._send(*_json({"ok": False, "error": f"未知路径 {path}"}, 404))

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        body = self._read_post()
        by = (body.get("by") or "").strip()
        if not by:
            self._send(*_json({"ok": False, "error": "缺少 by（操作人）参数，写操作必须留痕"}, 400))
            return

        if path == "/api/sim/approve":
            oid = body.get("oid")
            if not oid:
                self._send(*_json({"ok": False, "error": "缺少 oid"}, 400))
                return
            self._send(*_json(_do_approve(int(oid), by)))
            return
        if path == "/api/sim/reject":
            oid = body.get("oid")
            if not oid:
                self._send(*_json({"ok": False, "error": "缺少 oid"}, 400))
                return
            self._send(*_json(_do_reject(int(oid), by, body.get("reason") or "")))
            return
        if path == "/api/sim/run_post_close":
            self._send(*_json(_do_run_post_close(by, force=bool(body.get("force")))))
            return
        if path == "/api/sim/run_pre_market":
            self._send(*_json(_do_run_pre_market(by, dry_run=bool(body.get("dry_run")))))
            return

        self._send(*_json({"ok": False, "error": f"未知路径 {path}"}, 404))


def main():
    ap = argparse.ArgumentParser(description="仿真交易控制台 API 服务（Phase 5）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()

    if not _IMPORT_OK:
        print(f"⚠ 后端导入失败（{_IMPORT_ERR}）→ 只读 API 仍可用，写操作会拒绝")
    print(f"* 仿真交易控制台  @ http://{args.host}:{args.port}/sim_control.html")
    print(f"* 仅仿真，不接真实资金 | Ctrl+C 停止")
    srv = ThreadingHTTPServer((args.host, args.port), SimHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n* 已停止")


if __name__ == "__main__":
    main()
