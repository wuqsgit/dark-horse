# AI Status Popover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show AI workflow and live progress in an accessible hover/click popover attached to the existing status chip.

**Architecture:** Extend the existing AI status DTO with stored sample counters, then render those fields in `AIQualityStatus.jsx`. Use local React state and CSS hover/focus behavior; do not add another route or service.

**Tech Stack:** Python, unittest, React, CSS, Vite.

## Global Constraints

- Do not change AI decision or training thresholds.
- Display maintenance times in UTC+8.
- Desktop hover and keyboard focus must work; click must support touch devices.
- Keep the panel compact and avoid horizontal overflow.

---

### Task 1: Status Counters

**Files:**
- Modify: `tests/test_ai_quality_service.py`
- Modify: `ai_service/service.py`

**Interfaces:**
- Produces: `models.<key>.total_samples`, `pending_samples`, and `sample_count`.

- [ ] Add a failing test that records one pending and one labeled sample and asserts all three counters.
- [ ] Run the focused test and confirm the missing-field failure.
- [ ] Return the counters from `EntryQualityService.status()`.
- [ ] Run AI service tests.

### Task 2: Hover Panel

**Files:**
- Modify: `frontend/src/components/AIQualityStatus.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `/api/ai/status`.
- Produces: hover/focus/click status panel.

- [ ] Add pure formatting/progress helpers and Node tests.
- [ ] Implement workflow text, progress bars, decision counters, UTC+8 timestamps, and error state.
- [ ] Add responsive CSS with no horizontal scrolling.
- [ ] Run frontend tests and production build.

### Task 3: Runtime Verification

**Files:**
- No source additions unless verification finds a scoped issue.

- [ ] Run full focused backend and frontend tests.
- [ ] Restart AI, API, and frontend services without restarting Trader.
- [ ] Verify the status endpoint and rendered popover.
