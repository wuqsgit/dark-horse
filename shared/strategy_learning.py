"""Strategy learning loop helpers.

This module keeps the learning loop intentionally conservative:
reviews and factor analysis create candidates, but only candidates moved to
``active`` are exported into runtime policy files.
"""
from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone
from typing import Any

from shared.db import get_conn, init_db

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(ROOT_DIR, "configs")
ENTRY_POLICY_PATH = os.path.join(CONFIG_DIR, "entry_policy.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _canonical_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _candidate_dedupe_key(candidate: dict) -> str:
    payload = {
        "source_type": candidate.get("source_type"),
        "target": candidate.get("target"),
        "action": candidate.get("action"),
        "title": candidate.get("title"),
        "condition": candidate.get("condition") or {},
        "change": candidate.get("change") or {},
    }
    return hashlib.sha1(_canonical_dumps(payload).encode("utf-8")).hexdigest()


def ensure_learning_tables() -> None:
    init_db()


def _upsert_candidate(conn, candidate: dict) -> int | None:
    dedupe_key = _candidate_dedupe_key(candidate)
    existing = conn.execute(
        """SELECT * FROM strategy_policy_candidates
           WHERE dedupe_key = ?
           ORDER BY
             CASE status
               WHEN 'active' THEN 0
               WHEN 'shadow' THEN 1
               WHEN 'approved' THEN 2
               WHEN 'proposed' THEN 3
               ELSE 4
             END,
             id ASC
           LIMIT 1""",
        (dedupe_key,),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE strategy_policy_candidates
               SET source_run_time = ?,
                   summary = ?,
                   confidence = MAX(confidence, ?),
                   sample_size = MAX(sample_size, ?),
                   expected_delta = ?,
                   risk_note = ?,
                   rollback_condition_json = ?
               WHERE id = ?""",
            (
                candidate.get("source_run_time"),
                candidate.get("summary"),
                float(candidate.get("confidence") or 0),
                int(candidate.get("sample_size") or 0),
                float(candidate.get("expected_delta") or 0),
                candidate.get("risk_note"),
                _dumps(candidate.get("rollback_condition")),
                int(existing["id"]),
            ),
        )
        return int(existing["id"])

    conn.execute(
        """INSERT OR IGNORE INTO strategy_policy_candidates
           (source_type, source_run_time, target, action, title, summary,
            condition_json, change_json, confidence, sample_size, expected_delta,
            risk_note, status, rollback_condition_json, dedupe_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate.get("source_type"),
            candidate.get("source_run_time"),
            candidate["target"],
            candidate["action"],
            candidate["title"],
            candidate.get("summary"),
            _dumps(candidate.get("condition")),
            _dumps(candidate.get("change")),
            float(candidate.get("confidence") or 0),
            int(candidate.get("sample_size") or 0),
            float(candidate.get("expected_delta") or 0),
            candidate.get("risk_note"),
            candidate.get("status", "proposed"),
            _dumps(candidate.get("rollback_condition")),
            dedupe_key,
        ),
    )
    row = conn.execute(
        """SELECT id FROM strategy_policy_candidates
           WHERE source_type IS ?
             AND source_run_time IS ?
             AND target = ?
             AND action = ?
             AND title = ?
           ORDER BY id DESC LIMIT 1""",
        (
            candidate.get("source_type"),
            candidate.get("source_run_time"),
            candidate["target"],
            candidate["action"],
            candidate["title"],
        ),
    ).fetchone()
    return int(row["id"]) if row else None


def candidates_from_review(review: dict, run_time: str | None = None) -> list[dict]:
    run_time = run_time or review.get("_run_time") or review.get("run_time") or utc_now()
    entry_issues = review.get("entry_issues") or []
    exit_issues = review.get("exit_issues") or []
    candidates: list[dict] = []

    bad_drawdown = [x for x in entry_issues if abs(float(x.get("max_dd_pct") or 0)) >= 8]
    if len(bad_drawdown) >= 5:
        avg_dd = sum(abs(float(x.get("max_dd_pct") or 0)) for x in bad_drawdown) / len(bad_drawdown)
        candidates.append({
            "source_type": "backtest_review",
            "source_run_time": run_time,
            "target": "entry_filter",
            "action": "tighten",
            "title": "拦截高回撤追入信号",
            "summary": f"最近复盘发现 {len(bad_drawdown)} 个信号入场后先承受较大回撤，建议开仓前更严格过滤追高。",
            "condition": {
                "side": "LONG",
                "max_drawdown_pct_gte": 8,
                "price_position_contains": ["高位", "overbought"],
                "rsi_gt": 76,
                "funding_rate_gt": 0.0005,
            },
            "change": {"block_entry": True, "reason": "review_high_drawdown_chase"},
            "confidence": min(0.85, 0.45 + len(bad_drawdown) / 100),
            "sample_size": len(bad_drawdown),
            "expected_delta": round(avg_dd / 100, 4),
            "risk_note": "只影响开仓前检查；先影子验证，确认后再激活。",
            "rollback_condition": {"live_loss_streak_gte": 3, "shadow_win_rate_lt": 0.45},
        })

    early_exit = [x for x in exit_issues if float(x.get("ret_24h_pct") or 0) >= 5]
    if len(early_exit) >= 5:
        avg_left = sum(float(x.get("ret_24h_pct") or 0) for x in early_exit) / len(early_exit)
        candidates.append({
            "source_type": "backtest_review",
            "source_run_time": run_time,
            "target": "exit_policy",
            "action": "loosen",
            "title": "盈利仓不要过早全平",
            "summary": f"最近 {len(early_exit)} 个样本平仓后仍继续走强，建议把全平改为分批保护。",
            "condition": {"unrealized_profit_pct_gte": 3, "hold_alpha_still_ok": True},
            "change": {"prefer_partial_close": True, "trail_before_full_close": True},
            "confidence": min(0.82, 0.42 + len(early_exit) / 120),
            "sample_size": len(early_exit),
            "expected_delta": round(avg_left / 100, 4),
            "risk_note": "第一版只生成建议，不直接影响实盘平仓。",
            "rollback_condition": {"profit_giveback_pct_gte": 4},
        })
    return candidates


def candidates_from_factor_analysis(result: dict, run_time: str | None = None) -> list[dict]:
    run_time = run_time or result.get("run_time") or utc_now()
    candidates: list[dict] = []
    for rec in result.get("candidate_recommendations") or []:
        disc = float(rec.get("discrimination") or 0)
        if disc < 5:
            continue
        factor = str(rec.get("factor") or "").strip()
        if not factor:
            continue
        candidates.append({
            "source_type": "factor_analysis",
            "source_run_time": run_time,
            "target": "score_weight",
            "action": "increase",
            "title": f"提高 {factor} 的评分影响",
            "summary": rec.get("description") or f"{factor} 的高低分组表现差异明显，建议进入影子验证。",
            "condition": {"factor": factor, "discrimination_gte": disc},
            "change": {"factor": factor, "weight_delta_pct": 3 if disc < 10 else 5},
            "confidence": min(0.9, 0.5 + disc / 100),
            "sample_size": int(result.get("total_signals") or 0),
            "expected_delta": round(disc / 100, 4),
            "risk_note": "评分权重建议先展示，不自动覆盖 factor_weights.json。",
            "rollback_condition": {"next_discrimination_lt": 2},
        })
    return candidates


def candidates_from_factor_effectiveness(conn, run_time: str | None = None) -> list[dict]:
    run_time = run_time or utc_now()
    try:
        rows = conn.execute(
            """SELECT factor_name, layer, profile, bucket, samples, win_rate_24h,
                      avg_return_24h, avg_drawdown, ev
               FROM factor_effectiveness
               WHERE run_time = (SELECT MAX(run_time) FROM factor_effectiveness)
                 AND samples >= 20
               ORDER BY ev DESC"""
        ).fetchall()
    except Exception:
        return []
    candidates = []
    for row in rows[:12]:
        ev = float(row["ev"] or 0)
        samples = int(row["samples"] or 0)
        factor = row["factor_name"]
        layer = row["layer"]
        profile = row["profile"]
        bucket = row["bucket"]
        if ev >= 0.015:
            action = "increase"
            title = f"提高 {profile}/{layer}/{factor} 权重"
            summary = f"{factor} 的 {bucket} 桶最近样本 EV 较好，建议提高该模板下的权重。"
            delta = 0.03
        elif ev <= -0.015:
            action = "decrease"
            title = f"降低 {profile}/{layer}/{factor} 权重"
            summary = f"{factor} 的 {bucket} 桶最近样本 EV 偏差，建议降低该模板下的权重或转入风险项。"
            delta = -0.03
        else:
            continue
        candidates.append({
            "source_type": "factor_effectiveness",
            "source_run_time": run_time,
            "target": "score_weight",
            "action": action,
            "title": title,
            "summary": summary,
            "condition": {
                "profile": profile,
                "layer": layer,
                "factor": factor,
                "bucket": bucket,
                "samples_gte": samples,
            },
            "change": {
                "profile": profile,
                "layer": layer,
                "factor": factor,
                "weight_delta": delta,
            },
            "confidence": min(0.88, 0.45 + samples / 300),
            "sample_size": samples,
            "expected_delta": round(ev, 4),
            "risk_note": "仅生成调权候选；切到 active 前不会改实盘权重。",
            "rollback_condition": {"next_ev_lt": 0, "samples_gte": 20},
        })
    return candidates


def generate_policy_candidates(review: dict | None = None, factor_result: dict | None = None) -> list[int]:
    ensure_learning_tables()
    conn = get_conn()
    ids: list[int] = []
    try:
        for candidate in candidates_from_review(review or {}):
            candidate_id = _upsert_candidate(conn, candidate)
            if candidate_id:
                ids.append(candidate_id)
        for candidate in candidates_from_factor_analysis(factor_result or {}):
            candidate_id = _upsert_candidate(conn, candidate)
            if candidate_id:
                ids.append(candidate_id)
        for candidate in candidates_from_factor_effectiveness(conn):
            candidate_id = _upsert_candidate(conn, candidate)
            if candidate_id:
                ids.append(candidate_id)
        conn.commit()
        return ids
    finally:
        conn.close()


def fetch_learning_summary(limit: int = 50) -> dict:
    ensure_learning_tables()
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM strategy_policy_candidates
               ORDER BY
                 CASE status
                   WHEN 'active' THEN 0
                   WHEN 'shadow' THEN 1
                   WHEN 'approved' THEN 2
                   WHEN 'proposed' THEN 3
                   ELSE 4
                 END,
                 created_at DESC, id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        candidates = []
        for row in rows:
            item = dict(row)
            item["condition"] = _loads(item.pop("condition_json", None), {})
            item["change"] = _loads(item.pop("change_json", None), {})
            item["rollback_condition"] = _loads(item.pop("rollback_condition_json", None), {})
            candidates.append(item)
        counts = conn.execute(
            """SELECT status, COUNT(*) AS count
               FROM strategy_policy_candidates
               GROUP BY status"""
        ).fetchall()
        audits = conn.execute(
            """SELECT * FROM strategy_policy_audit
               ORDER BY created_at DESC, id DESC
               LIMIT 20"""
        ).fetchall()
        try:
            factor_effectiveness = [
                dict(r) for r in conn.execute(
                    """SELECT factor_name, layer, profile, bucket, samples,
                              win_rate_24h, avg_return_24h, ev
                       FROM factor_effectiveness
                       WHERE run_time = (SELECT MAX(run_time) FROM factor_effectiveness)
                       ORDER BY ev DESC
                       LIMIT 20"""
                ).fetchall()
            ]
        except Exception:
            factor_effectiveness = []
        active_policy = load_entry_policy()
        return {
            "generated_at": utc_now(),
            "status_counts": {r["status"]: r["count"] for r in counts},
            "candidates": candidates,
            "active_entry_policy": active_policy,
            "recent_audits": [dict(r) for r in audits],
            "factor_effectiveness": factor_effectiveness,
        }
    finally:
        conn.close()


def load_entry_policy() -> dict:
    if not os.path.exists(ENTRY_POLICY_PATH):
        return {"version": "empty", "rules": []}
    try:
        with open(ENTRY_POLICY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"version": "error", "rules": [], "error": str(exc)}


def _write_active_entry_policy(conn) -> dict:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    rows = conn.execute(
        """SELECT * FROM strategy_policy_candidates
           WHERE status = 'active' AND target = 'entry_filter'
           ORDER BY activated_at DESC, id DESC"""
    ).fetchall()
    rules = []
    for row in rows:
        change = _loads(row["change_json"], {})
        condition = _loads(row["condition_json"], {})
        rules.append({
            "id": f"candidate_{row['id']}",
            "enabled": True,
            "title": row["title"],
            "source": row["source_type"],
            "source_run_time": row["source_run_time"],
            "conditions": condition,
            "effect": change,
            "risk_note": row["risk_note"],
        })
    policy = {
        "version": utc_now(),
        "mode": "active_candidates_only",
        "rules": rules,
    }
    with open(ENTRY_POLICY_PATH, "w", encoding="utf-8") as f:
        json.dump(policy, f, ensure_ascii=False, indent=2)
    return policy


def update_candidate_status(candidate_id: int, status: str, detail: dict | None = None) -> dict:
    allowed = {"proposed", "shadow", "approved", "active", "rejected", "rolled_back"}
    if status not in allowed:
        return {"error": f"invalid status: {status}"}
    ensure_learning_tables()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM strategy_policy_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if not row:
            return {"error": "candidate not found"}
        old_status = row["status"]
        activated_at = utc_now() if status == "active" else row["activated_at"]
        conn.execute(
            """UPDATE strategy_policy_candidates
               SET status = ?, activated_at = ?
               WHERE id = ?""",
            (status, activated_at, candidate_id),
        )
        conn.execute(
            """INSERT INTO strategy_policy_audit
               (candidate_id, action, old_status, new_status, detail_json)
               VALUES (?, ?, ?, ?, ?)""",
            (candidate_id, "status_change", old_status, status, _dumps(detail or {})),
        )
        policy = _write_active_entry_policy(conn)
        conn.commit()
        return {"status": "ok", "candidate_id": candidate_id, "old_status": old_status, "new_status": status, "entry_policy": policy}
    finally:
        conn.close()
