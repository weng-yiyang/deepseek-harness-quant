# -*- coding: utf-8 -*-
"""DeepSeek HARNESS Quant · 主入口（CLI 分发器）

设计：本文件只做命令分发，真实逻辑在各子模块自身的 ``if __name__ == "__main__"`` 中
（与项目既有模式一致，如 strategy/pitch_v2.py 通过 subprocess 调用 scan.py）。
接入 AI 后的完整能力由 launcher.py 启动（量化 Deck + HARNESS）。

注意：不再在模块加载时读取 config/params.yaml（该文件 gitignored，克隆后不存在，
旧版骨架会在任何命令下都抛 FileNotFoundError）。配置由各子模块按需惰性加载。

用法：
    python main.py serve [--port 8787]     启动 Web 决策台（deck/deck_server.py）
    python main.py launch                  启动完整系统（量化 Deck + HARNESS，见 launcher.py）
    python main.py validate [--quick]      数据审计 / 风控前置闸门（risk/data_audit.py）
    python main.py backtest [--mode ...]   回测引擎（backtest/bt_engine.py）
    python main.py pitch [--force]         构建 Pitch 决策卡（strategy/pitch_v2.py）
    python main.py update                  数据增量更新（data/daily_pipeline.py）
    python main.py help                    显示本帮助
"""
import argparse
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _run(module_rel: str, extra):
    """委派执行仓库内某模块（以其自身 __main__ 逻辑为准）。"""
    target = BASE / module_rel
    if not target.exists():
        print(f"[错误] 模块不存在: {target}")
        return 2
    cmd = [sys.executable, str(target), *extra]
    print(f"[main] 执行: {' '.join(cmd)}")
    return subprocess.call(cmd)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="main.py", description="DeepSeek HARNESS Quant CLI 分发器")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="启动 Web 决策台")
    p.add_argument("--port", default="8787", help="监听端口（默认 8787）")
    sub.add_parser("validate", help="数据审计 / 风控前置闸门").add_argument(
        "--quick", action="store_true", help="轻量审计")
    sub.add_parser("audit", help="数据审计（validate 别名）").add_argument(
        "--quick", action="store_true", help="轻量审计")
    sub.add_parser("backtest", help="回测引擎")
    p = sub.add_parser("pitch", help="构建 Pitch 决策卡")
    p.add_argument("--force", action="store_true", help="重新扫描机会引擎")
    sub.add_parser("update", help="数据增量更新")
    sub.add_parser("launch", help="启动完整系统（launcher.py）")
    sub.add_parser("help", help="显示帮助")

    args = parser.parse_args(argv)
    cmd = args.cmd

    if cmd is None or cmd == "help":
        parser.print_help()
        return 0
    if cmd == "launch":
        return _run("launcher.py", [])
    if cmd == "serve":
        return _run("deck/deck_server.py", ["--port", args.port])
    if cmd in ("validate", "audit"):
        extra = ["--quick"] if getattr(args, "quick", False) else []
        return _run("risk/data_audit.py", extra)
    if cmd == "backtest":
        return _run("backtest/bt_engine.py", [])
    if cmd == "pitch":
        extra = ["--force"] if args.force else []
        return _run("strategy/pitch_v2.py", extra)
    if cmd == "update":
        return _run("data/daily_pipeline.py", [])
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
