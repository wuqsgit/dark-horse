# Explosive Move Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a low-frequency explosive-breakout execution lane whose valid triggers survive soft gates, always reach an observable terminal outcome, and may scale from 1x to 2x after confirmation.

**Architecture:** Reuse the existing Alpha volume-price and breakout confirmation logic, tagging qualifying actions as `explosive_breakout`. Centralize soft-control behavior around that tag, make AI quality advisory for tagged actions, and enforce a terminal execution contract in the trader runner. Confirmation adds reuse the existing protected roll-add path with event-level deduplication.

**Tech Stack:** Python, SQLite, unittest/pytest, existing Trader and Alpha engine modules.

**Spec:** `docs/superpowers/specs/2026-08-27-explosive-move-capture.md`

## Global Constraints

- Initial explosive entry is 1.0x normal size; confirmed total exposure is capped at 2.0x.
- Existing hard account, data freshness, contract support, capital, spread >=0.35%, duplicate exposure, slippage, and exchange-error controls remain hard.
- AI, market phase, high-risk profile, category occupancy, single depth snapshots, and overheat are not hard vetoes for explosive events.
- Every planned explosive open must have a terminal outcome.

---

### Task 1: Explosive Signal Classification

**Files:**
- Modify: `alpha_engine/volume_price.py`
- Modify: `trader/execution.py`
- Test: `tests/test_alpha_entry_confirmation.py`

**Interfaces:**
- Produces: `volume_price["event_type"] == "explosive_breakout"` for qualifying BTR-like signals.
- Produces: opening actions with `event_type`, `setup_id`, and `soft_gate_override` metadata.

- [ ] Write a BTR replay fixture asserting that score 91.44, 3.5119x spot volume, 2.7481x futures volume, confirmed price, and acceptable spread classify as `explosive_breakout`.
- [ ] Run the focused test and verify it fails because event metadata is absent.
- [ ] Add the minimal event classification and action metadata.
- [ ] Run the focused test and verify it passes.

### Task 2: Advisory AI and Soft-Control Degradation

**Files:**
- Modify: `trader/ai_client.py`
- Modify: `trader/execution.py`
- Modify: `trader/portfolio_risk.py`
- Test: `tests/test_trader_ai_gate.py`
- Test: `tests/test_portfolio_category_limit.py`

**Interfaces:**
- Consumes: action `event_type == "explosive_breakout"`.
- Produces: retained action carrying AI annotations when AI returns `reject`.
- Produces: category/depth/overheat soft reductions with a minimum 0.5x factor.

- [ ] Write failing tests showing AI reject retains an explosive action and ordinary actions remain rejectable.
- [ ] Write failing tests showing occupied categories reduce but do not reject explosive actions.
- [ ] Run focused tests and verify expected failures.
- [ ] Implement advisory AI handling and explicit category soft-control results.
- [ ] Apply the 0.5x minimum soft factor in Alpha action sizing.
- [ ] Run focused tests and verify they pass.

### Task 3: Terminal Execution Contract

**Files:**
- Modify: `trader/runner.py`
- Modify: `trader/execution.py`
- Test: `tests/test_explosive_execution_contract.py`

**Interfaces:**
- Consumes: planned explosive actions.
- Produces: one `execution` decision with `opened`, `rejected`, or `error` for every planned action.

- [ ] Write a failing test for a planned action removed before execution.
- [ ] Write a failing test for successful and failed exchange submissions recording terminal states.
- [ ] Run tests and verify expected failures.
- [ ] Implement pre/post execution reconciliation by run ID, scan ID, symbol, and setup ID.
- [ ] Run focused tests and verify they pass.

### Task 4: One-Time Confirmation Add

**Files:**
- Modify: `trader/execution.py`
- Modify: `shared/db.py`
- Test: `tests/test_explosive_confirmation_add.py`

**Interfaces:**
- Consumes: an open explosive position and latest entry-capable Alpha context.
- Produces: at most one `roll_add` action and persists the add layer.

- [ ] Write a failing test that a profitable confirmed setup adds from 1x to 2x.
- [ ] Write a failing test that the same setup cannot add twice.
- [ ] Run tests and verify expected failures.
- [ ] Implement setup-level confirmation-add persistence using existing position management state.
- [ ] Run focused tests and verify they pass.

### Task 5: Replay and Regression Verification

**Files:**
- Create: `tests/fixtures/btr_20260826_explosive.json`
- Create: `tests/test_explosive_move_replay.py`

**Interfaces:**
- Consumes: production-derived BTR metrics with no credentials or account secrets.
- Produces: deterministic recall and execution-contract regression coverage.

- [ ] Add the sanitized BTR fixture and failing replay assertion.
- [ ] Run the replay test and verify it fails before integration completion.
- [ ] Complete fixture integration and verify the replay passes.
- [ ] Run all backend tests and inspect failures.
- [ ] Review the final diff against the specification.
