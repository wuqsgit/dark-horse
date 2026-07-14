# AI Entry Quality Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and connect an independent, observable AI entry-quality service that directly gates ready-model entries and safely collects data while models are not ready.

**Architecture:** A FastAPI service owns its SQLite sample/decision store and XGBoost artifacts. Trader sends canonical planned-entry snapshots over a 300ms HTTP call before execution; the main API proxies status and decisions to the frontend.

**Tech Stack:** Python 3, FastAPI, SQLite, XGBoost, httpx, unittest, React/Vite.

## Global Constraints

- Service port is 8010.
- AI service has no exchange credentials and no order execution code.
- Transport failure or expired ready model blocks new entries but never blocks position management.
- Healthy collecting state preserves existing entry behavior.
- Quality >=62 allows, 55 to <62 probes at 5% margin, and <55 rejects.
- Labels use the first +1R/-1R event within 24 hours; no +1R is negative.
- Model publication requires 300 labeled samples, a 60-sample validation slice, and improved allowed-group average R.

---

### Task 1: AI Storage and Labels

**Files:**
- Create: `ai_service/__init__.py`
- Create: `ai_service/config.py`
- Create: `ai_service/storage.py`
- Create: `ai_service/labels.py`
- Create: `tests/test_ai_quality_storage.py`
- Create: `tests/test_ai_quality_labels.py`

**Interfaces:**
- Produces: `AIStore`, `label_path(entry, stop_pct, side, candles)`, sample and decision records.

- [ ] Write failing tests for hourly deduplication, counters, and conservative +1R/-1R labels.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement the SQLite schema and label calculation.
- [ ] Run focused tests until green.

### Task 2: Model Lifecycle and Service API

**Files:**
- Create: `ai_service/features.py`
- Create: `ai_service/model.py`
- Create: `ai_service/service.py`
- Create: `ai_service/main.py`
- Create: `ai_service/requirements.txt`
- Create: `tests/test_ai_quality_service.py`

**Interfaces:**
- Consumes: `AIStore` and canonical candidate payloads.
- Produces: evaluate, train, label, status, and decisions endpoints.

- [ ] Write failing tests for collecting, allow/probe/reject thresholds, expiry, and publish guards.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement feature extraction, model backend, lifecycle service, and FastAPI endpoints.
- [ ] Run focused tests until green.

### Task 3: Trader Fail-Closed Integration

**Files:**
- Create: `trader/ai_client.py`
- Modify: `trader/runner.py`
- Modify: `trader/execution.py`
- Create: `tests/test_ai_entry_gate.py`

**Interfaces:**
- Consumes: planned open actions and latest scan features.
- Produces: filtered or resized open actions; non-open actions pass through unchanged.

- [ ] Write failing tests for collecting fallback, allow, exact 5% probe margin, reject, and transport fail-closed behavior.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement canonical candidate building and gate planned opens before execution.
- [ ] Run focused tests until green.

### Task 4: API Proxy, Frontend Status, and Startup

**Files:**
- Modify: `api/main.py`
- Modify: `api/requirements.txt`
- Modify: `start.sh`
- Create: `frontend/src/components/AITradingStatus.jsx`
- Create: `frontend/src/components/aiQuality.js`
- Create: `frontend/src/components/aiQuality.test.mjs`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/components/ScanTable.jsx`
- Modify: `frontend/src/components/AlphaScan.jsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: AI service status and decisions endpoints.
- Produces: visible runtime state and per-symbol AI results.

- [ ] Write failing frontend helper tests and backend proxy tests.
- [ ] Run tests and confirm expected failures.
- [ ] Implement proxy endpoints, status component, decision badges, and startup entry.
- [ ] Run frontend tests and production build.

### Task 5: End-to-End Verification

**Files:**
- Modify only where verification identifies a scoped compatibility issue.

- [ ] Install AI service dependencies in the workspace virtual environment.
- [ ] Run all backend unit tests.
- [ ] Run all frontend tests and the Vite production build.
- [ ] Start AI service and verify status, collecting evaluation, decisions, and API proxy endpoints.
- [ ] Keep Trader stopped unless explicitly requested, so the previously flattened account cannot reopen positions unexpectedly.
