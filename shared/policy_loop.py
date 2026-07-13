from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.db import fetch_entry_reviews, get_conn, init_db, record_entry_review_snapshot
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

EXIT_REVIEW_THRESHOLDS = {
    "alpha": {"early": 0.06, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.05},
    "core_bluechip": {"early": 0.03, "noise_mfe": 0.02, "noise_loss": -0.015, "small_profit": 0.015, "small_mfe": 0.025},
    "large_cap": {"early": 0.05, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.04},
    "fundamental": {"early": 0.05, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.04},
    "narrative": {"early": 0.05, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.04},
    "meme": {"early": 0.08, "noise_mfe": 0.05, "noise_loss": -0.03, "small_profit": 0.03, "small_mfe": 0.06},
    "discovery": {"early": 0.05, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.04},
    "default": {"early": 0.05, "noise_mfe": 0.03, "noise_loss": -0.02, "small_profit": 0.02, "small_mfe": 0.04},
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
        conn.execute(
            "DELETE FROM trade_entry_reviews WHERE COALESCE(entry_price, 0) <= 0 OR COALESCE(side, '') = ''"
        )
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


def classify_entry_review(
    risk_pct: float | None,
    returns: list[float],
    overheated: bool,
    is_closed: bool = False,
) -> dict:
    """Classify entry timing from the ordered path in initial-risk units."""
    if not risk_pct or risk_pct <= 0 or not returns:
        return {"label": "pending", "reason": "缺少可靠初始止损或后续K线"}
    first_one_r = next((i for i, value in enumerate(returns) if value >= risk_pct), None)
    first_minus = next((i for i, value in enumerate(returns) if value <= -0.75 * risk_pct), None)
    max_favorable = max(returns)
    min_adverse = min(returns)
    if first_one_r is not None and (first_minus is None or first_one_r < first_minus):
        return {"label": "reasonable", "reason": "先达到+1R，入场后走势直接兑现"}
    if first_minus is not None and first_one_r is not None and first_minus < first_one_r:
        return {"label": "early", "reason": "先回撤-0.75R，随后仍达到+1R"}
    if first_minus is not None and max_favorable < 0.5 * risk_pct and overheated:
        return {"label": "chased", "reason": "入场后先明显回撤且未达到+0.5R，开仓价格偏热"}
    if is_closed and min_adverse <= -risk_pct and max_favorable < 0.5 * risk_pct:
        return {"label": "bad_condition", "reason": "达到-1R且全程未达到+0.5R"}
    return {"label": "pending", "reason": "走势尚未形成明确结论"}


def build_entry_group_recommendation(stats: dict) -> dict:
    sample = int(stats.get("sample_size") or 0)
    if sample < 4:
        return {"action_type": "observe", "recommendation": "样本不足，继续观察，不调整入场规则。"}
    if sample < 8:
        return {"action_type": "broad", "recommendation": "样本仍少，可把入场范围放宽为等待回踩0至0.5 ATR再确认。"}
    problem_counts = {
        "early": int(stats.get("early_count") or 0),
        "chased": int(stats.get("chased_count") or 0),
        "bad_condition": int(stats.get("bad_condition_count") or 0),
    }
    problem, count = max(problem_counts.items(), key=lambda item: item[1])
    if count / sample < 0.60:
        return {"action_type": "keep", "recommendation": "问题分布不集中，保持现有入场条件并继续观察。"}
    if problem == "early":
        text = "偏早样本集中，增加1根确认K线，或等待回踩不破后再开仓。"
    elif problem == "chased":
        text = "追高样本集中，限制价格偏离，突破后等待回踩或二次放量。"
    else:
        text = "条件错误样本集中，提高趋势与现货/合约同步放量要求。"
    return {"action_type": "improve", "recommendation": text}


def _first_value(*values):
    return next((value for value in values if value is not None and value != ""), None)


def _entry_reason_sentence(snapshot: dict) -> str:
    symbol = snapshot.get("symbol") or "-"
    side = "做空" if str(snapshot.get("side") or "").upper() == "SHORT" else "做多"
    source = "Alpha" if snapshot.get("strategy_source") == "alpha" else "普通币"
    template = snapshot.get("entry_template") or "未记录模板"
    score = snapshot.get("total_score")
    parts = [f"{symbol} {side}：{source} {template}"]
    if score is not None:
        parts.append(f"评分{float(score):.1f}")
    evidence = []
    if snapshot.get("trend_score") is not None:
        evidence.append(f"趋势分{float(snapshot['trend_score']):.1f}")
    if snapshot.get("volume_sync_state"):
        evidence.append(str(snapshot["volume_sync_state"]))
    if snapshot.get("breakout_state"):
        evidence.append(str(snapshot["breakout_state"]))
    if evidence:
        parts.append("、".join(evidence[:3]))
    risk = []
    if snapshot.get("leverage") is not None:
        risk.append(f"{float(snapshot['leverage']):g}倍杠杆")
    if snapshot.get("margin") is not None:
        risk.append(f"{float(snapshot['margin']):.2f}U保证金")
    if snapshot.get("stop_pct") is not None:
        risk.append(f"初始止损{float(snapshot['stop_pct']) * 100:.2f}%")
    if risk:
        parts.append("、".join(risk))
    if len(parts) == 1:
        return f"{parts[0]}；该历史仓位仅保留成交事实，开仓指标未记录。"
    return "；".join(parts) + "，因此开仓。"


def build_execution_entry_snapshot(action: dict) -> dict:
    entry = float(action.get("entry_price") or 0)
    quantity = float(action.get("quantity") or 0)
    stop = action.get("stop_loss")
    stop_pct = action.get("stop_pct")
    if stop_pct is None and stop is not None and entry > 0:
        stop_pct = abs(entry - float(stop)) / entry
    source = action.get("strategy_source") or "normal"
    snapshot = {
        "position_trade_id": action.get("position_id"), "source_decision_id": action.get("decision_id"),
        "symbol": action.get("symbol"), "alpha_symbol": action.get("alpha_symbol"),
        "side": action.get("position_side") or action.get("side"), "strategy_source": source,
        "category": action.get("category") or ("alpha" if source == "alpha" else None),
        "entry_template": _first_value(action.get("entry_template"), action.get("alpha_profile"), action.get("signal_source")),
        "market_regime": action.get("market_regime"), "entry_time": action.get("entry_time") or utc_now(),
        "entry_price": entry, "quantity": quantity, "leverage": action.get("leverage"),
        "margin": action.get("margin"), "notional": action.get("notional") or (entry * quantity if entry else None),
        "stop_loss": stop, "stop_pct": stop_pct, "take_profit_1": action.get("tp1_price"),
        "take_profit_2": action.get("tp2_price"), "risk_reward_ratio": action.get("risk_reward_ratio"),
        "atr_pct": action.get("atr_pct"), "total_score": _first_value(action.get("score"), action.get("alpha_score")),
        "grade": action.get("grade"), "score_items_json": action.get("score_items"),
        "trend_score": action.get("trend_score"), "breakout_state": action.get("breakout_state"),
        "spot_volume_ratio": action.get("spot_volume_ratio"), "futures_volume_ratio": action.get("futures_volume_ratio"),
        "volume_sync_state": action.get("volume_sync_state"), "spread_pct": action.get("spread_pct"),
        "orderbook_state": action.get("orderbook_state"), "passed_conditions_json": action.get("passed_conditions"),
        "relaxed_conditions_json": action.get("relaxed_conditions"), "features_json": action.get("features") or {},
        "risk_params_json": {key: action.get(key) for key in ("leverage", "margin", "stop_loss", "stop_pct", "tp1_price", "tp2_price")},
        "reason_json": {"reason": action.get("reason")}, "snapshot_source": "live_execution", "position_status": "open",
    }
    snapshot["entry_reason_text"] = _entry_reason_sentence(snapshot)
    return snapshot


def _snapshot_from_trade(conn, trade) -> dict:
    entry_time = trade["entry_time"]
    decision = conn.execute(
        """SELECT * FROM strategy_decisions
           WHERE symbol=? AND datetime(time) <= datetime(?)
             AND datetime(time) >= datetime(?, '-15 minutes')
             AND (decision_result IN ('opened','planned_open') OR decision_stage IN ('open_decision','entry_policy'))
           ORDER BY datetime(time) DESC, id DESC LIMIT 1""",
        (trade["symbol"], entry_time, entry_time),
    ).fetchone() if entry_time else None
    entry = float(trade["entry_price"] or 0)
    quantity = float(trade["quantity"] or 0)
    order_side = "SELL" if str(trade["side"] or "").upper() == "SHORT" else "BUY"
    order = None
    if not decision and entry > 0 and quantity > 0:
        order = conn.execute(
            """SELECT * FROM orders
               WHERE symbol=? AND side=? AND order_type='MARKET'
                 AND datetime(created_at) <= datetime(?)
                 AND ABS(COALESCE(quantity, 0)-?) <= MAX(0.001, ?*0.02)
                 AND ABS(COALESCE(price, 0)-?) <= MAX(1e-10, ?*0.03)
               ORDER BY ABS(COALESCE(quantity, 0)-?) ASC,
                        ABS(COALESCE(price, 0)-?) ASC,
                        datetime(created_at) DESC
               LIMIT 1""",
            (trade["symbol"], order_side, trade["exit_time"], quantity, quantity, entry, entry, quantity, entry),
        ).fetchone()

    candidate = None
    if order and order["alpha_symbol"]:
        candidate = conn.execute(
            """SELECT * FROM alpha_trade_candidates
               WHERE futures_symbol=? AND alpha_symbol=?
                 AND datetime(time) BETWEEN datetime(?, '-20 minutes') AND datetime(?, '+20 minutes')
               ORDER BY CASE entry_status WHEN 'planned_open' THEN 0 ELSE 1 END,
                        ABS(strftime('%s', time)-strftime('%s', ?)) ASC
               LIMIT 1""",
            (trade["symbol"], order["alpha_symbol"], order["created_at"], order["created_at"], order["created_at"]),
        ).fetchone()

    features = loads(decision["features_json"], {}) if decision else {}
    risk = loads(decision["risk_params_json"], {}) if decision else {}
    reason = loads(decision["reason_json"], {}) if decision else {}
    if candidate:
        raw_alpha = loads(candidate["raw_alpha_json"], {})
        vp_metrics = loads(candidate["volume_price_metrics_json"], {})
        features = {
            **raw_alpha,
            "trend_score": vp_metrics.get("trend_score"),
            "spot_volume_ratio": vp_metrics.get("alpha_volume_growth_6h"),
            "futures_volume_ratio": vp_metrics.get("futures_volume_growth_6h"),
            "spread_pct": vp_metrics.get("spread_pct"),
            "volume_sync_state": "synchronized" if (
                float(vp_metrics.get("alpha_volume_growth_6h") or 0) >= 1.8
                and float(vp_metrics.get("futures_volume_growth_6h") or 0) >= 1.5
            ) else "not_confirmed",
            "entry_template": candidate["volume_price_state"],
        }
        reason = {
            "reason": order["reason"],
            "passed_conditions": loads(candidate["volume_price_reasons_json"], []),
            "entry_template": candidate["volume_price_state"],
        }
    if order:
        stop_order = conn.execute(
            """SELECT * FROM orders
               WHERE symbol=? AND order_type='STOP_MARKET' AND side<>?
                 AND datetime(created_at) BETWEEN datetime(?, '-2 minutes') AND datetime(?, '+2 minutes')
               ORDER BY datetime(created_at) ASC LIMIT 1""",
            (trade["symbol"], order_side, order["created_at"], order["created_at"]),
        ).fetchone()
        if stop_order:
            risk["stop_loss"] = stop_order["price"]
            risk["stop_pct"] = abs(float(order["price"] or entry) - float(stop_order["price"] or 0)) / float(order["price"] or entry)
    stop = _first_value(risk.get("initial_stop_loss"), risk.get("stop_loss"))
    stop_pct = _first_value(risk.get("stop_pct"), abs(entry - float(stop)) / entry if stop and entry else None)
    source = _first_value(
        order["strategy_source"] if order else None,
        trade["strategy_source"] if trade["strategy_source"] != "unknown" else None,
        "alpha" if (trade["alpha_symbol"] or (order and order["alpha_symbol"])) else "normal",
    )
    resolved_entry_time = order["created_at"] if order else entry_time
    resolved_entry_price = order["price"] if order else trade["entry_price"]
    resolved_quantity = order["quantity"] if order else trade["quantity"]
    resolved_alpha_symbol = _first_value(order["alpha_symbol"] if order else None, trade["alpha_symbol"])
    snapshot = {
        "position_trade_id": trade["position_trade_id"], "source_decision_id": decision["decision_id"] if decision else None,
        "symbol": trade["symbol"], "alpha_symbol": resolved_alpha_symbol, "side": trade["side"],
        "strategy_source": source, "category": category_for(trade["symbol"], features, source),
        "entry_template": _first_value(features.get("entry_template"), features.get("alpha_profile"), reason.get("entry_template"), order["alpha_profile"] if order else None),
        "market_regime": decision["market_regime"] if decision else None, "entry_time": resolved_entry_time,
        "entry_price": resolved_entry_price, "quantity": resolved_quantity,
        "leverage": _first_value(risk.get("leverage"), features.get("leverage")),
        "margin": _first_value(risk.get("margin"), risk.get("initial_margin")),
        "notional": _first_value(risk.get("notional"), entry * float(trade["quantity"] or 0) if entry else None),
        "stop_loss": stop, "stop_pct": stop_pct, "take_profit_1": _first_value(risk.get("tp1"), risk.get("take_profit_1")),
        "take_profit_2": _first_value(risk.get("tp2"), risk.get("take_profit_2")),
        "risk_reward_ratio": _first_value(risk.get("risk_reward_ratio"), risk.get("rr")),
        "atr_pct": _first_value(features.get("atr_pct"), risk.get("atr_pct")),
        "total_score": _first_value(decision["composite_score"] if decision else None, order["alpha_score"] if order else None),
        "grade": decision["grade"] if decision else None,
        "score_items_json": _first_value(features.get("score_items"), reason.get("score_items")),
        "trend_score": _first_value(features.get("trend_score"), features.get("trend_strength")),
        "breakout_state": _first_value(features.get("breakout_state"), features.get("breakout")),
        "spot_volume_ratio": features.get("spot_volume_ratio"), "futures_volume_ratio": features.get("futures_volume_ratio"),
        "volume_sync_state": _first_value(features.get("volume_sync_state"), reason.get("volume_sync_state")),
        "spread_pct": features.get("spread_pct"), "orderbook_state": features.get("orderbook_state"),
        "passed_conditions_json": reason.get("passed_conditions"), "relaxed_conditions_json": reason.get("relaxed_conditions"),
        "features_json": features, "risk_params_json": risk, "reason_json": reason,
        "snapshot_source": "decision_match" if decision else ("order_match" if order else "historical_rebuild"), "position_status": "closed",
    }
    snapshot["entry_reason_text"] = _entry_reason_sentence(snapshot)
    return snapshot


def review_position_trade_entries(limit: int = 300) -> dict:
    init_db()
    conn = get_conn()
    try:
        conn.execute(
            """DELETE FROM trade_entry_reviews
               WHERE position_status='open'
                 AND EXISTS (
                     SELECT 1 FROM position_trades pt
                     WHERE pt.symbol=trade_entry_reviews.symbol
                       AND COALESCE(pt.side, '')=COALESCE(trade_entry_reviews.side, '')
                       AND ABS(COALESCE(pt.entry_price, 0)-COALESCE(trade_entry_reviews.entry_price, 0))
                           <= MAX(1e-10, ABS(COALESCE(pt.entry_price, 0))*1e-8)
                       AND ABS(strftime('%s', pt.entry_time)-strftime('%s', trade_entry_reviews.entry_time)) <= 300
                       AND pt.exit_time IS NOT NULL
                 )"""
        )
        latest_exchange_time = conn.execute("SELECT MAX(time) FROM positions_history").fetchone()[0]
        exchange_positions = []
        if latest_exchange_time and parse_dt(latest_exchange_time) >= datetime.now(timezone.utc) - timedelta(hours=2):
            exchange_positions = conn.execute(
                "SELECT * FROM positions_history WHERE time=? AND COALESCE(quantity, 0)>0",
                (latest_exchange_time,),
            ).fetchall()
        live_keys = {(row["symbol"], row["side"]) for row in exchange_positions}
        exchange_dt = parse_dt(latest_exchange_time)
        for stale in conn.execute("SELECT position_trade_id,symbol,side,entry_time FROM trade_entry_reviews WHERE position_status='open'").fetchall():
            if (stale["symbol"], stale["side"]) in live_keys:
                continue
            if exchange_dt and parse_dt(stale["entry_time"]) and parse_dt(stale["entry_time"]) <= exchange_dt:
                conn.execute("DELETE FROM trade_entry_reviews WHERE position_trade_id=?", (stale["position_trade_id"],))
        inserted = 0
        live_position_ids = set()
        for exchange_position in exchange_positions:
            position = conn.execute(
                """SELECT * FROM position_history WHERE symbol=? AND side=?
                   ORDER BY datetime(entry_time) DESC LIMIT 1""",
                (exchange_position["symbol"], exchange_position["side"]),
            ).fetchone()
            action = dict(position) if position else {}
            action.update({
                "position_id": (position["position_id"] if position else None) or stable_id(
                    "exchange-position", exchange_position["symbol"], exchange_position["side"], exchange_position["entry_price"]
                ),
                "symbol": exchange_position["symbol"], "position_side": exchange_position["side"],
                "quantity": exchange_position["quantity"], "entry_price": exchange_position["entry_price"],
                "leverage": exchange_position["leverage"],
                "margin": (abs(float(exchange_position["quantity"] or 0) * float(exchange_position["entry_price"] or 0))
                           / max(float(exchange_position["leverage"] or 1), 1)),
                "score": position["entry_score"] if position else None,
                "stop_loss": position["initial_stop_loss"] if position else None,
                "stop_pct": position["stop_pct"] if position else None,
                "entry_template": _first_value(position["alpha_profile"], position["signal_source"]) if position else None,
                "entry_time": position["entry_time"] if position else latest_exchange_time,
            })
            if record_entry_review_snapshot(build_execution_entry_snapshot(action), conn=conn):
                inserted += 1
            live_position_ids.add(action["position_id"])
        trades = conn.execute(
            """SELECT * FROM position_trades WHERE position_trade_id IS NOT NULL AND symbol <> 'ACCOUNT'
               ORDER BY datetime(entry_time) DESC LIMIT ?""", (limit,)
        ).fetchall()
        for trade in trades:
            if not trade["side"] or float(trade["entry_price"] or 0) <= 0:
                continue
            snapshot = _snapshot_from_trade(conn, trade)
            if snapshot.get("snapshot_source") != "historical_rebuild":
                conn.execute(
                    "DELETE FROM trade_entry_reviews WHERE position_trade_id=? AND snapshot_source='historical_rebuild'",
                    (trade["position_trade_id"],),
                )
            if record_entry_review_snapshot(snapshot, conn=conn):
                inserted += 1
            conn.execute(
                """UPDATE trade_entry_reviews SET position_status='closed', exit_time=?, exit_price=?,
                   net_pnl=?, pnl_pct=?, updated_at=datetime('now') WHERE position_trade_id=?""",
                (trade["exit_time"], trade["exit_price"], trade["net_pnl"],
                 _pct_for_review(trade["pnl_pct"]), trade["position_trade_id"]),
            )
        for position_id in live_position_ids:
            conn.execute(
                """UPDATE trade_entry_reviews SET position_status='open', exit_time=NULL, exit_price=NULL,
                   net_pnl=NULL, pnl_pct=NULL, updated_at=datetime('now') WHERE position_trade_id=?""",
                (position_id,),
            )
        rows = conn.execute(
            "SELECT * FROM trade_entry_reviews ORDER BY datetime(entry_time) DESC LIMIT ?", (limit,)
        ).fetchall()
        reviewed = 0
        for row in rows:
            entry = float(row["entry_price"] or 0)
            entry_dt = parse_dt(row["entry_time"])
            if entry <= 0 or not entry_dt:
                continue
            horizon = 24 if row["strategy_source"] == "alpha" else (72 if row["category"] == "core_bluechip" else 48)
            candles = conn.execute(
                """SELECT time, high, low, close FROM futures_candles_1h
                   WHERE symbol=? AND datetime(time) >= datetime(?) AND datetime(time) <= datetime(?, ?)
                   ORDER BY datetime(time) ASC""",
                (row["symbol"], row["entry_time"], row["entry_time"], f"+{horizon} hours"),
            ).fetchall()
            side = row["side"]
            path = [_signed_return(float(c["close"]), entry, side) for c in candles]
            highs_lows = []
            for candle in candles:
                favorable_price = float(candle["low"] if str(side).upper() == "SHORT" else candle["high"])
                adverse_price = float(candle["high"] if str(side).upper() == "SHORT" else candle["low"])
                highs_lows.append((_signed_return(favorable_price, entry, side), _signed_return(adverse_price, entry, side)))
            risk_pct = row["stop_pct"] or (abs(entry - float(row["stop_loss"])) / entry if row["stop_loss"] else row["atr_pct"])
            overheated = str(row["breakout_state"] or "").lower() in {"overheated", "extended", "chasing"}
            result = classify_entry_review(float(risk_pct) if risk_pct else None, path, overheated, row["position_status"] == "closed")
            conn.execute(
                """UPDATE trade_entry_reviews SET max_favorable_return=?, max_adverse_return=?, return_now=?,
                   bars_observed=?, review_label=?, review_reason=?, reviewed_at=?, updated_at=datetime('now')
                   WHERE position_trade_id=?""",
                (max((x[0] for x in highs_lows), default=None), min((x[1] for x in highs_lows), default=None),
                 path[-1] if path else None, len(path), result["label"], result["reason"], utc_now(), row["position_trade_id"]),
            )
            reviewed += 1
        conn.commit()
        return {"inserted": inserted, "reviewed": reviewed}
    finally:
        conn.close()


def summarize_entry_reviews(recent_limit: int = 30) -> dict:
    init_db()
    conn = get_conn()
    try:
        groups = conn.execute(
            """SELECT strategy_source, category, COALESCE(entry_template, 'unknown') entry_template,
                      COUNT(*) sample_size,
                      SUM(review_label='reasonable') reasonable_count,
                      SUM(review_label='early') early_count,
                      SUM(review_label='chased') chased_count,
                      SUM(review_label='bad_condition') bad_condition_count,
                      AVG(max_favorable_return) avg_mfe, AVG(max_adverse_return) avg_mae
               FROM (SELECT * FROM trade_entry_reviews WHERE position_status='closed' AND review_label <> 'pending'
                     ORDER BY datetime(entry_time) DESC LIMIT ?)
               GROUP BY strategy_source, category, COALESCE(entry_template, 'unknown')""", (recent_limit,)
        ).fetchall()
        summaries = []
        for group in groups:
            item = dict(group)
            item.update(build_entry_group_recommendation(item))
            item["summary_id"] = stable_id(item["strategy_source"], item["category"], item["entry_template"])
            summaries.append(item)
        return {"summaries": summaries}
    finally:
        conn.close()


def fetch_position_action_evidence(position_trade_id: str, limit: int = 100) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        review = conn.execute("SELECT * FROM trade_entry_reviews WHERE position_trade_id=?", (position_trade_id,)).fetchone()
        if not review:
            return []
        start = review["entry_time"]
        end = review["exit_time"] or utc_now()
        return [dict(row) for row in conn.execute(
            """SELECT * FROM decision_actions WHERE symbol=? AND side=?
               AND datetime(time) >= datetime(?, '-15 minutes') AND datetime(time) <= datetime(?, '+15 minutes')
               ORDER BY datetime(time), id LIMIT ?""",
            (review["symbol"], review["side"], start, end, int(limit)),
        ).fetchall()]
    finally:
        conn.close()


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
                       FROM futures_candles_1h
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


def _exit_review_thresholds(category: str, strategy_source: str | None = None) -> dict:
    if strategy_source == "alpha":
        return EXIT_REVIEW_THRESHOLDS["alpha"]
    return EXIT_REVIEW_THRESHOLDS.get(category) or EXIT_REVIEW_THRESHOLDS["default"]


def _pct_for_review(value: Any) -> float | None:
    """Convert stored percentage points (for example, -0.99%) to a ratio."""
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except Exception:
        return None


def _select_position_trades_for_review(conn, limit: int):
    return conn.execute(
        """SELECT pt.*
           FROM position_trades pt
           WHERE pt.position_trade_id IS NOT NULL
             AND pt.symbol <> 'ACCOUNT'
             AND pt.exit_time IS NOT NULL
           ORDER BY datetime(pt.exit_time) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def _exit_review_label(pnl_pct: float | None, mfe: float, mae: float, category: str, strategy_source: str | None, bars: int) -> str:
    if bars < 4:
        return "pending"
    th = _exit_review_thresholds(category, strategy_source)
    pnl = pnl_pct
    if pnl is not None:
        if th["noise_loss"] <= pnl <= 0 and mfe >= th["noise_mfe"]:
            return "noise_loss_exit"
        if 0 < pnl <= th["small_profit"] and mfe >= th["small_mfe"]:
            return "small_profit_exit"
    if mfe >= th["early"]:
        return "early_exit"
    if mfe < th["early"] * 0.5 and mae <= -max(th["noise_mfe"], 0.02):
        return "good_exit"
    if pnl is not None and pnl > 0 and mae <= -max(float(pnl) * 0.6, 0.02):
        return "late_exit"
    return "good_exit" if mfe < th["early"] * 0.7 else "unknown"


def _pct_text(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except Exception:
        return "-"


def _minutes_text(value: Any) -> str:
    if value is None:
        return "-"
    try:
        minutes = float(value)
    except Exception:
        return "-"
    if minutes >= 120:
        return f"{minutes / 60:.1f}小时"
    return f"{minutes:.0f}分钟"


def _trade_value(trade: dict | Any, key: str, default: Any = None) -> Any:
    try:
        return trade.get(key, default)
    except AttributeError:
        try:
            return trade[key]
        except Exception:
            return default


def _simple_category_key(category: str | None, source: str | None) -> str:
    if source == "alpha" or category == "alpha":
        return "alpha"
    if category in {"core_bluechip", "bluechip", "bluechip_trend"}:
        return "core_bluechip"
    return "normal"


def _simple_threshold(category: str | None, source: str | None) -> float:
    key = _simple_category_key(category, source)
    if key == "alpha":
        return 0.06
    if key == "core_bluechip":
        return 0.025
    return 0.04


def _entry_reason_text(trade: dict | Any) -> str:
    source = _trade_value(trade, "strategy_source") or "unknown"
    side = _trade_value(trade, "side") or "-"
    reason = str(_trade_value(trade, "entry_reason") or "").strip()
    score_match = re.search(r"(?:alpha_score|score|entry_alpha)=([0-9]+(?:\.[0-9]+)?)", reason)
    score_text = f"，alpha_score={score_match.group(1)}" if score_match else ""
    if source == "alpha" or "alpha_" in reason:
        if "trend_probe" in reason:
            mode = "Alpha 放量趋势探测通过"
        elif "volume_price" in reason:
            mode = "Alpha 量价条件通过"
        else:
            mode = "Alpha 入场条件通过"
        return f"{mode}{score_text}，方向 {side}；原始条件：{reason or '缺少本地开仓明细'}。"
    if reason:
        return f"普通策略入场，方向 {side}；原始条件：{reason}。"
    return f"开仓记录缺少详细条件，方向 {side}；需要结合当时评分和动作流水继续补充。"


def _exit_reason_text(trade: dict | Any) -> str:
    reason = str(_trade_value(trade, "exit_reason") or "").strip()
    pnl_pct = _trade_value(trade, "pnl_pct")
    pnl_text = _pct_text(pnl_pct)
    if "alpha_volume_regime_profit_protect" in reason:
        return f"触发 Alpha 成交量状态可疑下的利润保护；当时仓位收益约 {pnl_text}，系统担心放量衰减或诱多，执行保护性平仓。"
    if "alpha_volume_regime_bad" in reason:
        return f"触发 Alpha 成交量状态可疑/转弱；当时仓位收益约 {pnl_text}，系统认为量能结构不再支持继续持有。"
    if "hard_stop" in reason:
        return f"触发硬止损；当时仓位收益约 {pnl_text}，属于风险底线退出。"
    if "roll_stop" in reason or "trailing" in reason:
        return f"触发移动止盈/回撤保护；当时仓位收益约 {pnl_text}，系统认为利润回吐达到退出条件。"
    if "history_expectancy_turns_bad" in reason:
        return f"触发历史期望转差过滤；当时仓位收益约 {pnl_text}，系统认为该类交易近期表现变差。"
    if "trend_fade" in reason:
        return f"触发趋势衰减；当时仓位收益约 {pnl_text}，系统认为趋势延续性下降。"
    if "orderbook_depth_weak" in reason:
        return f"触发盘口深度不足；当时仓位收益约 {pnl_text}，系统担心流动性和滑点风险。"
    if "alpha_spread_widened" in reason:
        return f"触发点差扩大；当时仓位收益约 {pnl_text}，系统担心成交质量变差。"
    if reason == "REALIZED_PNL":
        return f"交易所成交盈亏反推的平仓，缺少本地明确平仓条件；实际收益约 {pnl_text}。"
    return f"触发 {reason or 'unknown'}；实际收益约 {pnl_text}。"


def _simple_exit_conclusion(trade: dict | Any, metrics: dict) -> str:
    bars = int(metrics.get("bars_observed") or 0)
    if bars < 4:
        return "数据不足"
    category = _trade_value(trade, "category")
    source = _trade_value(trade, "strategy_source")
    threshold = _simple_threshold(category, source)
    pnl = _trade_value(trade, "pnl_pct")
    mfe = float(metrics.get("max_favorable_return") or 0)
    mae = float(metrics.get("max_adverse_return") or 0)
    if pnl is not None and -0.03 <= pnl <= 0.01 and mfe >= threshold:
        return "小亏误杀"
    if mfe >= threshold and abs(mae) <= max(mfe * 0.6, 0.015):
        return "平早了"
    if pnl is not None and pnl > 0 and mfe > 0 and (pnl / mfe) < 0.4:
        return "止盈太早"
    if pnl is not None and pnl < 0 and mfe < threshold * 0.5:
        return "入场质量差，不是平仓问题"
    return "平得合理"


def _exit_advice(category: str | None, source: str | None, label: str) -> str:
    key = _simple_category_key(category, source)
    if label == "平早了":
        if key == "alpha":
            return "建议 Alpha 成交量可疑不要直接全平，改为部分止盈，剩余仓位用移动止盈或近 3 根 K 线低点确认。"
        if key == "core_bluechip":
            return "建议蓝筹趋势仓放宽移动止盈，避免小波动打断完整波段。"
        return "建议普通币平仓信号增加趋势确认，强势延续时先减仓而不是直接清仓。"
    if label == "小亏误杀":
        return "建议该类小亏退出增加 1-2 根 K 线确认，避免短期噪音把刚启动的仓位洗掉。"
    if label == "止盈太早":
        return "建议把一次性止盈改为部分止盈 + 移动止盈，保留仓位继续跟随趋势。"
    if label == "入场质量差，不是平仓问题":
        return "建议优先优化入场过滤，提高趋势、成交额和盘口质量要求，平仓规则暂不作为主要问题。"
    if label == "数据不足":
        return "建议等待更多后续 K 线后再判断，当前只记录事实，不调整规则。"
    return "建议保留当前平仓条件，继续观察同类样本是否稳定。"


def _build_simple_exit_review_text(trade: dict | Any, metrics: dict, label: str) -> str:
    category = _trade_value(trade, "category")
    source = _trade_value(trade, "strategy_source")
    symbol = _trade_value(trade, "symbol") or "-"
    net_pnl = float(_trade_value(trade, "net_pnl") or 0)
    holding = _minutes_text(_trade_value(trade, "holding_minutes"))
    follow = (
        f"平仓后1h {_pct_text(metrics.get('return_1h'))}，4h {_pct_text(metrics.get('return_4h'))}，"
        f"12h {_pct_text(metrics.get('return_12h'))}，24h {_pct_text(metrics.get('return_24h'))}；"
        f"最大有利空间 {_pct_text(metrics.get('max_favorable_return'))}，"
        f"最大反向回撤 {_pct_text(metrics.get('max_adverse_return'))}。"
    )
    return (
        f"开仓：{symbol} 持仓 {holding}，{_entry_reason_text(trade)}\n"
        f"平仓：{_exit_reason_text(trade)}实际盈亏 {net_pnl:.2f}U。\n"
        f"后续：{follow}\n"
        f"结论：{label}。{_exit_advice(category, source, label)}"
    )


def _exit_summary_recommendation(
    category: str | None,
    source: str | None,
    label: str,
    sample: int,
    total_pnl: float,
    avg_mfe: float,
    avg_mae: float,
) -> str:
    key = _simple_category_key(category, source)
    category_text = "Alpha" if key == "alpha" else ("蓝筹" if key == "core_bluechip" else "普通币")
    base = (
        f"{category_text}类最近 {sample} 笔归为“{label}”，合计盈亏 {total_pnl:.2f}U，"
        f"平仓后平均最大有利空间 {_pct_text(avg_mfe)}，平均反向回撤 {_pct_text(avg_mae)}。"
    )
    return base + _exit_advice(category, source, label)


def _exit_review_summary(symbol: str, exit_reason: str, net_pnl: float, mfe: float, label: str) -> str:
    label_text = {
        "good_exit": "平仓后没有明显继续走强，暂定平得合理",
        "early_exit": "平仓后继续朝原方向走出明显空间，疑似平早",
        "noise_loss_exit": "小亏出场后又给出有利空间，疑似被噪音洗出",
        "small_profit_exit": "小盈利出场后仍有明显空间，疑似吃得太少",
        "late_exit": "平仓前后回吐偏大，疑似保护利润偏慢",
        "pending": "后续K线不足，暂不下结论",
    }.get(label, "样本结论不明确，继续观察")
    return f"{symbol} 因 {exit_reason or 'unknown'} 平仓，实际盈亏 {net_pnl:.2f}U；平仓后最大有利空间 {mfe * 100:.2f}%，{label_text}。"


def review_position_trade_exits(limit: int = 300) -> dict:
    init_db()
    conn = get_conn()
    try:
        conn.execute(
            """DELETE FROM trade_exit_reviews
               WHERE position_trade_id NOT IN (
                   SELECT position_trade_id
                   FROM position_trades
                   WHERE position_trade_id IS NOT NULL
               )"""
        )
        rows = _select_position_trades_for_review(conn, limit)
        payload = []
        for row in rows:
            entry_review = conn.execute(
                "SELECT * FROM trade_entry_reviews WHERE position_trade_id=? ORDER BY id DESC LIMIT 1",
                (row["position_trade_id"],),
            ).fetchone()
            entry_dt = parse_dt(entry_review["entry_time"] if entry_review else row["entry_time"])
            exit_reason = row["exit_reason"]
            exit_dt = parse_dt(row["exit_time"])
            if entry_review and entry_review["stop_loss"] and row["exit_price"]:
                stop = float(entry_review["stop_loss"])
                actual_exit_price = float(row["exit_price"])
                side_text = str(row["side"] or "LONG").upper()
                stop_hit = (
                    (side_text == "LONG" and actual_exit_price <= stop * 1.01)
                    or (side_text == "SHORT" and actual_exit_price >= stop * 0.99)
                )
                if stop_hit:
                    exit_reason = "initial_stop_loss"
                    price_column = "low" if side_text == "LONG" else "high"
                    operator = "<=" if side_text == "LONG" else ">="
                    crossing = conn.execute(
                        f"""SELECT time FROM futures_candles_15m
                            WHERE symbol=? AND datetime(time) >= datetime(?)
                              AND {price_column} {operator} ?
                            ORDER BY datetime(time) ASC LIMIT 1""",
                        (row["symbol"], entry_review["entry_time"], stop),
                    ).fetchone()
                    crossing_dt = parse_dt(crossing["time"]) if crossing else None
                    if crossing_dt:
                        commission = conn.execute(
                            """SELECT income_time FROM exchange_income_ledger
                               WHERE symbol=? AND income_type='COMMISSION'
                                 AND datetime(income_time) >= datetime(?)
                                 AND datetime(income_time) < datetime(?, '+15 minutes')
                               ORDER BY datetime(income_time) ASC LIMIT 1""",
                            (row["symbol"], iso_z(crossing_dt), iso_z(crossing_dt)),
                        ).fetchone()
                        exit_dt = parse_dt(commission["income_time"]) if commission else crossing_dt
            if not exit_dt:
                continue
            entry_price = float(row["exit_price"] or row["entry_price"] or 0)
            if entry_price <= 0:
                continue
            side = row["side"] or "LONG"
            end_dt = exit_dt + timedelta(hours=73)
            candles = [
                dict(c)
                for c in conn.execute(
                    """SELECT time, close, high, low
                       FROM futures_candles_1h
                       WHERE symbol = ?
                         AND time > ?
                         AND time <= ?
                       ORDER BY time ASC""",
                    (row["symbol"], iso_z(exit_dt), iso_z(end_dt)),
                ).fetchall()
            ]
            for c in candles:
                c["_time"] = parse_dt(c["time"])
            candles = [c for c in candles if c.get("_time")]
            returns = {}
            for hours in WINDOWS:
                eligible = [c for c in candles if c["_time"] <= exit_dt + timedelta(hours=hours)]
                returns[hours] = _signed_return(float(eligible[-1]["close"]), entry_price, side) if eligible else None
            signed_highs = []
            signed_lows = []
            for c in candles:
                high_ret = _signed_return(float(c["high"]), entry_price, side)
                low_ret = _signed_return(float(c["low"]), entry_price, side)
                signed_highs.append((max(high_ret, low_ret), c["time"]))
                signed_lows.append((min(high_ret, low_ret), c["time"]))
            if signed_highs:
                mfe, mfe_time = max(signed_highs, key=lambda x: x[0])
                mae, mae_time = min(signed_lows, key=lambda x: x[0])
            else:
                mfe, mfe_time, mae, mae_time = 0.0, None, 0.0, None
            strategy_source = _first_value(
                entry_review["strategy_source"] if entry_review else None,
                row["strategy_source"] if row["strategy_source"] != "unknown" else None,
                "unknown",
            )
            category = _first_value(entry_review["category"] if entry_review else None, category_for(row["symbol"], {}, strategy_source))
            pnl_pct = _pct_for_review(row["pnl_pct"])
            net_pnl = float(row["net_pnl"] or 0)
            holding_minutes = ((exit_dt - entry_dt).total_seconds() / 60.0) if entry_dt else None
            trade = dict(row)
            trade.update({
                "category": category,
                "strategy_source": strategy_source,
                "exit_reason": exit_reason,
                "pnl_pct": pnl_pct,
                "net_pnl": net_pnl,
                "holding_minutes": holding_minutes,
            })
            metrics = {
                "bars_observed": len(candles),
                "return_1h": returns.get(1),
                "return_4h": returns.get(4),
                "return_12h": returns.get(12),
                "return_24h": returns.get(24),
                "return_48h": returns.get(48),
                "return_72h": returns.get(72),
                "max_favorable_return": mfe,
                "max_adverse_return": mae,
                "max_favorable_time": mfe_time,
                "max_adverse_time": mae_time,
            }
            label = _simple_exit_conclusion(trade, metrics)
            evidence = {
                "bars_observed": len(candles),
                "thresholds": _exit_review_thresholds(category, strategy_source),
                "returns": {str(k): returns.get(k) for k in WINDOWS},
                "entry_reason_text": _entry_reason_text(trade),
                "exit_reason_text": _exit_reason_text(trade),
                "simple_conclusion": label,
                "recommendation": _exit_advice(category, strategy_source, label),
            }
            payload.append(
                (
                    row["position_trade_id"], row["symbol"], strategy_source, row["alpha_symbol"],
                    side, category, entry_review["entry_time"] if entry_review else row["entry_time"], iso_z(exit_dt), exit_reason,
                    net_pnl, pnl_pct, holding_minutes, returns.get(1), returns.get(4),
                    returns.get(12), returns.get(24), returns.get(72), mfe, mae,
                    mfe_time, mae_time, label,
                    _build_simple_exit_review_text(trade, metrics, label),
                    dumps(evidence),
                )
            )
        if payload:
            conn.executemany(
                """INSERT INTO trade_exit_reviews
                   (position_trade_id, symbol, strategy_source, alpha_symbol, side, category,
                    entry_time, exit_time, exit_reason, net_pnl, pnl_pct, holding_minutes,
                    return_1h, return_4h, return_12h, return_24h, return_72h,
                    max_favorable_return, max_adverse_return, max_favorable_time,
                    max_adverse_time, review_label, review_summary, evidence_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(position_trade_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    strategy_source=excluded.strategy_source,
                    alpha_symbol=excluded.alpha_symbol,
                    side=excluded.side,
                    category=excluded.category,
                    entry_time=excluded.entry_time,
                    exit_time=excluded.exit_time,
                    exit_reason=excluded.exit_reason,
                    net_pnl=excluded.net_pnl,
                    pnl_pct=excluded.pnl_pct,
                    holding_minutes=excluded.holding_minutes,
                    return_1h=excluded.return_1h,
                    return_4h=excluded.return_4h,
                    return_12h=excluded.return_12h,
                    return_24h=excluded.return_24h,
                    return_72h=excluded.return_72h,
                    max_favorable_return=excluded.max_favorable_return,
                    max_adverse_return=excluded.max_adverse_return,
                    max_favorable_time=excluded.max_favorable_time,
                    max_adverse_time=excluded.max_adverse_time,
                    review_label=excluded.review_label,
                    review_summary=excluded.review_summary,
                    evidence_json=excluded.evidence_json,
                    updated_at=datetime('now')""",
                payload,
            )
        conn.commit()
        return {"reviewed": len(payload), "scanned": len(rows)}
    finally:
        conn.close()


def _summarize_exit_reviews_simple(window_days: int = 7, recent_limit: int = 30) -> dict:
    init_db()
    review_position_trade_exits(limit=500)
    conn = get_conn()
    run_time = utc_now()
    cutoff = iso_z(datetime.now(timezone.utc) - timedelta(days=window_days))
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT *
                   FROM trade_exit_reviews
                   WHERE datetime(exit_time) >= datetime(?)
                   ORDER BY datetime(exit_time) DESC
                   LIMIT ?""",
                (cutoff, recent_limit),
            ).fetchall()
        ]
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in rows:
            groups[
                (
                    row.get("category") or "unknown",
                    row.get("strategy_source") or "unknown",
                    row.get("review_label") or "unknown",
                )
            ].append(row)

        payload = []
        summaries = []
        for (category, source, label), items in groups.items():
            sample = len(items)
            pnl_values = [float(x.get("net_pnl") or 0) for x in items]
            total_pnl = sum(pnl_values)
            labels = [x.get("review_label") or "unknown" for x in items]
            good = labels.count("平得合理")
            early = labels.count("平早了")
            noise = labels.count("小亏误杀")
            small = labels.count("止盈太早")
            late = labels.count("入场质量差，不是平仓问题")
            avg_mfe = _mean([x.get("max_favorable_return") for x in items])
            avg_mae = _mean([x.get("max_adverse_return") for x in items])
            summary_text = _exit_summary_recommendation(category, source, label, sample, total_pnl, avg_mfe, avg_mae)
            if label == "平得合理":
                action_type = "keep"
                conclusion = "继续保持"
            elif label in {"平早了", "小亏误杀", "止盈太早"}:
                action_type = "improve"
                conclusion = "需要优化"
            elif label == "入场质量差，不是平仓问题":
                action_type = "improve_entry"
                conclusion = "优化入场"
            else:
                action_type = "watch"
                conclusion = "继续观察"
            summary_id = stable_id("exit_summary", window_days, category, source, label)
            win_count = sum(1 for v in pnl_values if v > 0)
            loss_count = sum(1 for v in pnl_values if v <= 0)
            payload.append(
                (
                    summary_id,
                    run_time,
                    window_days,
                    category,
                    source,
                    label,
                    sample,
                    win_count,
                    loss_count,
                    _mean(pnl_values),
                    total_pnl,
                    avg_mfe,
                    avg_mae,
                    good,
                    early,
                    noise,
                    small,
                    late,
                    conclusion,
                    action_type,
                    summary_text,
                )
            )
            summaries.append({
                "summary_id": summary_id,
                "run_time": run_time,
                "window_days": window_days,
                "category": category,
                "strategy_source": source,
                "exit_reason": label,
                "sample_size": sample,
                "win_count": win_count,
                "loss_count": loss_count,
                "avg_pnl": _mean(pnl_values),
                "total_pnl": total_pnl,
                "avg_mfe_after_exit": avg_mfe,
                "avg_mae_after_exit": avg_mae,
                "good_exit_count": good,
                "early_exit_count": early,
                "noise_loss_exit_count": noise,
                "small_profit_exit_count": small,
                "late_exit_count": late,
                "conclusion": conclusion,
                "action_type": action_type,
                "summary_text": summary_text,
            })
        if payload:
            conn.executemany(
                """INSERT OR REPLACE INTO exit_review_summaries
                   (summary_id, run_time, window_days, category, strategy_source,
                    exit_reason, sample_size, win_count, loss_count, avg_pnl,
                    total_pnl, avg_mfe_after_exit, avg_mae_after_exit,
                    good_exit_count, early_exit_count, noise_loss_exit_count,
                    small_profit_exit_count, late_exit_count, conclusion,
                    action_type, summary_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                payload,
            )
        conn.commit()
        return {"run_time": run_time, "window_days": window_days, "samples": len(rows), "summaries": summaries}
    finally:
        conn.close()


def summarize_exit_reviews(window_days: int = 7, recent_limit: int = 30) -> dict:
    return _summarize_exit_reviews_simple(window_days=window_days, recent_limit=recent_limit)
    init_db()
    review_position_trade_exits(limit=500)
    conn = get_conn()
    run_time = utc_now()
    cutoff = iso_z(datetime.now(timezone.utc) - timedelta(days=window_days))
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT *
                   FROM trade_exit_reviews
                   WHERE datetime(exit_time) >= datetime(?)
                   ORDER BY datetime(exit_time) DESC
                   LIMIT ?""",
                (cutoff, recent_limit),
            ).fetchall()
        ]
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in rows:
            groups[(row.get("category") or "unknown", row.get("strategy_source") or "unknown", row.get("exit_reason") or "unknown")].append(row)
        payload = []
        summaries = []
        for (category, source, reason), items in groups.items():
            sample = len(items)
            pnl_values = [float(x.get("net_pnl") or 0) for x in items]
            labels = [x.get("review_label") or "unknown" for x in items]
            good = labels.count("good_exit")
            early = labels.count("early_exit")
            noise = labels.count("noise_loss_exit")
            small = labels.count("small_profit_exit")
            late = labels.count("late_exit")
            avg_mfe = _mean([x.get("max_favorable_return") for x in items])
            avg_mae = _mean([x.get("max_adverse_return") for x in items])
            if sample >= 3 and good / sample >= 0.60 and early / sample < 0.25:
                action_type = "keep"
                conclusion = "继续保持"
                summary_text = f"{reason}: 最近{sample}次触发，{good}次平仓有效，平均平仓后最大有利空间{avg_mfe*100:.2f}%，规则暂时有效。"
            elif sample >= 3 and max(early, noise, small) / sample >= 0.40:
                action_type = "improve"
                conclusion = "需要优化"
                if early >= noise and early >= small:
                    focus = "过早平仓"
                elif noise >= small:
                    focus = "小亏噪音洗出"
                else:
                    focus = "小盈利吃得太少"
                summary_text = f"{reason}: 最近{sample}次触发，{focus}占比{max(early, noise, small)/sample:.0%}，平均平仓后最大有利空间{avg_mfe*100:.2f}%，建议进入策略优化观察。"
            else:
                action_type = "watch"
                conclusion = "继续观察"
                summary_text = f"{reason}: 最近{sample}次触发，样本或方向暂不稳定，继续观察。"
            summary_id = stable_id("exit_summary", window_days, category, source, reason)
            payload.append(
                (
                    summary_id, run_time, window_days, category, source, reason, sample,
                    sum(1 for v in pnl_values if v > 0), sum(1 for v in pnl_values if v <= 0),
                    _mean(pnl_values), sum(pnl_values), avg_mfe, avg_mae,
                    good, early, noise, small, late, conclusion, action_type, summary_text,
                )
            )
            summaries.append({
                "summary_id": summary_id,
                "run_time": run_time,
                "window_days": window_days,
                "category": category,
                "strategy_source": source,
                "exit_reason": reason,
                "sample_size": sample,
                "win_count": sum(1 for v in pnl_values if v > 0),
                "loss_count": sum(1 for v in pnl_values if v <= 0),
                "avg_pnl": _mean(pnl_values),
                "total_pnl": sum(pnl_values),
                "avg_mfe_after_exit": avg_mfe,
                "avg_mae_after_exit": avg_mae,
                "good_exit_count": good,
                "early_exit_count": early,
                "noise_loss_exit_count": noise,
                "small_profit_exit_count": small,
                "late_exit_count": late,
                "conclusion": conclusion,
                "action_type": action_type,
                "summary_text": summary_text,
            })
        if payload:
            conn.executemany(
                """INSERT OR REPLACE INTO exit_review_summaries
                   (summary_id, run_time, window_days, category, strategy_source,
                    exit_reason, sample_size, win_count, loss_count, avg_pnl,
                    total_pnl, avg_mfe_after_exit, avg_mae_after_exit,
                    good_exit_count, early_exit_count, noise_loss_exit_count,
                    small_profit_exit_count, late_exit_count, conclusion,
                    action_type, summary_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                payload,
            )
        conn.commit()
        return {"run_time": run_time, "window_days": window_days, "samples": len(rows), "summaries": summaries}
    finally:
        conn.close()


def fetch_exit_reviews(limit: int = 100) -> list[dict]:
    init_db()
    review_position_trade_exits(limit=500)
    conn = get_conn()
    try:
        return [
            {**dict(r), "evidence": loads(r["evidence_json"], {})}
            for r in conn.execute(
                """SELECT *
                   FROM trade_exit_reviews
                   ORDER BY datetime(exit_time) DESC, id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
    finally:
        conn.close()


def fetch_exit_review_summaries(limit: int = 100) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        latest = conn.execute("SELECT MAX(run_time) AS run_time FROM exit_review_summaries").fetchone()["run_time"]
        if not latest:
            summarize_exit_reviews()
            latest = conn.execute("SELECT MAX(run_time) AS run_time FROM exit_review_summaries").fetchone()["run_time"]
        if not latest:
            return []
        return [
            dict(r)
            for r in conn.execute(
                """SELECT *
                   FROM exit_review_summaries
                   WHERE run_time = ?
                   ORDER BY
                     CASE action_type WHEN 'improve' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END,
                     sample_size DESC
                   LIMIT ?""",
                (latest, limit),
            ).fetchall()
        ]
    finally:
        conn.close()


def _lifecycle_conclusion(entry: dict, exit_review: dict) -> tuple[str, str]:
    if exit_review.get("review_label") in {"pending", "数据不足"}:
        return (
            "后续数据不足",
            "平仓后的有效K线不足，当前只记录开平仓事实，不评价是否平早或入场质量，也不据此调整策略。",
        )
    pnl = float(exit_review.get("net_pnl") or entry.get("net_pnl") or 0)
    entry_mfe = float(entry.get("max_favorable_return") or 0)
    entry_mae = float(entry.get("max_adverse_return") or 0)
    post_mfe = float(exit_review.get("max_favorable_return") or 0)
    post_mae = float(exit_review.get("max_adverse_return") or 0)
    category = entry.get("category") or exit_review.get("category") or "unknown"
    early_threshold = 0.10 if category == "alpha" else (0.04 if category == "core_bluechip" else 0.06)

    if pnl < 0 and entry_mfe < 0.02 and entry_mae <= -0.04 and post_mfe < early_threshold:
        return (
            "开仓确认不足，止损基本合理",
            "该仓位开仓后几乎没有形成有效浮盈，随后持续走弱；应收紧开仓确认条件，保留止损，不应通过放宽止损解决。",
        )
    if post_mfe >= early_threshold:
        return (
            "平仓后仍有明显原方向空间",
            "该类仓位应检查是否平早：盈利时优先部分止盈并跟踪趋势；小亏退出需增加一根确认K线，但硬止损仍立即执行。",
        )
    if pnl > 0 and post_mfe < early_threshold:
        return (
            "开平仓整体合理",
            "入场最终产生盈利，平仓后没有留下明显趋势空间；当前条件可以继续保持并积累更多样本。",
        )
    if post_mae < -0.05:
        return (
            "退出避免了进一步亏损",
            "平仓后价格继续向不利方向运行，退出有效；重点优化开仓质量，而不是延后平仓。",
        )
    return (
        "样本暂不明确",
        "当前后续K线或开仓证据不足，先保留事实，不自动调整策略，等待同类样本继续积累。",
    )


def fetch_trade_lifecycle_reviews(limit: int = 100) -> list[dict]:
    """Combine entry, exit and post-exit evidence into one position review."""
    init_db()
    conn = get_conn()
    try:
        entries = {
            row["position_trade_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM trade_entry_reviews WHERE position_status='closed' ORDER BY id ASC"
            ).fetchall()
        }
        exits = {
            row["position_trade_id"]: dict(row)
            for row in conn.execute("SELECT * FROM trade_exit_reviews ORDER BY id ASC").fetchall()
        }
        trades = conn.execute(
            """SELECT * FROM position_trades
               WHERE symbol <> 'ACCOUNT' AND exit_time IS NOT NULL
               ORDER BY datetime(exit_time) DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        result = []
        for trade_row in trades:
            trade = dict(trade_row)
            position_id = trade.get("position_trade_id")
            entry = entries.get(position_id) or {}
            exit_review = exits.get(position_id) or {}
            if not entry and not exit_review:
                continue
            evidence = loads(exit_review.get("evidence_json"), {})
            conclusion, recommendation = _lifecycle_conclusion(entry, exit_review)
            returns = evidence.get("returns") or {}
            result.append({
                "position_trade_id": position_id,
                "symbol": trade.get("symbol"),
                "side": trade.get("side"),
                "strategy_source": _first_value(entry.get("strategy_source"), exit_review.get("strategy_source"), trade.get("strategy_source")),
                "category": _first_value(entry.get("category"), exit_review.get("category"), "unknown"),
                "entry_time": _first_value(entry.get("entry_time"), trade.get("entry_time")),
                "exit_time": _first_value(exit_review.get("exit_time"), trade.get("exit_time")),
                "entry_price": _first_value(entry.get("entry_price"), trade.get("entry_price")),
                "exit_price": trade.get("exit_price"),
                "net_pnl": trade.get("net_pnl"),
                "pnl_pct": _pct_for_review(trade.get("pnl_pct")),
                "entry_condition": entry.get("entry_reason_text") or "开仓指标未完整记录",
                "exit_condition": evidence.get("exit_reason_text") or exit_review.get("exit_reason") or trade.get("exit_reason") or "平仓原因未完整记录",
                "entry_review_label": entry.get("review_label"),
                "exit_review_label": exit_review.get("review_label"),
                "entry_mfe": entry.get("max_favorable_return"),
                "entry_mae": entry.get("max_adverse_return"),
                "return_1h": _first_value(exit_review.get("return_1h"), returns.get("1")),
                "return_4h": _first_value(exit_review.get("return_4h"), returns.get("4")),
                "return_12h": _first_value(exit_review.get("return_12h"), returns.get("12")),
                "return_24h": _first_value(exit_review.get("return_24h"), returns.get("24")),
                "post_mfe": exit_review.get("max_favorable_return"),
                "post_mae": exit_review.get("max_adverse_return"),
                "conclusion": conclusion,
                "recommendation": recommendation,
                "snapshot_source": entry.get("snapshot_source"),
            })
        return result
    finally:
        conn.close()


def _lifecycle_issue_type(row: dict) -> str | None:
    label = str(row.get("exit_review_label") or "").lower()
    if label == "pending" or (row.get("post_mfe") is None and row.get("post_mae") is None):
        return None

    pnl = float(row.get("net_pnl") or 0)
    entry_mfe = float(row.get("entry_mfe") or 0)
    entry_mae = float(row.get("entry_mae") or 0)
    post_mfe = float(row.get("post_mfe") or 0)
    category = row.get("category") or "unknown"
    early_threshold = 0.10 if category == "alpha" else (0.04 if category == "core_bluechip" else 0.06)

    if pnl < 0 and entry_mfe < 0.02 and entry_mae <= -0.04 and post_mfe < early_threshold:
        return "entry_confirmation"
    if post_mfe >= early_threshold:
        return "early_exit"
    return None


def _lifecycle_recommendation(source: str, category: str, issue_type: str) -> str:
    if issue_type == "entry_confirmation":
        if source == "alpha" or category == "alpha":
            return "Alpha probe 必须满足合约同步放量、价格结构站稳且趋势标签非破位风险；not_confirmed 只观察，不开仓。"
        if category == "core_bluechip":
            return "蓝筹开仓需趋势向上且关键价位站稳；趋势或结构未确认时只观察。"
        return "普通币开仓需趋势向上并完成价格结构确认；弱确认信号只观察，不用放宽止损补救。"
    if source == "alpha" or category == "alpha":
        return "Alpha 盈利转弱时先减仓 20%-30%，剩余仓位用移动止盈或近 3 根 K 线低点保护。"
    return "强势延续时先减仓，不直接全平；剩余仓位用移动止盈跟随。"


def _representative_symbols(rows: list[dict], issue_type: str, limit: int = 3) -> list[str]:
    if issue_type == "entry_confirmation":
        ordered = sorted(rows, key=lambda row: float(row.get("net_pnl") or 0))
    else:
        ordered = sorted(rows, key=lambda row: float(row.get("post_mfe") or 0), reverse=True)
    result = []
    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        if symbol and symbol not in result:
            result.append(symbol)
        if len(result) >= limit:
            break
    return result


def _weighted_mean(items: list[dict], field: str) -> float:
    weight = sum(int(item.get("issue_count") or 0) for item in items)
    if weight <= 0:
        return 0.0
    return sum(
        float(item.get(field) or 0) * int(item.get("issue_count") or 0)
        for item in items
    ) / weight


def summarize_trade_lifecycle_reviews(
    reviews: list[dict],
    min_category_samples: int = 8,
    min_issue_samples: int = 3,
    min_issue_rate: float = 0.30,
    limit: int = 5,
) -> list[dict]:
    category_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in reviews:
        label = str(row.get("exit_review_label") or "").lower()
        if label == "pending" or (row.get("post_mfe") is None and row.get("post_mae") is None):
            continue
        key = (row.get("strategy_source") or "unknown", row.get("category") or "unknown")
        category_groups[key].append(row)

    category_candidates = []
    for (source, category), rows in category_groups.items():
        sample_size = len(rows)
        if sample_size < min_category_samples:
            continue
        issues: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            issue_type = _lifecycle_issue_type(row)
            if issue_type:
                issues[issue_type].append(row)

        eligible = []
        for issue_type, issue_rows in issues.items():
            issue_count = len(issue_rows)
            issue_rate = issue_count / sample_size
            if issue_count < min_issue_samples or issue_rate < min_issue_rate:
                continue
            total_pnl = sum(float(row.get("net_pnl") or 0) for row in issue_rows)
            avg_post_mfe = _mean([row.get("post_mfe") for row in issue_rows])
            impact = max(0.0, -total_pnl) if issue_type == "entry_confirmation" else avg_post_mfe * 100
            evidence_score = issue_rate * 100 + issue_count * 3 + min(impact, 20)
            priority = "急需修复" if (
                issue_type == "entry_confirmation" and (total_pnl <= -5 or issue_rate >= 0.50)
            ) else "需要优化"
            eligible.append({
                "strategy_source": source,
                "category": category,
                "categories": [category],
                "sample_size": sample_size,
                "issue_type": issue_type,
                "issue_count": issue_count,
                "issue_rate": issue_rate,
                "total_pnl": total_pnl,
                "category_total_pnl": sum(float(row.get("net_pnl") or 0) for row in rows),
                "avg_pnl_pct": _mean([row.get("pnl_pct") for row in issue_rows]),
                "avg_mfe": _mean([row.get("entry_mfe") for row in issue_rows]),
                "avg_mae": _mean([row.get("entry_mae") for row in issue_rows]),
                "avg_post_mfe": avg_post_mfe,
                "avg_post_mae": _mean([row.get("post_mae") for row in issue_rows]),
                "representative_symbols": _representative_symbols(issue_rows, issue_type),
                "priority": priority,
                "recommendation": _lifecycle_recommendation(source, category, issue_type),
                "evidence_score": evidence_score,
            })
        if eligible:
            category_candidates.append(max(eligible, key=lambda item: item["evidence_score"]))

    merged_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in category_candidates:
        key = (item["strategy_source"], item["issue_type"], item["recommendation"])
        merged_groups[key].append(item)

    result = []
    for (_, issue_type, _), items in merged_groups.items():
        categories = sorted({category for item in items for category in item["categories"]})
        sample_size = sum(int(item["sample_size"]) for item in items)
        issue_count = sum(int(item["issue_count"]) for item in items)
        issue_rate = issue_count / sample_size if sample_size else 0.0
        symbols = []
        for item in items:
            for symbol in item["representative_symbols"]:
                if symbol not in symbols:
                    symbols.append(symbol)
        avg_mfe = _weighted_mean(items, "avg_mfe")
        avg_mae = _weighted_mean(items, "avg_mae")
        avg_post_mfe = _weighted_mean(items, "avg_post_mfe")
        category_text = "、".join(categories)
        if issue_type == "entry_confirmation":
            conclusion = (
                f"{category_text} 有 {issue_count}/{sample_size} 笔开仓后未形成有效浮盈，"
                f"平均最大有利 {avg_mfe:.2%}、最大不利 {avg_mae:.2%}，开仓确认偏早。"
            )
        else:
            conclusion = (
                f"{category_text} 有 {issue_count}/{sample_size} 笔平仓后仍有原方向空间，"
                f"平均后续最大有利 {avg_post_mfe:.2%}，存在平早。"
            )
        result.append({
            "strategy_source": items[0]["strategy_source"],
            "category": category_text,
            "categories": categories,
            "sample_size": sample_size,
            "issue_type": issue_type,
            "issue_count": issue_count,
            "issue_rate": issue_rate,
            "total_pnl": sum(float(item["total_pnl"]) for item in items),
            "category_total_pnl": sum(float(item["category_total_pnl"]) for item in items),
            "avg_pnl_pct": _weighted_mean(items, "avg_pnl_pct"),
            "avg_mfe": avg_mfe,
            "avg_mae": avg_mae,
            "avg_post_mfe": avg_post_mfe,
            "avg_post_mae": _weighted_mean(items, "avg_post_mae"),
            "representative_symbols": symbols[:3],
            "priority": "急需修复" if any(item["priority"] == "急需修复" for item in items) else "需要优化",
            "conclusion": conclusion,
            "recommendation": items[0]["recommendation"],
            "evidence_score": max(float(item["evidence_score"]) for item in items),
        })

    return sorted(
        result,
        key=lambda row: (row["priority"] != "急需修复", -row["evidence_score"], -row["issue_count"]),
    )[:max(0, int(limit))]


def run_policy_review(days: int = 14) -> dict:
    label_decision_outcomes()
    summarize_exit_reviews(window_days=7, recent_limit=30)
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


def fetch_policy_loop_summary(limit: int = 200, include_diagnostics: bool = False) -> dict:
    review_position_trade_entries(limit=max(300, limit))
    summarize_exit_reviews(window_days=7, recent_limit=30)
    entry_summaries = summarize_entry_reviews(recent_limit=30).get("summaries", [])
    lifecycle_reviews = fetch_trade_lifecycle_reviews(limit=100)
    lifecycle_summaries = summarize_trade_lifecycle_reviews(lifecycle_reviews)
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
        category_rows = []
        reviews = []
        if include_diagnostics:
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
        policy_counts = conn.execute(
            """SELECT
                   SUM(CASE WHEN source_type = 'policy_review' THEN 1 ELSE 0 END) AS review_candidates,
                   SUM(CASE WHEN source_type = 'policy_review' AND status = 'active' THEN 1 ELSE 0 END) AS active_candidates,
                   SUM(CASE WHEN source_type != 'policy_review' THEN 1 ELSE 0 END) AS offline_suggestions
               FROM strategy_policy_candidates"""
        ).fetchone()
        active_versions = conn.execute(
            "SELECT COUNT(*) FROM policy_versions WHERE status = 'active'"
        ).fetchone()[0]
        entry_policy = _read_json(ENTRY_POLICY_PATH, {})
        entry_reviews = fetch_entry_reviews(limit=100)
        entry_status = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(review_label <> 'pending') reviewed,
                      SUM(review_label = 'pending') pending,
                      SUM(snapshot_source = 'historical_rebuild') missing_snapshot
               FROM trade_entry_reviews"""
        ).fetchone()
        exit_reviews = fetch_exit_reviews(limit=100)
        exit_summaries = fetch_exit_review_summaries(limit=100)
        result = {
            "generated_at": utc_now(),
            "latest_review_time": latest_review,
            "overview": dict(overview) if overview else {},
            "candidates": candidates,
            "versions": versions,
            "entry_reviews": entry_reviews,
            "entry_summaries": entry_summaries,
            "entry_review_status": {key: int(entry_status[key] or 0) for key in entry_status.keys()},
            "exit_reviews": exit_reviews,
            "exit_summaries": exit_summaries,
            "trade_reviews": lifecycle_reviews,
            "trade_review_summaries": lifecycle_summaries,
            "auto_policy_status": {
                "review_candidates": int(policy_counts["review_candidates"] or 0),
                "active_candidates": int(policy_counts["active_candidates"] or 0),
                "offline_suggestions": int(policy_counts["offline_suggestions"] or 0),
                "active_versions": int(active_versions or 0),
                "entry_rules": len(entry_policy.get("rules") or []),
            },
            "entry_policy": entry_policy,
            "exit_policy": _read_json(EXIT_POLICY_PATH, {}),
        }
        if include_diagnostics:
            result["categories"] = category_rows
            result["reviews"] = reviews
        return result
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
