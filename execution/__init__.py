# -*- coding: utf-8 -*-
"""execution 包 — Phase 2 执行层（仿真券商 + OMS）。

设计原则：先仿真、human-in-the-loop、不接真实资金。
- Broker 抽象形态与真实券商一致，Phase 6 接真实券商时 OMS/loop 不变（单一路径）。
- PaperAccount 为现金/持仓/净值唯一真相源；成交由 OMS 应用（防"模拟一套实盘一套"）。
- 所有下单前接 Phase 1 数据审计闸门：脏数据（STOP.md / 审计 FAIL）一律不下单。
"""
