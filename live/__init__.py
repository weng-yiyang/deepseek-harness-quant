# -*- coding: utf-8 -*-
"""live/ — 盘前盘后编排（Phase 4）

把「扫描 → 人工审批 → 次日下单」串成可被外部调度器（cron / Windows 计划任务）
调用的定时链路。所有步骤幂等，可安全重复运行。

链路：
  T 日收盘后  live/post_close.py   数据刷新 → 机会扫描 → Pitch → 生成次日(T+1)候选订单计划
  ────────── 人工在 Deck 审批 → logs/deck_decisions.json（JSONL）──────────
  T+1 开盘前  live/pre_market.py   数据闸门 → 交易日校验 → 读已审批条目 → 盘前过滤 → 执行

设计：
- **human-in-the-loop 的位置在 Deck 审批**：机器只负责生成候选与执行"人已批准"的订单，
  不代替人做买入决策（与 pitch_v2 既有流程一致）。
- 幂等：同一交易日不重复生成计划（--force 覆盖）；执行由 OMS 订单状态保证。
- 不接真实资金：执行仍走 execution/ 的仿真券商（Phase 6 才接真实券商）。
- 步骤失败不静默：每步结果记入状态文件 logs/phase4_state.json。
"""
