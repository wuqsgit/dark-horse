# Lifecycle Category Advice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a small set of evidence-backed category recommendations only from complete trade lifecycle reviews.

**Architecture:** Replace the current dominant-text summary with metric-based issue classification and two-stage aggregation. Preserve the existing API field, add evidence fields, and render a compact frontend table.

**Tech Stack:** Python 3, unittest, React/Vite.

## Global Constraints

- Minimum category sample is 8.
- Minimum issue sample is 3 and minimum issue rate is 30%.
- Keep one issue per category and at most five merged recommendations.
- Do not change live entry or exit execution.

---

### Task 1: Lifecycle Recommendation Aggregator

**Files:**
- Modify: `shared/policy_loop.py`
- Create: `tests/test_policy_loop_lifecycle_summary.py`

**Interfaces:**
- Consumes: complete lifecycle review dictionaries.
- Produces: `summarize_trade_lifecycle_reviews(reviews, min_category_samples=8, min_issue_samples=3, min_issue_rate=0.30, limit=5)`.

- [ ] Write failing tests for sample thresholds, issue thresholds, per-category deduplication, cross-category merging, and the global limit.
- [ ] Run the focused test and verify the old implementation fails the new contract.
- [ ] Implement metric-based issue classification and aggregation.
- [ ] Run focused and existing policy-loop tests.

### Task 2: Compact Category Advice UI

**Files:**
- Modify: `frontend/src/components/BacktestPanel.jsx`

**Interfaces:**
- Consumes: enriched `trade_review_summaries` rows.
- Produces: compact evidence and recommendation rows without horizontal scrolling.

- [ ] Replace the old dominant-conclusion columns with priority, evidence, representative symbols, conclusion, and action.
- [ ] Build the frontend and verify the API response shape.
- [ ] Restart the API and frontend-facing services needed to serve the changes.
