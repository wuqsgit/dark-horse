# Alpha Probe Entry Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Alpha probe signals from opening positions before futures volume and price structure confirm the move.

**Architecture:** Add one pure decision helper at the live Alpha entry boundary. It consumes the existing dual-market volume state, 15-minute breakout confirmation, and market phase, then returns an explicit allow/block reason used by the existing candidate and decision logging paths.

**Tech Stack:** Python 3, unittest, existing trader execution pipeline.

## Global Constraints

- Apply only to new Alpha probe entries.
- Do not change existing position management, hard stops, or normal-market entries.
- Fail closed when synchronization or structure evidence is missing.

---

### Task 1: Enforce Alpha Probe Confirmation

**Files:**
- Modify: `trader/execution.py`
- Test: `tests/test_alpha_probe_entry_gate.py`

**Interfaces:**
- Consumes: `raw_alpha["dual_market_volume"]`, breakout confirmation result, `raw_alpha["market_phase"]`, and current entry status.
- Produces: `_alpha_probe_entry_decision(...) -> tuple[bool, str]`.

- [x] **Step 1: Write failing tests** for missing synchronization, missing structure confirmation, breakdown-risk phase, and a fully confirmed probe.
- [x] **Step 2: Run the focused test** and verify the helper is missing.
- [x] **Step 3: Add the minimal decision helper and call it before position sizing.**
- [x] **Step 4: Run focused and related Alpha entry tests.**
- [x] **Step 5: Compile the changed Python modules and verify the restarted trader process.**
