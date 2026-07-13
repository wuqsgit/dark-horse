# Alpha Symbol Detail Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `/api/alpha/scan/by_symbol/{alpha_symbol}` from multi-second latency to an indexed point lookup without stale-response caching.

**Architecture:** Replace the endpoint's 500-row candidate fetch and Python scan with one SQL query constrained by `alpha_symbol`. Add composite indexes matching the symbol-plus-time ordering used by candidate, detail, and history lookups.

**Tech Stack:** FastAPI, Python, SQLite, `unittest`.

## Global Constraints

- The endpoint must return the newest candidate for the requested Alpha symbol.
- A candidate for another symbol must never be returned.
- No TTL cache is added; newly committed scan data must be visible on the next request.
- Existing response fields remain unchanged.

---

### Task 1: Indexed Alpha Detail Lookup

**Files:**
- Modify: `shared/db.py`
- Modify: `db/init.sql`
- Modify: `api/main.py`
- Create: `tests/test_alpha_symbol_detail_query.py`

- [x] Add a failing database test for a direct latest-candidate helper and composite indexes.
- [x] Run `.venv\Scripts\python.exe -m unittest tests.test_alpha_symbol_detail_query -v` and confirm failure.
- [x] Implement `fetch_latest_alpha_trade_candidate(alpha_symbol)` and use it in the endpoint.
- [x] Add `alpha_symbol, time DESC, updated_at DESC, id DESC` and `alpha_symbol, time DESC` indexes to runtime and exported schema definitions.
- [x] Run focused and full tests, restart API, and time the endpoint three times.
