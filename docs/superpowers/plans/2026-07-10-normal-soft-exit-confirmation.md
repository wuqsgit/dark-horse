# Normal Soft Exit Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trend confirmation and a 60-minute cooldown to normal soft reductions.

**Architecture:** Pure helpers classify trend state and query recent planned soft reductions. `_build_position_actions` routes weak-hold and score-decay actions through those helpers while hard actions remain earlier in the decision chain.

**Tech Stack:** Python, SQLite, unittest.

## Global Constraints

- Strong-trend weak-hold reduction is 20%.
- Confirmed-weak soft reduction is 25%.
- Cooldown is 60 minutes per symbol across normal soft-exit reasons.
- Hard stops, trailing stops, TP1/TP2, residual cleanup, and Alpha exits are unchanged.

---

### Task 1: Soft Exit Policy And Cooldown

**Files:**
- Modify: `trader/config.py`
- Modify: `trader/execution.py`
- Create: `tests/test_normal_soft_exit.py`

**Interfaces:**
- Produces `_normal_soft_trend_state(side, mark_price, tech) -> str` with `strong`, `weak`, or `ambiguous`.
- Produces `_normal_soft_exit_in_cooldown(symbol, minutes) -> bool`.

- [ ] Add failing tests for strong-trend 20% reduction, cooldown hold, ambiguous hold, confirmed-weak 25% reduction, and hard-stop bypass.
- [ ] Run `.venv\\Scripts\\python.exe -m unittest tests.test_normal_soft_exit -v` and confirm failures.
- [ ] Implement the trend classifier and recent-decision cooldown query.
- [ ] Route weak-hold, score-decay, and category momentum reductions through the policy.
- [ ] Run focused tests and full unittest discovery.
- [ ] Compile changed Python, run `git diff --check`, restart Trader, and inspect logs.
