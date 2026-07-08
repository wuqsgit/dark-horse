from __future__ import annotations

import hashlib
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.db import get_conn, init_db
from shared.strategy_learning import update_candidate_status


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
ENTRY_POLICY_PATH = CONFIG_DIR / "entry_policy.json"
EXIT_POLICY_PATH = CONFIG_DIR / "exit_policy.json"

WINDOWS = (1, 4, 12, 24, 48, 72)

BIG_MOVE_RULES = {
    "core_bluechip": {"pct": 0.04, "atr": 1.8},
    "large_cap": {"pct": 0.07, "atr": 2.0},
    "fundamental": {"pct": 0.08, "atr": 2.0},
    "narrative": {"pct": 0.12, "atr": 2.5},
    "meme": {"pct": 0.20, "atr": 3.0},
    "alpha": {"pct": 0.20, "atr": 3.0},
    "discovery": {"pct": 0.10, "atr": 2.2},
    "default": {"pct": 0.08, "atr": 2.0},
}

CATEGORY_ALIASES = {
    "bluechip": "core_bluechip",
    "bluechip_trend": "core_bluechip",
    "蓝筹": "core_bluechip",
    "large": "large_cap",
    "large_cap_alt": "large_cap",
    "alt": "narrative",
    "基本面": "fundamental",
    "叙事": "narrative",
    "叙事/庄股": "narrative",
    "other": "discovery",
    "others": "discovery",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def token_category_map() -> dict[str, str]:
    cfg = _read_json(ROOT / "strategies" / "token_profiles.json", {})
    mapping = {}
    for symbol, category in (cfg.get("token_map") or {}).items():
        base = str(symbol).upper().replace("USDT", "")
        cat = normalize_category(category)
        mapping[base] = cat
        mapping[f"{base}USDT"] = cat
    for bluechip in ("BTC", "ETH", "SOL", "BNB"):
        mapping.setdefault(bluechip, "core_bluechip")
        mapping.setdefault(f"{bluechip}USDT", "core_bluechip")
    return mapping


def normalize_category(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "discovery"
    if "meme" in raw:
        return "meme"
    if "alpha" in raw:
        return "alpha"
    if "�" in raw:
        return "discovery"
    return CATEGORY_ALIASES.get(raw, raw)


def category_for(symbol: str, features: dict | None = None, strategy_source: str | None = None) -> str:
    if strategy_source == "alpha":
        return "alpha"
    raw_cat = (features or {}).get("selection_category") or (features or {}).get("category")
    if raw_cat:
        return normalize_category(raw_cat)
    mapping = token_category_map()
    symbol_u = str(symbol or "").upper()
    return mapping.get(symbol_u) or mapping.get(symbol_u.replace("USDT", "")) or "discovery"


def action_type_from_stage(stage: str | None, result: str | None) -> str:
    stage = str(stage or "")
    result = str(result or "")
    if "close" in result or "close" in stage:
        return "close"
    if result in {"opened", "planned_open"} or stage in {"open_decision", "entry_policy"}:
        return "open"
    if result in {"blocked", "rejected", "skipped"} or stage in {"candidate_filter", "entry_policy"}:
        return "blocked"
    if stage in {"side_decision", "scan"}:
        return "selected"
    return stage or result or "scan"


def sync_decision_actions(limit: int = 5000) -> int:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT d.*
               FROM strategy_decisions d
               LEFT JOIN decision_actions a ON a.source_decision_id = d.decision_id
               WHERE d.decision_id IS NOT NULL
                 AND a.id IS NULL
               ORDER BY d.time ASC, d.id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        payload = []
        for row in rows:
            features = loads(row["features_json"], {})
            risk = loads(row["risk_params_json"], {})
            reason = loads(row["reason_json"], {})
            source = "alpha" if (features or {}).get("alpha_symbol") else "normal"
            category = category_for(row["symbol"], features, source)
            action_id = stable_id("decision", row["decision_id"], row["id"])
            payload.append(
                (
                    action_id,
                    row["decision_id"],
                    row["run_id"],
                    row["time"],
                    row["symbol"],
                    category,
                    source,
                    action_type_from_stage(row["decision_stage"], row["decision_result"]),
                    row["decision_result"],
                    row["side"],
                    row["price"] or row["entry_price"],
                    row["composite_score"],
                    (features or {}).get("entry_alpha"),
                    (features or {}).get("hold_alpha"),
                    row["grade"],
                    row["filter_reason"],
                    row["filter_reason"],
                    dumps(reason),
                    dumps(features),
                    dumps(risk),
                    dumps({}),
                    (risk or {}).get("policy_version") or (reason or {}).get("policy_version"),
                )
            )
        if payload:
            conn.executemany(
                """INSERT OR IGNORE INTO decision_actions
                   (action_id, source_decision_id, run_id, time, symbol, category,
                    strategy_source, action_type, action_result, side, price, score,
                    entry_alpha, hold_alpha, grade, reason_code, reason_text,
                    reason_json, features_json, risk_params_json, position_params_json,
                    policy_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                payload,
            )
        trade_rows = conn.execute(
            """SELECT t.*
               FROM trades t
               LEFT JOIN decision_actions a ON a.source_trade_id = t.id
               WHERE t.exit_time IS NOT NULL
                 AND a.id IS NULL
               ORDER BY t.exit_time ASC, t.id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        trade_payload = []
        for row in trade_rows:
            category = category_for(row["symbol"], {}, row["strategy_source"])
            action_id = stable_id("trade", row["id"], row["symbol"], row["exit_time"])
            entry_dt = parse_dt(row["entry_time"])
            exit_dt = parse_dt(row["exit_time"])
            holding_minutes = ((exit_dt - entry_dt).total_seconds() / 60.0) if entry_dt and exit_dt else None
            trade_payload.append(
                (
                    action_id,
                    int(row["id"]),
                    row["exit_time"] or row["created_at"],
                    row["symbol"],
                    category,
                    row["strategy_source"] or "normal",
                    "close",
                    "closed",
                    row["side"],
                    row["exit_price"] or row["entry_price"],
                    row["score_at_entry"],
                    row["grade_at_entry"],
                    row["exit_reason"],
                    row["exit_reason"],
                    dumps({
                        "entry_reason": row["entry_reason"],
                        "exit_reason": row["exit_reason"],
                        "pnl": row["pnl"],
                        "pnl_pct": row["pnl_pct"],
                        "holding_minutes": holding_minutes,
                    }),
                    dumps({}),
                    dumps({}),
                    dumps({"quantity": row["quantity"], "entry_price": row["entry_price"], "exit_price": row["exit_price"]}),
                )
            )
        if trade_payload:
            conn.executemany(
                """INSERT OR IGNORE INTO decision_actions
                   (action_id, source_trade_id, time, symbol, category, strategy_source,
                    action_type, action_result, side, price, score, grade,
                    reason_code, reason_text, reason_json, features_json,
                    risk_params_json, position_params_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                trade_payload,
            )
        conn.commit()
        return len(payload) + len(trade_payload)
    finally:
        conn.close()


def _signed_return(price: float, entry: float, side: str | None) -> float:
    raw = (price - entry) / entry
    return -raw if str(side or "").upper() == "SHORT" else raw


def _atr(candles: list[dict], entry: float) -> float | None:
    if len(candles) < 2 or entry <= 0:
        return None
    trs = []
    prev_close = float(candles[0]["close"])
    for c in candles[1:15]:
        high = float(c["high"])
        low = float(c["low"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = float(c["close"])
    if not trs:
        return None
    return statistics.mean(trs) / entry


def _category_big_move(category: str, atr_pct: float | None) -> float:
    rule = BIG_MOVE_RULES.get(category) or BIG_MOVE_RULES["default"]
    if atr_pct and atr_pct > 0:
        return max(float(rule["pct"]), float(rule["atr"]) * atr_pct)
    return float(rule["pct"])


def label_decision_outcomes(limit: int = 2500) -> int:
    sync_decision_actions(limit=limit)
    conn = get_conn()
    try:
        min_time = iso_z(datetime.now(timezone.utc) - timedelta(minutes=30))
        rows = conn.execute(
            """SELECT a.*
               FROM decision_actions a
               LEFT JOIN decision_outcomes o ON o.action_id = a.action_id
               WHERE a.price IS NOT NULL
                 AND a.price > 0
                 AND a.time <= ?
                 AND (o.id IS NULL OR o.is_complete = 0)
               ORDER BY a.time ASC, a.id ASC
               LIMIT ?""",
            (min_time, limit),
        ).fetchall()
        updates = []
        for action in rows:
            signal_dt = parse_dt(action["time"])
            if not signal_dt:
                continue
            entry = float(action["price"] or 0)
            if entry <= 0:
                continue
            end_dt = signal_dt + timedelta(hours=73)
            candles = [
                dict(c)
                for c in conn.execute(
                    """SELECT time, close, high, low
                       FROM candles_1h
                       WHERE symbol = ?
                         AND time > ?
                         AND time <= ?
                       ORDER BY time ASC""",
                    (action["symbol"], iso_z(signal_dt - timedelta(hours=2)), iso_z(end_dt)),
                ).fetchall()
            ]
            for c in candles:
                c["_time"] = parse_dt(c["time"])
            candles = [c for c in candles if c.get("_time")]
            future = [c for c in candles if c["_time"] > signal_dt]
            if not future:
                continue
            side = action["side"]
            returns = {}
            for hours in WINDOWS:
                eligible = [c for c in future if c["_time"] <= signal_dt + timedelta(hours=hours)]
                returns[hours] = _signed_return(float(eligible[-1]["close"]), entry, side) if eligible else None
            signed_highs = []
            signed_lows = []
            for c in future:
                high_ret = _signed_return(float(c["high"]), entry, side)
                low_ret = _signed_return(float(c["low"]), entry, side)
                signed_highs.append((max(high_ret, low_ret), c["time"]))
                signed_lows.append((min(high_ret, low_ret), c["time"]))
            mfe, mfe_time = max(signed_highs, key=lambda x: x[0])
            mae, mae_time = min(signed_lows, key=lambda x: x[0])
            atr_pct = _atr(candles, entry)
            big_move = _category_big_move(action["category"] or "default", atr_pct)
            action_type = action["action_type"]
            is_block = action_type == "blocked" or str(action["action_result"] or "").lower() in {"blocked", "rejected", "skipped"}
            is_close = action_type == "close"
            missed_big_move = 1 if is_block and mfe >= big_move else 0
            bad_block = missed_big_move
            good_block = 1 if is_block and mfe < big_move and (returns.get(24) or 0) <= 0 else 0
            early_exit = 1 if is_close and mfe >= max(big_move * 0.5, (atr_pct or 0) * 1.5, 0.015) and mae > -max((atr_pct or 0), 0.01) else 0
            small_profit_exit = 0
            churn_trade = 0
            probe_failed = 0
            weak_after_entry = 0
            holding_minutes = None
            if is_close:
                reason = loads(action["reason_json"], {})
                pnl_pct = reason.get("pnl_pct")
                holding_minutes = reason.get("holding_minutes")
                reason_text = str(action["reason_text"] or reason.get("exit_reason") or "")
                entry_reason = str(reason.get("entry_reason") or "")
                if pnl_pct is not None:
                    pnl_pct_f = float(pnl_pct)
                    small_profit_exit = 1 if 0 < pnl_pct_f < 2.0 and early_exit else 0
                    churn_trade = 1 if pnl_pct_f <= 0 and abs(pnl_pct_f) <= 1.5 and (holding_minutes is None or float(holding_minutes) <= 90) else 0
                    probe_failed = 1 if "probe" in entry_reason.lower() and pnl_pct_f <= 0 else 0
                weak_terms = (
                    "orderbook_depth_weak", "orderbook_robot_signature",
                    "alpha_long_momentum_reversal", "alpha_volume_price_weak",
                    "alpha_trend_score_fade", "alpha_volume_regime_bad",
                    "alpha_spread_widened",
                )
                weak_after_entry = 1 if any(term in reason_text for term in weak_terms) else 0
            trend_capture = None
            if is_close:
                reason = loads(action["reason_json"], {})
                pnl_pct = reason.get("pnl_pct")
                if pnl_pct is not None and mfe > 0:
                    trend_capture = max(0.0, min(1.5, (float(pnl_pct) / 100.0) / mfe))
            updates.append(
                (
                    action["action_id"], action["symbol"], action["category"], action_type,
                    action["action_result"], action["time"], entry, side,
                    returns.get(1), returns.get(4), returns.get(12), returns.get(24),
                    returns.get(48), returns.get(72), mfe, mae, mfe_time, mae_time,
                    atr_pct, (mfe / atr_pct if atr_pct else None),
                    (abs(mae) / atr_pct if atr_pct else None), missed_big_move,
                    early_exit, good_block, bad_block, small_profit_exit,
                    churn_trade, probe_failed, weak_after_entry, holding_minutes,
                    trend_capture, len(future),
                    1 if max(c["_time"] for c in future) >= signal_dt + timedelta(hours=72) else 0,
                )
            )
        if updates:
            conn.executemany(
                """INSERT INTO decision_outcomes
                   (action_id, symbol, category, action_type, action_result,
                    signal_time, entry_price, side, return_1h, return_4h,
                    return_12h, return_24h, return_48h, return_72h,
                    max_favorable_return, max_adverse_return, max_favorable_time,
                    max_adverse_time, atr_at_signal, mfe_atr_multiple,
                    mae_atr_multiple, missed_big_move, early_exit, good_block,
                    bad_block, small_profit_exit, churn_trade, probe_failed,
                    weak_after_entry, holding_minutes, trend_capture_ratio,
                    bars_observed, is_complete, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(action_id) DO UPDATE SET
                    return_1h=excluded.return_1h,
                    return_4h=excluded.return_4h,
                    return_12h=excluded.return_12h,
                    return_24h=excluded.return_24h,
                    return_48h=excluded.return_48h,
                    return_72h=excluded.return_72h,
                    max_favorable_return=excluded.max_favorable_return,
                    max_adverse_return=excluded.max_adverse_return,
                    max_favorable_time=excluded.max_favorable_time,
                    max_adverse_time=excluded.max_adverse_time,
                    atr_at_signal=excluded.atr_at_signal,
                    mfe_atr_multiple=excluded.mfe_atr_multiple,
                    mae_atr_multiple=excluded.mae_atr_multiple,
                    missed_big_move=excluded.missed_big_move,
                    early_exit=excluded.early_exit,
                    good_block=excluded.good_block,
                    bad_block=excluded.bad_block,
                    small_profit_exit=excluded.small_profit_exit,
                    churn_trade=excluded.churn_trade,
                    probe_failed=excluded.probe_failed,
                    weak_after_entry=excluded.weak_after_entry,
                    holding_minutes=excluded.holding_minutes,
                    trend_capture_ratio=excluded.trend_capture_ratio,
                    bars_observed=excluded.bars_observed,
                    is_complete=excluded.is_complete,
                    updated_at=datetime('now')""",
                updates,
            )
        conn.commit()
        return len(updates)
    finally:
        conn.close()


def _mean(values: list[float | None]) -> float:
    usable = [float(v) for v in values if v is not None]
    return sum(usable) / len(usable) if usable else 0.0


def _median(values: list[float | None]) -> float:
    usable = [float(v) for v in values if v is not None]
    return statistics.median(usable) if usable else 0.0


def run_policy_review(days: int = 14) -> dict:
    label_decision_outcomes()
    conn = get_conn()
    run_time = utc_now()
    cutoff = iso_z(datetime.now(timezone.utc) - timedelta(days=days))
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT o.*, a.reason_code, a.reason_text, a.strategy_source, a.score
                   FROM decision_outcomes o
                   JOIN decision_actions a ON a.action_id = o.action_id
                   WHERE o.signal_time >= ?
                   ORDER BY o.signal_time DESC""",
                (cutoff,),
            ).fetchall()
        ]
        groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
        for r in rows:
            category = r.get("category") or "discovery"
            source = r.get("strategy_source") or "normal"
            if r.get("action_type") == "blocked":
                groups[(category, source, "entry_filter", r.get("reason_code") or "unknown")].append(r)
            elif r.get("action_type") == "close":
                groups[(category, source, "exit", r.get("reason_code") or "close")].append(r)
            else:
                groups[(category, source, "scoring", "score_bucket")].append(r)
        review_rows = []
        for (category, source, target_type, target_name), items in groups.items():
            sample_size = len(items)
            if sample_size < 3:
                continue
            returns = [x.get("return_24h") for x in items]
            avg_return = _mean(returns)
            median_return = _median(returns)
            avg_mfe = _mean([x.get("max_favorable_return") for x in items])
            avg_mae = _mean([x.get("max_adverse_return") for x in items])
            capture = _mean([x.get("trend_capture_ratio") for x in items])
            missed = sum(int(x.get("missed_big_move") or 0) for x in items)
            early = sum(int(x.get("early_exit") or 0) for x in items)
            small = sum(int(x.get("small_profit_exit") or 0) for x in items)
            bad = sum(int(x.get("bad_block") or 0) for x in items)
            good = sum(int(x.get("good_block") or 0) for x in items)
            diagnosis = "正常"
            recommendation = {"action": "keep"}
            if target_type == "entry_filter" and bad >= max(3, sample_size * 0.25):
                diagnosis = "该拦截条件反复挡住后续大波段"
                recommendation = {
                    "action": "loosen_or_soften",
                    "reason": "bad_block_rate_high",
                    "bad_block_rate": bad / sample_size,
                    "missed_big_move_count": missed,
                }
            elif target_type == "exit" and early >= max(2, sample_size * 0.25):
                diagnosis = "该平仓原因存在过早退出"
                recommendation = {
                    "action": "loosen_exit",
                    "reason": "early_exit_rate_high",
                    "early_exit_rate": early / sample_size,
                    "small_profit_exit_count": small,
                }
            elif target_type == "scoring" and avg_mfe >= _category_big_move(category, None):
                diagnosis = "该分类信号存在可捕捉波段"
                recommendation = {"action": "keep_or_increase_sizing", "reason": "mfe_positive"}
            review_id = stable_id("review", run_time, category, source, target_type, target_name)
            review_rows.append(
                (
                    review_id, run_time, category, source, target_type, target_name,
                    sample_size, avg_return, median_return, sum(float(v or 0) for v in returns),
                    avg_mfe, avg_mae, capture, missed, early, small, bad, good,
                    diagnosis, dumps(recommendation),
                )
            )
        if review_rows:
            conn.executemany(
                """INSERT OR REPLACE INTO policy_reviews
                   (review_id, run_time, category, strategy_source, target_type,
                    target_name, sample_size, avg_return, median_return, total_return,
                    avg_mfe, avg_mae, trend_capture_ratio, missed_big_move_count,
                    early_exit_count, small_profit_exit_count, bad_block_count,
                    good_block_count, diagnosis, recommendation_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                review_rows,
            )
        conn.commit()
        return {"run_time": run_time, "reviews": len(review_rows), "samples": len(rows)}
    finally:
        conn.close()


def _candidate_payload(row: dict) -> dict | None:
    target_type = row["target_type"]
    category = row["category"] or "discovery"
    target_name = row["target_name"] or "unknown"
    sample = int(row["sample_size"] or 0)
    if target_type == "entry_filter" and int(row["bad_block_count"] or 0) >= max(3, sample * 0.25):
        return {
            "source_type": "policy_review",
            "source_run_time": row["run_time"],
            "target": "entry_filter",
            "action": "soften",
            "title": f"{category}: 放宽误杀拦截 {target_name}",
            "summary": f"{target_name} 在 {category} 中 {row['bad_block_count']} 次挡住后续大波段，自动改为软拦截/降仓提示。",
            "condition": {"category": category, "reason_code": target_name, "bad_block_rate_gte": 0.25},
            "change": {"block_entry": False, "size_multiplier": 0.7, "reason": f"auto_softened_{target_name}"},
            "confidence": min(0.9, 0.45 + sample / 100),
            "sample_size": sample,
            "expected_delta": float(row["avg_mfe"] or 0),
            "risk_note": "自动生效: 硬拦截转软拦截，不突破全局仓位风控。",
            "rollback_condition": {"missed_big_move_rate_increase": 0.2, "after_return_lt_before": True},
        }
    if target_type == "exit" and int(row["early_exit_count"] or 0) >= max(2, sample * 0.25):
        return {
            "source_type": "policy_review",
            "source_run_time": row["run_time"],
            "target": "exit_policy",
            "action": "loosen",
            "title": f"{category}: 减少过早平仓 {target_name[:32]}",
            "summary": f"{category} 的 {target_name} 出现 {row['early_exit_count']} 次过早平仓，自动提高小盈利全平门槛并倾向分批止盈。",
            "condition": {"category": category, "exit_reason": target_name, "early_exit_rate_gte": 0.25},
            "change": {"category": category, "small_profit_pct_delta": 1.0, "allow_full_close_on_small_profit": False},
            "confidence": min(0.88, 0.45 + sample / 80),
            "sample_size": sample,
            "expected_delta": float(row["avg_mfe"] or 0),
            "risk_note": "自动生效: 放宽退出只影响分类退出参数，止损和硬风控不取消。",
            "rollback_condition": {"after_early_exit_rate_gte": 0.4, "after_return_lt_before": True},
        }
    if target_type == "scoring" and float(row["avg_mfe"] or 0) > 0.08 and sample >= 15:
        return {
            "source_type": "policy_review",
            "source_run_time": row["run_time"],
            "target": "position_sizing",
            "action": "increase",
            "title": f"{category}: 信号有波段空间，略提高 confirmed/strong 仓位",
            "summary": f"{category} 最近最大浮盈均值较高，自动小幅提高强信号目标保证金，但仍受全局上限限制。",
            "condition": {"category": category, "avg_mfe_gte": row["avg_mfe"]},
            "change": {"category": category, "confirmed_margin_delta": 0.01, "strong_margin_delta": 0.015},
            "confidence": min(0.8, 0.40 + sample / 120),
            "sample_size": sample,
            "expected_delta": float(row["avg_mfe"] or 0),
            "risk_note": "自动生效: 小幅仓位建议写入版本表，执行侧仍受 trader/config.py 上限保护。",
            "rollback_condition": {"after_return_lt_before": True},
        }
    return None


def _upsert_candidate(conn, candidate: dict) -> int | None:
    key = stable_id(
        "autocand",
        candidate.get("source_type"),
        candidate.get("target"),
        candidate.get("action"),
        candidate.get("title"),
        dumps(candidate.get("condition")),
    )
    row = conn.execute("SELECT id, status FROM strategy_policy_candidates WHERE dedupe_key = ?", (key,)).fetchone()
    if row:
        conn.execute(
            """UPDATE strategy_policy_candidates
               SET source_run_time = ?, summary = ?, confidence = MAX(confidence, ?),
                   sample_size = MAX(sample_size, ?), expected_delta = ?,
                   risk_note = ?, rollback_condition_json = ?
               WHERE id = ?""",
            (
                candidate.get("source_run_time"), candidate.get("summary"),
                float(candidate.get("confidence") or 0), int(candidate.get("sample_size") or 0),
                float(candidate.get("expected_delta") or 0), candidate.get("risk_note"),
                dumps(candidate.get("rollback_condition")), row["id"],
            ),
        )
        return int(row["id"])
    conn.execute(
        """INSERT INTO strategy_policy_candidates
           (source_type, source_run_time, target, action, title, summary,
            condition_json, change_json, confidence, sample_size, expected_delta,
            risk_note, status, rollback_condition_json, dedupe_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)""",
        (
            candidate.get("source_type"), candidate.get("source_run_time"),
            candidate["target"], candidate["action"], candidate["title"],
            candidate.get("summary"), dumps(candidate.get("condition")),
            dumps(candidate.get("change")), float(candidate.get("confidence") or 0),
            int(candidate.get("sample_size") or 0), float(candidate.get("expected_delta") or 0),
            candidate.get("risk_note"), dumps(candidate.get("rollback_condition")), key,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _merge_exit_policy(candidate: dict) -> dict:
    policy = _read_json(EXIT_POLICY_PATH, {"version": "auto-empty", "default_class": "narrative", "classes": {}})
    change = candidate.get("change") or {}
    category = change.get("category") or (candidate.get("condition") or {}).get("category") or "narrative"
    classes = policy.setdefault("classes", {})
    current = classes.setdefault(category, {})
    current["small_profit_pct"] = round(float(current.get("small_profit_pct", 2.0)) + float(change.get("small_profit_pct_delta", 0.5)), 2)
    if "allow_full_close_on_small_profit" in change:
        current["allow_full_close_on_small_profit"] = bool(change["allow_full_close_on_small_profit"])
    current.setdefault("partial_close_pct", 0.35 if category in {"core_bluechip", "large_cap"} else 0.5)
    current.setdefault("confirm_rounds_for_full_close", 2)
    policy["version"] = f"auto-{utc_now()}"
    _write_json(EXIT_POLICY_PATH, policy)
    return policy


def _merge_entry_policy(conn) -> dict:
    rows = conn.execute(
        """SELECT * FROM strategy_policy_candidates
           WHERE status = 'active'
             AND target = 'entry_filter'
           ORDER BY activated_at DESC, id DESC"""
    ).fetchall()
    rules = []
    for r in rows:
        rules.append(
            {
                "id": f"auto_candidate_{r['id']}",
                "enabled": True,
                "title": r["title"],
                "source": r["source_type"],
                "source_run_time": r["source_run_time"],
                "conditions": loads(r["condition_json"], {}),
                "effect": loads(r["change_json"], {}),
                "risk_note": r["risk_note"],
            }
        )
    policy = {"version": f"auto-{utc_now()}", "mode": "auto_active_candidates", "rules": rules}
    _write_json(ENTRY_POLICY_PATH, policy)
    return policy


def _record_policy_version(conn, candidate_id: int, candidate: dict, policy: dict) -> str:
    version_id = stable_id("policy", candidate_id, utc_now())
    category = (candidate.get("condition") or {}).get("category") or (candidate.get("change") or {}).get("category")
    target = candidate.get("target")
    conn.execute(
        """INSERT OR IGNORE INTO policy_versions
           (version_id, category, strategy_source, target_type, policy_json,
            source_candidate_id, status, activated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
        (version_id, category, "normal", target, dumps(policy), candidate_id, utc_now()),
    )
    return version_id


def generate_and_activate_policies() -> dict:
    review = run_policy_review()
    conn = get_conn()
    activated = []
    created = 0
    try:
        latest = conn.execute("SELECT MAX(run_time) AS run_time FROM policy_reviews").fetchone()["run_time"]
        rows = [dict(r) for r in conn.execute("SELECT * FROM policy_reviews WHERE run_time = ?", (latest,)).fetchall()]
        for row in rows:
            candidate = _candidate_payload(row)
            if not candidate:
                continue
            candidate_id = _upsert_candidate(conn, candidate)
            if not candidate_id:
                continue
            created += 1
            if int(candidate.get("sample_size") or 0) < 3:
                continue
            update_candidate_status(candidate_id, "active", {"source": "auto_policy_loop", "auto": True})
            conn2 = get_conn()
            try:
                crow = conn2.execute("SELECT * FROM strategy_policy_candidates WHERE id = ?", (candidate_id,)).fetchone()
                if not crow:
                    continue
                candidate_for_policy = {
                    "target": crow["target"],
                    "condition": loads(crow["condition_json"], {}),
                    "change": loads(crow["change_json"], {}),
                }
                if crow["target"] == "exit_policy":
                    policy = _merge_exit_policy(candidate_for_policy)
                else:
                    policy = _merge_entry_policy(conn2)
                version_id = _record_policy_version(conn2, candidate_id, candidate_for_policy, policy)
                conn2.commit()
                activated.append({"candidate_id": candidate_id, "target": crow["target"], "version_id": version_id})
            finally:
                conn2.close()
        conn.commit()
        return {"created": created, "activated": activated, "review": review}
    finally:
        conn.close()


def policy_guard() -> dict:
    conn = get_conn()
    checked = 0
    rolled_back = 0
    try:
        versions = conn.execute(
            """SELECT * FROM policy_versions
               WHERE status = 'active'
               ORDER BY activated_at DESC
               LIMIT 20"""
        ).fetchall()
        for version in versions:
            checked += 1
            category = version["category"]
            if not category:
                continue
            recent = conn.execute(
                """SELECT AVG(return_24h) AS avg_return,
                          AVG(early_exit) AS early_exit_rate,
                          AVG(missed_big_move) AS missed_rate,
                          COUNT(*) AS samples
                   FROM decision_outcomes
                   WHERE category = ?
                     AND signal_time >= ?""",
                (category, version["activated_at"] or utc_now()),
            ).fetchone()
            samples = int(recent["samples"] or 0)
            if samples < 8:
                continue
            avg_return = float(recent["avg_return"] or 0)
            early_rate = float(recent["early_exit_rate"] or 0)
            missed_rate = float(recent["missed_rate"] or 0)
            if avg_return < -0.02 or early_rate > 0.55 or missed_rate > 0.55:
                conn.execute("UPDATE policy_versions SET status = 'rolled_back' WHERE id = ?", (version["id"],))
                if version["source_candidate_id"]:
                    conn.execute(
                        """UPDATE strategy_policy_candidates
                           SET status = 'rolled_back'
                           WHERE id = ?""",
                        (version["source_candidate_id"],),
                    )
                rolled_back += 1
        if rolled_back:
            _merge_entry_policy(conn)
        conn.commit()
        return {"checked": checked, "rolled_back": rolled_back}
    finally:
        conn.close()


def fetch_policy_loop_summary(limit: int = 200) -> dict:
    conn = get_conn()
    try:
        latest_review = conn.execute("SELECT MAX(run_time) AS run_time FROM policy_reviews").fetchone()["run_time"]
        overview = conn.execute(
            """SELECT COUNT(*) AS samples,
                      AVG(return_24h) AS avg_return_24h,
                      AVG(return_72h) AS avg_return_72h,
                      AVG(max_favorable_return) AS avg_mfe,
                      AVG(max_adverse_return) AS avg_mae,
                      AVG(trend_capture_ratio) AS trend_capture_ratio,
                      SUM(missed_big_move) AS missed_big_move_count,
                      SUM(early_exit) AS early_exit_count,
                      SUM(small_profit_exit) AS small_profit_exit_count,
                      SUM(bad_block) AS bad_block_count,
                      SUM(good_block) AS good_block_count
               FROM decision_outcomes"""
        ).fetchone()
        category_rows = [
            dict(r)
            for r in conn.execute(
                """SELECT category, COUNT(*) AS samples,
                          AVG(return_24h) AS avg_return_24h,
                          AVG(return_72h) AS avg_return_72h,
                          AVG(max_favorable_return) AS avg_mfe,
                          AVG(max_adverse_return) AS avg_mae,
                          AVG(trend_capture_ratio) AS trend_capture_ratio,
                          SUM(missed_big_move) AS missed_big_move_count,
                          SUM(early_exit) AS early_exit_count,
                          SUM(bad_block) AS bad_block_count
                   FROM decision_outcomes
                   GROUP BY category
                   ORDER BY samples DESC"""
            ).fetchall()
        ]
        reviews = [
            {**dict(r), "recommendation": loads(r["recommendation_json"], {})}
            for r in conn.execute(
                """SELECT * FROM policy_reviews
                   ORDER BY run_time DESC, sample_size DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        candidates = [
            {
                **dict(r),
                "condition": loads(r["condition_json"], {}),
                "change": loads(r["change_json"], {}),
                "rollback_condition": loads(r["rollback_condition_json"], {}),
            }
            for r in conn.execute(
                """SELECT * FROM strategy_policy_candidates
                   WHERE source_type = 'policy_review'
                   ORDER BY
                     CASE status WHEN 'active' THEN 0 WHEN 'proposed' THEN 1 ELSE 2 END,
                     created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        versions = [
            {**dict(r), "policy": loads(r["policy_json"], {})}
            for r in conn.execute(
                """SELECT * FROM policy_versions
                   ORDER BY created_at DESC
                   LIMIT 50"""
            ).fetchall()
        ]
        actions = [
            {**dict(r)}
            for r in conn.execute(
                """SELECT a.*, o.return_24h, o.return_72h, o.max_favorable_return,
                          o.max_adverse_return, o.missed_big_move, o.early_exit,
                          o.good_block, o.bad_block, o.small_profit_exit,
                          o.trend_capture_ratio
                   FROM decision_actions a
                   LEFT JOIN decision_outcomes o ON o.action_id = a.action_id
                   ORDER BY a.time DESC, a.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        return {
            "generated_at": utc_now(),
            "latest_review_time": latest_review,
            "overview": dict(overview) if overview else {},
            "categories": category_rows,
            "reviews": reviews,
            "candidates": candidates,
            "versions": versions,
            "actions": actions,
            "entry_policy": _read_json(ENTRY_POLICY_PATH, {}),
            "exit_policy": _read_json(EXIT_POLICY_PATH, {}),
        }
    finally:
        conn.close()


def clear_legacy_backtest_data(vacuum: bool = False) -> dict:
    conn = get_conn()
    tables = ["backtest_results", "backtest_summary_cache", "backtest_review", "factor_performance", "factor_analysis"]
    counts = {}
    try:
        for table in tables:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                counts[table] = None
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
        return {"cleared": counts, "vacuum": vacuum}
    finally:
        conn.close()
