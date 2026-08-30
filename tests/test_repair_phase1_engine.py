# -*- coding: utf-8 -*-
"""tests/test_repair_phase1_engine.py — 修复编排器默认引擎与故障转移验证

覆盖：默认主源为 baostock（免费免 token）、主源失败自动切 tushare 备份、
--no-fallback 关闭切换、本地步骤始终执行、退市清单源选择、CLI 默认参数。
用 mock 拦截子进程，不真跑网络。

★两个踩坑点（写这类测试时注意）：
1. 必须替换 repair_phase1 模块里的 subprocess **引用**（rp.subprocess = fake），
   不能 setattr(rp.subprocess, "run", ...) —— 那会改到全局 subprocess 模块，
   连带污染 pytest 自己发起的子进程调用。
2. Windows 路径是反斜杠，断言"data/xxx.py"永远匹配不上；
   统一用 Path(...).name 取文件名做精确匹配（也避免 xxx.py 误匹配 xxx_tushare.py）。
"""
import subprocess
import sys
import types
from pathlib import Path

import pytest

import data.repair_phase1 as rp

BASE = Path(__file__).resolve().parent.parent


def _fake_run_factory(calls, tushare_succeeds=True):
    """构造假的 subprocess.run：命令中含 tushare 则成功（模拟 baostock 不可用）"""
    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        ok = ("tushare" in joined) if tushare_succeeds else True
        # _run() 现为 capture_output=True，会读 p.stdout / p.stderr，fake 必须带这两个属性
        return types.SimpleNamespace(
            returncode=0 if ok else 1,
            stdout="mock stdout" if ok else "",
            stderr="" if ok else "mock stderr：模拟该源失败",
        )
    return _fake_run


def _scripts(calls):
    """从调用记录中提取被执行脚本的**文件名**（精确匹配用）"""
    out = []
    for cmd in calls:
        for c in cmd:
            s = str(c)
            if s.endswith(".py"):
                out.append(Path(s).name)
    return out


def _args_of(calls, script_name):
    """返回某脚本被调用时的参数列表（取首次）"""
    for cmd in calls:
        joined = " ".join(str(c) for c in cmd)
        if script_name in joined:
            return " ".join(str(c) for c in cmd)
    return ""


@pytest.fixture
def patched(monkeypatch):
    """拦截子进程 + 审计闸门，避免真实网络与真实库"""
    calls = []
    monkeypatch.setattr(rp, "subprocess",
                        types.SimpleNamespace(run=_fake_run_factory(calls)))
    monkeypatch.setattr(rp, "_gate", lambda: (True, {}))
    return calls


# ---------- 1) 默认引擎 ----------
def test_run_repair_defaults_to_baostock(patched):
    s = rp.run_repair()
    assert s["engine_primary"] == "baostock"
    assert s["engine_backup"] == "tushare"


def test_cli_default_engine_is_baostock():
    """用户实际敲命令时的默认值（直接验证解析结果，不依赖 --help 文本格式）"""
    parser = rp.build_parser()
    assert parser.parse_args([]).engine == "baostock"          # 无参数 → baostock
    assert parser.parse_args(["--engine", "tushare"]).engine == "tushare"
    assert parser.parse_args([]).no_fallback is False          # 默认启用故障转移


# ---------- 2) 自动故障转移 ----------
def test_fallback_to_tushare_when_baostock_fails(patched):
    s = rp.run_repair(engine="baostock")
    scripts = _scripts(patched)

    # 主源 baostock 版被尝试过
    assert "backfill_delisted.py" in scripts
    assert "fix_st_flags.py" in scripts
    # 主源失败后自动切 tushare 备份版
    assert "backfill_delisted_tushare.py" in scripts
    assert "fix_st_flags_tushare.py" in scripts
    # 最终成功的是备份源
    assert s["steps"]["1_backfill"] == "F-2/tushare"
    assert s["steps"]["2_st"] == "F-1/tushare"


def test_fallback_to_baostock_when_tushare_primary_fails(monkeypatch):
    """主源设为 tushare 时，tushare 失败 → 自动切 baostock"""
    calls = []

    def _fake(cmd, **kw):
        calls.append(list(cmd))
        ok = "tushare" not in " ".join(str(c) for c in cmd)   # tushare 失败，baostock 成功
        return types.SimpleNamespace(
            returncode=0 if ok else 1,
            stdout="mock stdout" if ok else "",
            stderr="" if ok else "mock stderr：模拟该源失败",
        )

    monkeypatch.setattr(rp, "subprocess", types.SimpleNamespace(run=_fake))
    monkeypatch.setattr(rp, "_gate", lambda: (True, {}))

    s = rp.run_repair(engine="tushare")
    assert s["engine_primary"] == "tushare"
    assert s["engine_backup"] == "baostock"
    assert s["steps"]["2_st"] == "F-1/baostock"


def test_no_fallback_disables_switching(monkeypatch):
    calls = []
    monkeypatch.setattr(rp, "subprocess",
                        types.SimpleNamespace(run=_fake_run_factory(calls)))
    monkeypatch.setattr(rp, "_gate", lambda: (True, {}))

    s = rp.run_repair(engine="baostock", no_fallback=True)
    scripts = _scripts(calls)
    # 只尝试主源，不出现 tushare 备份脚本
    assert "backfill_delisted.py" in scripts
    assert "backfill_delisted_tushare.py" not in scripts
    assert s["engine_backup"] is None


# ---------- 3) 本地步骤始终执行 ----------
def test_local_steps_always_run(patched):
    rp.run_repair(skip_network=True)
    scripts = _scripts(patched)
    assert "repair_consistency.py" in scripts
    assert "recompute_bar_meta.py" in scripts
    # 跳过网络步骤时不应出现网络脚本
    assert "fix_st_flags.py" not in scripts
    assert "backfill_delisted.py" not in scripts


# ---------- 4) 退市清单源选择 ----------
def test_list_source_akshare_first_when_baostock_primary(patched):
    """主源 baostock（零 token 路径）时，退市清单优先用 akshare"""
    rp.run_repair(engine="baostock", no_fallback=True)
    first = " ".join(str(c) for c in patched[0])
    assert "gen_delisted_list.py" in first
    assert "akshare" in first


def test_list_source_tushare_first_when_tushare_primary(monkeypatch):
    calls = []
    monkeypatch.setattr(rp, "subprocess",
                        types.SimpleNamespace(run=_fake_run_factory(calls)))
    monkeypatch.setattr(rp, "_gate", lambda: (True, {}))
    rp.run_repair(engine="tushare", no_fallback=True)
    first = " ".join(str(c) for c in calls[0])
    assert "gen_delisted_list.py" in first
    assert "tushare" in first


# ---------- 5) 财报步骤 ----------
def test_finance_step_uses_tushare(patched):
    rp.run_repair(engine="baostock", finance=True, no_fallback=True)
    assert "fetch_quality_tushare.py" in _scripts(patched)


def test_finance_step_skipped_without_flag(patched):
    rp.run_repair(engine="baostock", finance=False, no_fallback=True)
    assert "fetch_quality_tushare.py" not in _scripts(patched)
