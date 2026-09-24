"""L4 投研层（research 包）。

设计出处：docs/26-09-20-投研基建架构/2026-09-21-总体方案设计.md §6。
对象模型：课题（topic）→ 实验（experiment）两级；实验 = 固定骨架 + 已注册
评估模块 + spec + 假设 + verdict。台账 append-only 由 db.py 触发器强制，
状态机合法性由 lifecycle.py 强制。
"""
