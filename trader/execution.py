"""Live execution engine."""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from trader.exchange import BinanceFutures
from trader.risk import (
    calculate_position, determine_side, meets_safety_filters,
    calc_tp_levels, calc_trailing_stop, evaluate_entry_policy
)
from trader.entry_profiles import evaluate_profile_entry
from trader.config import EXCHANGE_CONFIG, TRADING_CONFIG
from trader.cooldown_manager import is_in_cooldown, record_stop, record_profit
from trader.market_regime import detect_current_regime, adjust_strategy_for_regime, get_regime_adjustment_message
from trader.selection import BluechipTrendSelector, CandidateSelector  # V5: 鍊欓夐夋嫨鍣?
from trader.symbol_risk import get_symbol_risk
from trader.ai_client import build_learning_action
from alpha_engine.volume_price import evaluate_alpha_volume_price
logger = logging.getLogger("execution")


EXIT_POLICY_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs", "exit_policy.json"))
_EXIT_POLICY_CACHE = {"mtime": None, "data": None}


def _row_to_dict(row):
    """Convert sqlite Row to dict."""
    if isinstance(row, dict):
        return row
    return dict(row)


def _features_for_decision(row):
    return row.get("raw_features") or {
        "trend_state": row.get("trend_state"),
        "trend_direction": row.get("trend_direction"),
        "chip_phase": row.get("chip_phase"),
        "volatility_level": row.get("volatility_level"),
        "price_position": row.get("price_position"),
        "relative_strength": row.get("relative_strength"),
    }


def _score_layer_gate(row, entry_profile):
    raw = row.get("raw_features") or row.get("features") or {}
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    score_layers = raw.get("score_layers") or {}
    layers = score_layers.get("layers") or {}
    thresholds = score_layers.get("thresholds") or {}
    opportunity = float((layers.get("opportunity") or {}).get("score") or 50)
    entry = float((layers.get("entry") or {}).get("score") or 50)
    risk = float((layers.get("risk") or {}).get("score") or 50)
    execution = float((layers.get("execution") or {}).get("score") or 50)
    min_opp = float(thresholds.get("min_opportunity_score") or 0)
    min_entry = float(thresholds.get("min_entry_score") or 0)
    probe_min_entry = float(thresholds.get("probe_min_entry_score") or min_entry)
    max_risk = float(thresholds.get("max_risk_score") or 100)
    min_exec = float(thresholds.get("min_execution_score") or 0)
    status = entry_profile.get("status")
    if opportunity < min_opp:
        return False, f"opportunity_score {opportunity:.1f} < {min_opp:.1f}", score_layers
    if risk > max_risk:
        return False, f"risk_score {risk:.1f} > {max_risk:.1f}", score_layers
    required_entry = probe_min_entry if status == "probe" else min_entry
    if entry < required_entry:
        return False, f"entry_score {entry:.1f} < {required_entry:.1f}", score_layers
    if execution < min_exec:
        return False, f"execution_score {execution:.1f} < {min_exec:.1f}", score_layers
    return True, "OK", score_layers


def _raw_features(row):
    raw = row.get("raw_features") if isinstance(row, dict) else None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _market_phase_gate(raw: dict) -> dict:
    phase = (raw or {}).get("market_phase") or {}
    if not isinstance(phase, dict):
        return {}
    return phase


def _market_phase_entry_decision(market_phase: dict, current_status: str) -> tuple[bool, str, str]:
    phase = str((market_phase or {}).get("phase") or "")
    if phase in {"breakdown_risk", "uncertain"}:
        return False, "blocked", f"market_phase_{phase}"
    if phase == "breakout_pending":
        return True, "probe", "market_phase_breakout_pending_probe"
    return True, current_status, "market_phase_ok"


def _alpha_probe_entry_decision(
    raw_alpha: dict,
    entry_status: str,
    breakout_confirmed: bool,
) -> tuple[bool, str]:
    """Require futures synchronization before any Alpha entry and structure confirmation for probes."""
    raw = raw_alpha or {}
    dual_market = raw.get("dual_market_volume") or {}
    synchronized = bool(
        dual_market.get("synchronized", dual_market.get("sync_confirmed", False))
    )
    status = str(entry_status or "").lower()
    if not synchronized:
        prefix = "alpha_probe" if status == "probe" else "alpha"
        return False, f"{prefix}_futures_volume_not_synchronized"
    if status != "probe":
        return True, "alpha_probe_gate_not_applicable"
    if not breakout_confirmed:
        return False, "alpha_probe_price_structure_not_confirmed"
    phase = str(((raw.get("market_phase") or {}).get("phase") or "")).lower()
    if phase == "breakdown_risk":
        return False, "alpha_probe_market_phase_breakdown_risk"
    return True, "alpha_probe_confirmed"


def _compute_entry_v3_signals(symbol, row):
    try:
        from engine.breakout_detector import check_breakout_confirmation, compute_breakout_metrics, compute_rr_detail

        raw = _raw_features(row)
        tech = raw.get("technical") or {}
        price = float(row.get("market_price") or row.get("price") or 0)
        atr = float(tech.get("atr") or 0)
        breakout_ok, breakout_reason = check_breakout_confirmation(symbol)
        metrics = compute_breakout_metrics(symbol)
        rr = compute_rr_detail(symbol, price, atr) if price > 0 and atr > 0 else {"rr_used": 0, "rr_atr": 0, "rr_structure": 0, "rr_method": "none"}
        in_cooldown, cooldown_reason, remaining = is_in_cooldown(symbol)
        return {
            "breakout": {
                "ok": breakout_ok,
                "reason": breakout_reason,
                "volume_ratio": round(metrics.get("volume_ratio", 0), 2),
                "volume_source": metrics.get("volume_source"),
                "breakout_level": metrics.get("breakout_level") or metrics.get("high_price"),
                "current_price": metrics.get("current_price"),
                "distance_to_breakout_pct": metrics.get("distance_to_breakout_pct"),
                "last_closed_time": metrics.get("last_closed_time"),
            },
            "rr": rr,
            "rr_ratio": round(float(rr.get("rr_used") or 0), 2),
            "cooldown": {"in_cooldown": in_cooldown, "reason": cooldown_reason, "remaining_sec": remaining},
            "atr": round(atr, 4),
        }
    except Exception as e:
        return {"error": str(e)}


def _bluechip_cfg():
    return TRADING_CONFIG.get("bluechip_trend") or {}


def _load_exit_policy() -> dict:
    if not os.path.exists(EXIT_POLICY_PATH):
        return {"default_class": "narrative", "classes": {}}
    try:
        mtime = os.path.getmtime(EXIT_POLICY_PATH)
        if _EXIT_POLICY_CACHE["mtime"] == mtime and _EXIT_POLICY_CACHE["data"] is not None:
            return _EXIT_POLICY_CACHE["data"]
        with open(EXIT_POLICY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _EXIT_POLICY_CACHE["mtime"] = mtime
        _EXIT_POLICY_CACHE["data"] = data
        return data
    except Exception as e:
        logger.warning("exit policy unavailable: %s", e)
        return {"default_class": "narrative", "classes": {}}


def _exit_policy_for_symbol(symbol: str, strategy_source: str | None = None) -> tuple[str, dict]:
    if strategy_source == "alpha":
        key = "alpha"
    else:
        risk = get_symbol_risk(symbol)
        key = risk.get("class") or "narrative"
    policy = _load_exit_policy()
    classes = policy.get("classes") or {}
    default_key = policy.get("default_class") or "narrative"
    return key, dict(classes.get(key) or classes.get(default_key) or {})


def _recent_momentum_exit_count(symbol: str, class_key: str, limit: int = 2) -> int:
    try:
        from shared.db import get_conn

        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT filter_reason
                   FROM strategy_decisions
                   WHERE symbol = ?
                     AND decision_stage = 'position_management'
                     AND decision_result IN ('planned_partial_close', 'planned_close')
                   ORDER BY id DESC
                   LIMIT ?""",
                (symbol, limit),
            ).fetchall()
        finally:
            conn.close()
        needle = f"category_momentum_reversal class={class_key}"
        return sum(1 for r in rows if needle in str(r["filter_reason"] or ""))
    except Exception:
        return 0


def _normal_soft_trend_state(side: str, mark_price: float, tech: dict) -> str:
    side_u = str(side or "LONG").upper()
    mark = float(mark_price or 0)
    ema20 = float((tech or {}).get("ema20") or 0)
    slope = float((tech or {}).get("ema20_slope") or 0)
    ema_ratio = float((tech or {}).get("ema20_50_ratio") or 1.0)
    ret_6h = float((tech or {}).get("return_6h") or 0)
    ret_24h = float((tech or {}).get("return_24h") or 0)
    if mark <= 0 or ema20 <= 0:
        return "ambiguous"

    if side_u == "LONG":
        strong = mark > ema20 and slope > 0 and ema_ratio >= 1.0 and not (ret_6h < 0 and ret_24h < 0)
        weak = (mark < ema20 and slope < 0) or (ret_6h < 0 and ret_24h < 0)
    else:
        strong = mark < ema20 and slope < 0 and ema_ratio <= 1.0 and not (ret_6h > 0 and ret_24h > 0)
        weak = (mark > ema20 and slope > 0) or (ret_6h > 0 and ret_24h > 0)
    if strong:
        return "strong"
    if weak:
        return "weak"
    return "ambiguous"


def _normal_soft_exit_in_cooldown(symbol: str, minutes: float = 60) -> bool:
    try:
        from shared.db import get_conn

        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT 1
                   FROM strategy_decisions
                   WHERE symbol = ?
                     AND decision_stage = 'position_management'
                     AND decision_result = 'planned_partial_close'
                     AND datetime(time) >= datetime('now', ?)
                     AND (
                         filter_reason LIKE 'normal_soft_exit %'
                         OR filter_reason LIKE 'hold_alpha_weak_profit_protect%'
                         OR filter_reason LIKE 'score_decay%'
                         OR filter_reason LIKE 'category_momentum_reversal%'
                     )
                   LIMIT 1""",
                (symbol, f"-{float(minutes):g} minutes"),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _bluechip_tp_levels(entry_price: float, side: str, stop_pct: float | None = None) -> dict:
    cfg = _bluechip_cfg()
    tp1_pct = float(stop_pct or cfg.get("tp1_target_pct", 0.035))
    tp2_pct = tp1_pct * 2
    if side == "LONG":
        tp1 = entry_price * (1 + tp1_pct)
        tp2 = entry_price * (1 + tp2_pct)
    else:
        tp1 = entry_price * (1 - tp1_pct)
        tp2 = entry_price * (1 - tp2_pct)
    return {
        "tp1_price": round(tp1, 8),
        "tp2_price": round(tp2, 8),
        "tp1_qty_pct": float(cfg.get("tp1_pct", 0.50)),
        "tp2_qty_pct": float(cfg.get("tp2_pct", 0.30)),
        "trail_trigger_atr": float(TRADING_CONFIG.get("trailing_stop_atr_multiplier", 1.5)),
    }


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _fetch_closed_futures_15m(symbol: str, limit: int = 16, now: datetime | None = None) -> list[dict]:
    """Return recent fully closed 15-minute futures candles in time order."""
    from shared.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT time, low, high, close
               FROM futures_candles_15m
               WHERE symbol = ?
               ORDER BY time DESC
               LIMIT ?""",
            (symbol, max(int(limit), 4)),
        ).fetchall()
    finally:
        conn.close()

    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(minutes=15)
    closed = []
    for row in rows:
        item = _row_to_dict(row)
        candle_time = _parse_time(item.get("time"))
        if candle_time and candle_time.astimezone(timezone.utc) <= cutoff:
            closed.append(item)
    closed.sort(key=lambda item: _parse_time(item.get("time")) or datetime.min.replace(tzinfo=timezone.utc))
    return closed[-limit:]


def _latest_alpha_soft_exit_confirmation(symbol: str, position_id: str) -> dict | None:
    """Load the latest soft-exit confirmation marker for one position lifecycle."""
    from shared.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT filter_reason, reason_json
               FROM strategy_decisions
               WHERE symbol = ?
                 AND decision_stage = 'position_management'
                 AND filter_reason LIKE 'alpha_soft_exit_%'
               ORDER BY id DESC
               LIMIT 30""",
            (symbol,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            state = json.loads(row["reason_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if str(state.get("position_id") or "") == str(position_id):
            return state
    return None


def _age_hours(value):
    dt = _parse_time(value)
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600)


def _latest_by_symbol(rows):
    latest = {}
    for row in rows:
        item = _row_to_dict(row)
        latest[item.get("symbol")] = item
    return latest


def _signal_age_minutes(row):
    dt = _parse_time(row.get("time") or row.get("scan_time") or row.get("update_time"))
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60)


def _alpha_cfg():
    return TRADING_CONFIG.get("alpha_trading") or {}


def _runtime_controls():
    try:
        from shared.db import get_trading_runtime_controls

        return get_trading_runtime_controls()
    except Exception as e:
        logger.warning(f"trading runtime controls unavailable: {e}")
        return {"normal_trading_enabled": True, "alpha_trading_enabled": False}


def _write_alpha_post_close_cooldown(symbol, pnl=0.0, reason="", is_stop=False, prefix="alpha close"):
    """Prevent immediate alpha re-entry after a position has just exited."""
    cfg = _alpha_cfg()
    pnl = float(pnl or 0)
    reason_text = str(reason or "")
    stop_like = bool(is_stop) or "stop" in reason_text.lower() or "止损" in reason_text
    if stop_like:
        cooldown_type = "stop"
        minutes = int(cfg.get("stop_cooldown_minutes", 180))
        loss_count = 1
    elif pnl < 0:
        cooldown_type = "loss"
        minutes = int(cfg.get("loss_cooldown_minutes", 120))
        loss_count = 1
    else:
        cooldown_type = "post_close"
        minutes = int(cfg.get("post_close_cooldown_minutes", 45))
        loss_count = 0
    try:
        from shared.db import set_alpha_cooldown

        set_alpha_cooldown(
            symbol,
            cooldown_type,
            f"{prefix}: pnl={pnl:.2f}; {reason_text}"[:240],
            minutes,
            loss_count=loss_count,
        )
        logger.info("    Alpha cooldown: %s %s %sm", symbol, cooldown_type, minutes)
    except Exception as e:
        logger.warning(f"    alpha cooldown write failed: {e}")


def _price_return_pct(side, entry_price, mark_price):
    entry = float(entry_price or 0)
    mark = float(mark_price or 0)
    if entry <= 0 or mark <= 0:
        return 0.0
    raw = (mark - entry) / entry
    return -raw if str(side or "").upper() == "SHORT" else raw


def _promote_confirmed_alpha_probe(volume_price: dict, raw_alpha: dict, breakout_confirmed: bool) -> dict:
    """Allow a 68-72 trend probe only after independent market confirmation."""
    if volume_price.get("allow_long") or not breakout_confirmed:
        return volume_price
    alpha_trend = raw_alpha.get("alpha_trend") or {}
    volume = raw_alpha.get("volume") or {}
    futures = raw_alpha.get("futures_sync") or {}
    trend_score = float(alpha_trend.get("trend_continuation_score") or 0)
    alpha_volume = float(volume.get("alpha_volume_growth_6h") or 0)
    futures_volume = float(futures.get("futures_volume_growth_6h") or 0)
    oi4 = float(futures.get("oi_change_4h") or 0)
    oi24 = float(futures.get("oi_change_24h") or 0)
    funding = abs(float(futures.get("funding_rate") or 0))
    oi_confirmed = oi4 >= 0 and oi24 >= -0.01
    oi_waiver = -0.005 <= oi4 < 0 and alpha_volume >= 3.0 and futures_volume >= 2.0
    confirmed = (
        68 <= trend_score < 72
        and alpha_volume >= 1.8
        and bool(futures.get("available"))
        and futures_volume >= 1.5
        and (oi_confirmed or oi_waiver)
        and funding <= 0.0015
    )
    if not confirmed:
        return volume_price
    promoted = dict(volume_price)
    promoted.update({
        "state": "alpha_trend_probe_confirmed_15m",
        "action": "normal_review_probe",
        "allow_long": True,
        "allow_short": False,
        "max_position_factor": 0.12,
        "reasons": list(volume_price.get("reasons") or [])
        + [f"trend {trend_score:.1f} confirmed by dual market volume and closed 15m breakout hold"],
    })
    metrics = dict(promoted.get("metrics") or {})
    metrics["trend_score"] = round(trend_score, 2)
    metrics["breakout_15m_confirmed"] = True
    promoted["metrics"] = metrics
    return promoted


def _position_r_state(side, entry_price, mark_price, hist, atr, highest_price=None, lowest_price=None):
    stop_pct = float((hist or {}).get("stop_pct") or 0)
    entry = float(entry_price or 0)
    if stop_pct <= 0 and entry > 0:
        initial_stop = float((hist or {}).get("initial_stop_loss") or 0)
        if initial_stop > 0:
            stop_pct = abs(entry - initial_stop) / entry
    if stop_pct <= 0:
        stop_pct = 0.10 if (hist or {}).get("strategy_source") == "alpha" else 0.12
    if stop_pct <= 0:
        return {"r_multiple": 0.0, "stop_pct": 0.0, "trailing_enabled": False}
    side_u = str(side or "LONG").upper()
    mark = float(mark_price or 0)
    price_ret = _price_return_pct(side_u, entry, mark)
    r_multiple = price_ret / stop_pct if stop_pct > 0 else 0.0
    atr_v = float(atr or (entry * stop_pct / 2 if entry else 0))
    trail_mult = float((hist or {}).get("trailing_atr_multiplier") or (2.0 if (hist or {}).get("strategy_source") == "alpha" else 1.5))
    current_stop = float((hist or {}).get("current_stop_loss") or (hist or {}).get("initial_stop_loss") or 0)
    trailing_price = None
    trailing_enabled = False

    if entry <= 0:
        return {"r_multiple": r_multiple, "stop_pct": stop_pct, "trailing_enabled": False}

    if side_u == "LONG":
        if r_multiple >= 1:
            current_stop = max(current_stop or 0, entry * 1.002)
        if r_multiple >= 2 or int((hist or {}).get("roll_layer") or 0) >= 1:
            current_stop = max(current_stop or 0, entry * (1 + stop_pct))
            high = float(highest_price or mark or entry)
            trailing_price = high - atr_v * trail_mult
            current_stop = max(current_stop or 0, trailing_price)
            trailing_enabled = True
        stop_triggered = current_stop > 0 and mark <= current_stop and r_multiple > 0
    else:
        if r_multiple >= 1:
            current_stop = min(current_stop or entry * 10, entry * 0.998)
        if r_multiple >= 2 or int((hist or {}).get("roll_layer") or 0) >= 1:
            current_stop = min(current_stop or entry * 10, entry * (1 - stop_pct))
            low = float(lowest_price or mark or entry)
            trailing_price = low + atr_v * trail_mult
            current_stop = min(current_stop or entry * 10, trailing_price)
            trailing_enabled = True
        stop_triggered = current_stop > 0 and mark >= current_stop and r_multiple > 0

    return {
        "r_multiple": r_multiple,
        "stop_pct": stop_pct,
        "current_stop_loss": current_stop or None,
        "trailing_stop_price": trailing_price,
        "trailing_enabled": trailing_enabled,
        "stop_triggered": stop_triggered,
        "trail_mult": trail_mult,
    }


def _json_or_empty(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _evaluate_alpha_breakout_bars(rows):
    """Require a closed 15m breakout bar followed by a holding confirmation."""
    bars = [dict(row) for row in (rows or [])]
    if len(bars) < 6:
        return False, "15m breakout confirmation data insufficient", {"bars": len(bars)}
    prior = bars[-6:-2]
    breakout = bars[-2]
    confirmation = bars[-1]
    breakout_level = max(float(bar.get("high") or 0) for bar in prior)
    prior_avg_volume = sum(float(bar.get("quote_vol") or 0) for bar in prior) / len(prior)
    breakout_close = float(breakout.get("close") or 0)
    confirmation_close = float(confirmation.get("close") or 0)
    confirmation_volume = float(confirmation.get("quote_vol") or 0)
    details = {
        "breakout_level": breakout_level,
        "breakout_close": breakout_close,
        "confirmation_close": confirmation_close,
        "confirmation_quote_vol": confirmation_volume,
        "prior_avg_quote_vol": prior_avg_volume,
        "breakout_time": breakout.get("time"),
        "confirmation_time": confirmation.get("time"),
    }
    if breakout_close <= breakout_level:
        return False, f"15m breakout not confirmed: close {breakout_close:.8g} <= level {breakout_level:.8g}", details
    if confirmation_close < breakout_level:
        return False, f"15m breakout failed to hold: close {confirmation_close:.8g} < level {breakout_level:.8g}", details
    if confirmation_volume < prior_avg_volume:
        return False, f"15m confirmation volume weak: {confirmation_volume:.0f} < avg {prior_avg_volume:.0f}", details
    return True, "15m breakout and hold confirmed", details


def _check_alpha_futures_breakout_confirmation(symbol, now=None):
    from shared.db import get_conn

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket = current.replace(minute=(current.minute // 15) * 15, second=0, microsecond=0)
    latest_closed_open = bucket - timedelta(minutes=15)
    cutoff = latest_closed_open.isoformat().replace("+00:00", "Z")
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT time, high, close, quote_vol
            FROM futures_candles_15m
            WHERE symbol = ? AND time <= ?
            ORDER BY time DESC
            LIMIT 6
            """,
            (symbol, cutoff),
        ).fetchall()
    finally:
        conn.close()
    return _evaluate_alpha_breakout_bars(list(reversed(rows)))


def _json_or_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _grade_from_score(score):
    score = float(score or 0)
    if score >= 80:
        return "S"
    if score >= 70:
        return "A"
    if score >= 60:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _adapter_quality(row):
    raw = _json_or_empty(row.get("raw_features"))
    missing = []
    quality = 95.0
    for section in ("technical", "futures", "depth", "score_layers"):
        if not raw.get(section):
            missing.append(section)
            quality -= 12.0
    technical = raw.get("technical") or {}
    futures = raw.get("futures") or {}
    for field in ("current_price", "atr", "volume_change_pct", "return_6h", "return_24h"):
        if technical.get(field) is None:
            missing.append(f"technical.{field}")
            quality -= 3.0
    for field in ("oi_change_pct", "funding_rate"):
        if futures.get(field) is None:
            missing.append(f"futures.{field}")
            quality -= 4.0
    return max(0.0, min(100.0, quality)), missing


def _adapt_alpha_to_normal_row(alpha_row):
    raw_alpha = _raw_features(alpha_row)
    returns = raw_alpha.get("returns") or {}
    volume = raw_alpha.get("volume") or {}
    depth = raw_alpha.get("depth") or {}
    price = float(alpha_row.get("market_price") or 0)
    discovery = float(alpha_row.get("alpha_score") or alpha_row.get("discovery_score") or 0)
    alpha_bonus = max(0.0, min(5.0, (discovery - 60.0) / 8.0))
    ret_6h = float(returns.get("ret_6h") or 0) / 100
    pct_24h = float(returns.get("pct_24h") or 0) / 100
    volume_growth = float(volume.get("volume_growth_6h") or 1.0)
    volume_change_pct = max(-0.5, min(1.5, volume_growth - 1.0))
    conservative_score = max(40.0, min(58.0, 50.0 + alpha_bonus + max(-4.0, min(4.0, ret_6h * 40))))
    technical = {
        "current_price": price,
        "atr": price * 0.03 if price > 0 else 0,
        "return_6h": ret_6h,
        "return_24h": pct_24h,
        "price_change_24h": pct_24h,
        "volume_change_pct": volume_change_pct,
        "vol_quality_score": 50,
    }
    futures = {
        "oi_change_pct": 0,
        "funding_rate": 0,
        "open_interest": 0,
    }
    normal_raw = {
        "alpha_context": {
            "alpha_symbol": alpha_row.get("alpha_symbol"),
            "alpha_profile": alpha_row.get("alpha_profile"),
            "discovery_score": discovery,
            "source": "alpha_adapter_fallback",
        },
        "technical": technical,
        "futures": futures,
        "depth": {
            "depth_ratio_score": 50,
            "big_order_score": 50,
            "spread_pct": float(depth.get("spread_pct") or 99),
        },
        "score_layers": {
            "display_score": conservative_score,
            "source": "alpha_adapter_fallback_conservative",
        },
    }
    return {
        "symbol": alpha_row.get("futures_symbol"),
        "time": alpha_row.get("time"),
        "scan_id": alpha_row.get("scan_id"),
        "composite_score": conservative_score,
        "composite_summary": _grade_from_score(conservative_score),
        "risk_label": "alpha_adapter_conservative",
        "chip_phase": "未知",
        "trend_state": "alpha候选",
        "trend_direction": "横盘",
        "volatility_level": "偏高",
        "price_position": "未知",
        "relative_strength": 50,
        "market_price": price,
        "entry_alpha": min(58.0, conservative_score),
        "hold_alpha": min(58.0, conservative_score),
        "raw_features": normal_raw,
        "adapter_fallback": True,
    }


class ExecutionEngine:
    def __init__(self, exchange: BinanceFutures):
        self.ex = exchange
        self.cfg = TRADING_CONFIG
        self.account_controls = None
        self._trading_symbols = None
        self.ai_learning_actions = []
        # V3.1: 绠鍖栫殑鎸佷粨璺熻釜
        # {symbol: {"highest_price": float, "entry_price": float}}
        self._pos_tracker = {}

    def get_current_positions(self) -> list:
        return self.ex.get_positions()

    def get_balance(self) -> float:
        return self.ex.get_balance()

    def _get_trading_symbols(self) -> set:
        if self._trading_symbols is None:
            self._trading_symbols = self.ex.get_trading_symbols()
            logger.info(f"鍙氦鏄撳竵绉? {len(self._trading_symbols)}")
        return self._trading_symbols

    def _sync_tracker(self, positions: list):
        """Sync local position tracker."""
        for p in positions:
            sym = p["symbol"]
            if sym not in self._pos_tracker:
                self._pos_tracker[sym] = {
                    "highest_price": p["mark_price"],
                    "entry_price": p["entry_price"],
                }
            else:
                # 鏇存柊鏈楂樹环
                self._pos_tracker[sym]["highest_price"] = max(
                    self._pos_tracker[sym]["highest_price"], p["mark_price"]
                )

    def _sync_tracker(self, positions: list):
        from shared.db import get_position_history, update_position_management

        for p in positions:
            sym = p["symbol"]
            hist = get_position_history(sym) or {}
            stored_high = float(hist.get("highest_price") or 0)
            stored_low = float(hist.get("lowest_price") or 0)
            mark_price = float(p.get("mark_price") or 0)
            current_high = max(stored_high, float(p.get("mark_price") or 0))
            current_low = min([v for v in (stored_low, mark_price) if v > 0], default=mark_price)
            if sym in self._pos_tracker:
                current_high = max(current_high, float(self._pos_tracker[sym].get("highest_price") or 0))
                tracked_low = float(self._pos_tracker[sym].get("lowest_price") or 0)
                if tracked_low > 0:
                    current_low = min(current_low, tracked_low)
            self._pos_tracker[sym] = {
                "highest_price": current_high,
                "lowest_price": current_low,
                "entry_price": p["entry_price"],
            }
            update_position_management(sym, highest_price=current_high, lowest_price=current_low, quantity=p.get("quantity"))

    def _record_decision(self, row_or_symbol, run_id=None, **kwargs):
        try:
            from shared.db import record_strategy_decision

            row = row_or_symbol if isinstance(row_or_symbol, dict) else {"symbol": row_or_symbol}
            symbol = row.get("symbol")
            stage = kwargs.get("decision_stage")
            result = kwargs.get("decision_result")
            decision_id = kwargs.pop("decision_id", None)
            scan_id = kwargs.pop("scan_id", row.get("scan_id"))
            price = kwargs.pop("price", row.get("price") or row.get("market_price"))
            entry_price = kwargs.pop("entry_price", None)
            if not decision_id and run_id and symbol and stage and result:
                decision_id = f"{run_id}:{stage}:{result}:{symbol}"
            record_strategy_decision(
                symbol=symbol,
                scan_id=scan_id,
                run_id=run_id,
                decision_id=decision_id,
                side=kwargs.pop("side", "SKIP"),
                composite_score=row.get("composite_score"),
                grade=row.get("composite_summary") or row.get("grade"),
                price=price,
                entry_price=entry_price,
                features=_features_for_decision(row),
                **kwargs,
            )
        except Exception as e:
            logger.debug(f"strategy decision log failed: {e}")

    def _latest_alpha_position_context(self, symbol: str, hist: dict) -> dict | None:
        try:
            from shared.db import fetch_latest_alpha_position_context

            return fetch_latest_alpha_position_context(
                symbol=symbol,
                alpha_symbol=hist.get("alpha_symbol"),
            )
        except Exception as e:
            logger.warning(f"Alpha position context unavailable for {symbol}: {e}")
            return None

    def _build_alpha_position_action(
        self,
        pos: dict,
        hist: dict,
        pnl_pct: float,
        mark_price: float,
        close_side: str,
        highest_price: float,
        atr: float,
        age_h: float | None,
        run_id: str | None = None,
    ) -> dict | None:
        sym = pos["symbol"]
        side = pos.get("side")
        ctx = self._latest_alpha_position_context(sym, hist)
        entry_score = float(hist.get("alpha_score") or hist.get("entry_score") or 0)

        def add(reason, is_stop=False, score=None, action="close", close_pct=None):
            item = {
                "action": action,
                "symbol": sym,
                "side": close_side,
                "reason": reason,
                "close_price": mark_price,
                "score": float(score if score is not None else entry_score),
                "strategy_source": "alpha",
                "signal_source": hist.get("signal_source"),
                "alpha_symbol": hist.get("alpha_symbol"),
                "alpha_profile": hist.get("alpha_profile"),
                "alpha_entry_level": hist.get("alpha_entry_level"),
                "alpha_score": entry_score,
                "alpha_suggested_position_pct": hist.get("alpha_suggested_position_pct"),
            }
            if close_pct is not None:
                item["close_pct"] = close_pct
            if is_stop:
                item["is_stop"] = True
            return item

        alpha_cfg = _alpha_cfg() or {}
        alpha_hard_stop_pct = float(alpha_cfg.get("position_hard_stop_pct", 0.10)) * 100
        if pnl_pct <= -alpha_hard_stop_pct:
            return add(
                f"margin_hard_stop roi={pnl_pct:.2f}% threshold=-{alpha_hard_stop_pct:.2f}%",
                is_stop=True,
            )
        r_state = _position_r_state(side, pos.get("entry_price"), mark_price, hist, atr, highest_price=highest_price)
        try:
            from shared.db import update_position_management

            update_position_management(
                sym,
                stop_model=hist.get("stop_model") or "atr_clamped_legacy_default",
                stop_pct=r_state.get("stop_pct"),
                current_stop_loss=r_state.get("current_stop_loss"),
                trailing_stop_price=r_state.get("trailing_stop_price"),
                trailing_enabled=1 if r_state.get("trailing_enabled") else 0,
                r_multiple=round(float(r_state.get("r_multiple") or 0), 3),
            )
        except Exception:
            pass
        if r_state.get("stop_triggered"):
            return add(
                f"alpha_trailing_stop r={float(r_state.get('r_multiple') or 0):.2f} stop={float(r_state.get('current_stop_loss') or 0):.4f}",
                score=entry_score,
            )
        if float(r_state.get("r_multiple") or 0) >= 2 and not int(hist.get("tp2_hit") or 0):
            return add("alpha_TP2 r>=2", score=entry_score, action="partial_close", close_pct=0.25)
        if float(r_state.get("r_multiple") or 0) >= 1 and not int(hist.get("tp1_hit") or 0):
            return add("alpha_TP1 r>=1", score=entry_score, action="partial_close", close_pct=0.20)

        hold_reason = None
        if not ctx:
            hold_reason = "alpha position hold: latest alpha volume-price context unavailable"
        else:
            context_age = _signal_age_minutes(ctx)
            max_age = float((_alpha_cfg() or {}).get("position_context_ttl_minutes", 30))
            if max_age and context_age is not None and context_age > max_age:
                hold_reason = f"alpha position hold: volume-price context stale age={context_age:.1f}m"

        if hold_reason:
            self._record_decision(
                {
                    "symbol": sym,
                    "composite_score": entry_score,
                    "grade": _grade_from_score(entry_score),
                    "raw_features": {
                        "alpha_position": {
                            "strategy_source": "alpha",
                            "alpha_symbol": hist.get("alpha_symbol"),
                            "alpha_score": entry_score,
                            "data_state": "missing_or_stale",
                        }
                    },
                },
                run_id=run_id,
                side=side,
                mode="alpha_live",
                decision_stage="position_management",
                decision_result="hold",
                filter_reason=hold_reason,
                reason={"reason": hold_reason, "pnl_pct": pnl_pct},
            )
            return None

        current_score = float(ctx.get("alpha_score") or entry_score)
        vp_state = str(ctx.get("volume_price_state") or "").lower()
        vp_action = str(ctx.get("volume_price_action") or "").lower()
        metrics = _json_or_empty(ctx.get("volume_price_metrics_json"))
        reasons = _json_or_list(ctx.get("volume_price_reasons_json"))
        ret_15m = float(metrics.get("ret_15m") or 0)
        ret_1h = float(metrics.get("ret_1h") or 0)
        ret_6h = float(metrics.get("ret_6h") or 0)
        spread_pct = float(metrics.get("spread_pct") or 0)
        trend_score = float(metrics.get("trend_score") or metrics.get("trend_continuation_score") or 0)
        trend_state = str(metrics.get("trend_state") or "").lower()
        volume_regime = str(metrics.get("volume_regime") or "").lower()
        max_spread_pct = float(alpha_cfg.get("max_spread_pct", 0.0012)) * 100

        weak_states = {"failed_breakout", "distribution", "dumping", "breakdown", "distribution_risk_long_only", "breakdown_volume_long_only"}
        profit_protect_pct = float(alpha_cfg.get("position_soft_exit_profit_pct", 2.0))
        profit_protect_close_pct = float(alpha_cfg.get("position_profit_protect_close_pct", 0.25))
        alpha_volume_regime_close_pct = min(max(profit_protect_close_pct, 0.20), 0.30)
        soft_hold_reason = None
        if vp_state in weak_states:
            if pnl_pct >= profit_protect_pct:
                return add(f"alpha_volume_price_profit_protect state={vp_state} pnl={pnl_pct:.1f}%", score=current_score, action="partial_close", close_pct=profit_protect_close_pct)
            soft_hold_reason = f"alpha soft hold: volume_price={vp_state} pnl={pnl_pct:.1f}%"
        if trend_score and trend_score < float(alpha_cfg.get("position_min_trend_score", 50)):
            if pnl_pct >= profit_protect_pct:
                return add(f"alpha_trend_profit_protect trend={trend_score:.1f} state={trend_state or '-'} pnl={pnl_pct:.1f}%", score=current_score, action="partial_close", close_pct=profit_protect_close_pct)
            soft_hold_reason = f"alpha soft hold: trend_score_fade trend={trend_score:.1f} pnl={pnl_pct:.1f}%"
        if volume_regime in {"suspicious", "overheated", "extreme"}:
            regime_rank = {"suspicious": 1, "overheated": 2, "extreme": 3}
            protected_regime = str(hist.get("alpha_volume_protect_regime") or "").lower()
            already_protected = regime_rank.get(protected_regime, 0) >= regime_rank[volume_regime]
            if pnl_pct > 0 and not already_protected:
                return add(f"alpha_volume_regime_profit_protect regime={volume_regime} pnl={pnl_pct:.1f}%", score=current_score, action="partial_close", close_pct=alpha_volume_regime_close_pct)
            if already_protected:
                soft_hold_reason = f"alpha soft hold: volume_regime={volume_regime} already protected={protected_regime}"
            else:
                soft_hold_reason = f"alpha soft hold: volume_regime={volume_regime} pnl={pnl_pct:.1f}%"
        elif hist.get("alpha_volume_protect_regime"):
            try:
                from shared.db import update_position_management

                update_position_management(
                    sym,
                    alpha_volume_protect_regime=None,
                    alpha_volume_protect_time=None,
                )
                hist["alpha_volume_protect_regime"] = None
                hist["alpha_volume_protect_time"] = None
            except Exception as e:
                logger.warning("Alpha volume protection reset failed for %s: %s", sym, e)
        if hist.get("alpha_entry_level") == "probe" and age_h is not None:
            probe_timeout_h = float(alpha_cfg.get("position_probe_timeout_hours", 1.0))
            probe_min_progress = float(alpha_cfg.get("position_probe_min_progress_pct", 3.0))
            if age_h >= probe_timeout_h and pnl_pct < probe_min_progress:
                soft_hold_reason = f"alpha soft hold: probe_timeout age={age_h:.1f}h pnl={pnl_pct:.1f}% trend={trend_score:.1f}"
        if vp_action in {"observe", "cooldown"} and pnl_pct <= 0 and (ret_15m < 0 or ret_1h < 0):
            soft_hold_reason = f"alpha soft hold: volume_price_weak action={vp_action} state={vp_state} pnl={pnl_pct:.1f}%"
        if spread_pct > max_spread_pct and pnl_pct <= 0:
            soft_hold_reason = f"alpha soft hold: spread_widened spread={spread_pct:.3f}% pnl={pnl_pct:.1f}%"
        if side == "LONG" and ret_15m < 0 and ret_1h < 0 and ret_6h < 0:
            if pnl_pct >= profit_protect_pct:
                return add(f"alpha_long_momentum_profit_protect ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}%", score=current_score, action="partial_close", close_pct=profit_protect_close_pct)
            soft_hold_reason = f"alpha soft hold: long_momentum_reversal ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}% pnl={pnl_pct:.1f}%"
        if side == "SHORT" and ret_15m > 0 and ret_1h > 0 and ret_6h > 0:
            if pnl_pct >= profit_protect_pct:
                return add(f"alpha_short_momentum_profit_protect ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}%", score=current_score, action="partial_close", close_pct=profit_protect_close_pct)
            soft_hold_reason = f"alpha soft hold: short_momentum_reversal ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}% pnl={pnl_pct:.1f}%"

        structural_states = {"breakdown", "breakdown_volume_long_only", "dumping"}
        if (
            side == "LONG"
            and vp_state in structural_states
            and trend_score <= 45
            and ret_1h <= -3
            and ret_6h <= -8
        ):
            soft_hold_reason = (
                f"alpha soft hold: structural_breakdown state={vp_state} trend={trend_score:.1f} "
                f"ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}% pnl={pnl_pct:.1f}%"
            )

        time_stop_h = float(self.cfg.get("time_stop_hours", 12))
        min_ret = float(self.cfg.get("time_stop_min_return", 0.02)) * 100
        if age_h is not None and age_h >= time_stop_h and pnl_pct < min_ret and vp_action not in {"normal_review", "normal_review_probe"}:
            soft_hold_reason = f"alpha soft hold: time_stop age={age_h:.1f}h pnl={pnl_pct:.1f}% state={vp_state}"
        if pnl_pct >= float(self.cfg.get("tp2_target_pct", 0.10)) * 100 and calc_trailing_stop(mark_price, highest_price, atr, self.cfg.get("trailing_stop_atr_multiplier", 1.5)):
            return add(f"alpha_trailing_stop high={highest_price:.4f} now={mark_price:.4f}", score=current_score)

        confirmation_id = str(
            hist.get("position_id")
            or f"{sym}:{side}:{float(pos.get('entry_price') or 0):.12g}:{hist.get('entry_time') or '-'}"
        )
        confirmation_state = _latest_alpha_soft_exit_confirmation(sym, confirmation_id)
        confirmation_status = str((confirmation_state or {}).get("status") or "")

        def record_confirmation(status: str, filter_reason: str, **details):
            state = {
                "status": status,
                "position_id": confirmation_id,
                "soft_reason": soft_hold_reason,
                "pnl_pct": pnl_pct,
                **details,
            }
            self._record_decision(
                {
                    "symbol": sym,
                    "composite_score": current_score,
                    "grade": _grade_from_score(current_score),
                    "raw_features": {
                        "alpha_position": {
                            "strategy_source": "alpha",
                            "alpha_symbol": hist.get("alpha_symbol"),
                            "entry_alpha_score": entry_score,
                            "current_alpha_score": current_score,
                            "volume_price_state": vp_state,
                            "volume_price_action": vp_action,
                            "volume_price_metrics": metrics,
                        }
                    },
                },
                run_id=run_id,
                side=side,
                mode="alpha_live",
                decision_stage="position_management",
                decision_result="hold",
                filter_reason=filter_reason,
                entry_price=pos.get("entry_price"),
                reason=state,
            )

        if confirmation_status in {"pending", "waiting"}:
            record_confirmation(
                "cancelled",
                "alpha_soft_exit_cancelled loss_soft_exit_disabled",
                trigger_candle_time=confirmation_state.get("trigger_candle_time"),
                trigger_low=confirmation_state.get("trigger_low"),
                trigger_high=confirmation_state.get("trigger_high"),
            )
            return None

        hold_reason = soft_hold_reason or f"alpha hold volume_price={vp_state or '-'} action={vp_action or '-'} score={current_score:.1f}"
        if reasons:
            hold_reason += f" reason={'; '.join(map(str, reasons[:2]))}"
        self._record_decision(
            {
                "symbol": sym,
                "composite_score": current_score,
                "grade": _grade_from_score(current_score),
                "raw_features": {
                    "alpha_position": {
                        "strategy_source": "alpha",
                        "alpha_symbol": hist.get("alpha_symbol"),
                        "entry_alpha_score": entry_score,
                        "current_alpha_score": current_score,
                        "volume_price_state": vp_state,
                        "volume_price_action": vp_action,
                        "volume_price_metrics": metrics,
                    }
                },
            },
            run_id=run_id,
            side=side,
            mode="alpha_live",
            decision_stage="position_management",
            decision_result="hold",
            filter_reason=hold_reason,
            reason={"reason": hold_reason, "pnl_pct": pnl_pct},
        )
        return None

    def _build_category_momentum_exit(
        self,
        sym: str,
        side: str,
        close_side: str,
        mark_price: float,
        pnl_pct: float,
        hold_alpha: float,
        ret_6h: float,
        ret_24h: float,
        score: float,
        score_decay: float,
        tech: dict,
        hist: dict,
    ) -> dict | None:
        strategy_source = hist.get("strategy_source") or "normal"
        if strategy_source == "alpha":
            return None

        is_long_reversal = side == "LONG" and ret_6h < 0 and ret_24h < 0
        is_short_reversal = side == "SHORT" and ret_6h > 0 and ret_24h > 0
        if not (is_long_reversal or is_short_reversal):
            return None

        class_key, policy = _exit_policy_for_symbol(sym, strategy_source)
        if policy.get("use_normal_momentum_exit") is False:
            return None
        soft_cfg = self.cfg.get("normal_soft_exit") or {}
        if _normal_soft_exit_in_cooldown(sym, float(soft_cfg.get("cooldown_minutes", 60))):
            return None

        hold_exit = float(policy.get("hold_alpha_exit", 50))
        if hold_alpha >= hold_exit:
            return None

        small_profit = float(policy.get("small_profit_pct", 2.0))
        partial_pct = float(policy.get("partial_close_pct", 0.5))
        strong_partial_pct = float(policy.get("strong_partial_close_pct", partial_pct))
        ema_ratio = float(tech.get("ema20_50_ratio") or 1.0)
        ema_slope = float(tech.get("ema20_slope") or 0)
        oi_change = float(tech.get("oi_change_pct") or tech.get("oi_change") or 0)
        if side == "LONG":
            trend_broken = ema_ratio < 1.0 or ema_slope < 0
            oi_weak = oi_change < 0
        else:
            trend_broken = ema_ratio > 1.0 or ema_slope > 0
            oi_weak = oi_change > 0
        score_fade = score_decay >= 20
        reason_base = (
            f"category_momentum_reversal class={class_key} hold_alpha={hold_alpha:.1f} "
            f"pnl={pnl_pct:.1f}% ret6h={ret_6h:.2%} ret24h={ret_24h:.2%}"
        )

        def action(kind: str, reason: str, close_pct: float | None = None) -> dict:
            item = {
                "action": kind,
                "symbol": sym,
                "side": close_side,
                "reason": reason,
                "close_price": mark_price,
                "score": score,
                "strategy_source": strategy_source,
                "signal_source": hist.get("signal_source"),
            }
            if close_pct is not None:
                item["close_pct"] = max(
                    0.05,
                    min(float(soft_cfg.get("weak_trend_close_pct", 0.25)), close_pct),
                )
            return item

        if class_key == "core_bluechip":
            if pnl_pct <= 0 and trend_broken:
                return None
            if 0 < pnl_pct <= small_profit:
                if trend_broken and (oi_weak or score_fade):
                    return action("partial_close", f"{reason_base} bluechip_strong_warning", strong_partial_pct)
                if trend_broken:
                    return action("partial_close", f"{reason_base} bluechip_trend_warning", partial_pct)
            return None

        if class_key == "large_cap":
            if pnl_pct <= 0 and trend_broken:
                return None
            if 0 < pnl_pct <= small_profit and trend_broken:
                pct = strong_partial_pct if (oi_weak or score_fade) else partial_pct
                return action("partial_close", f"{reason_base} large_cap_trend_warning", pct)
            return None

        if class_key == "fundamental":
            if pnl_pct <= 0:
                return None
            if 0 < pnl_pct <= small_profit:
                rounds = _recent_momentum_exit_count(sym, class_key, limit=3)
                if rounds >= int(policy.get("confirm_rounds_for_full_close", 2)) - 1:
                    return action("partial_close", f"{reason_base} fundamental_confirmed_weakness", strong_partial_pct)
                return action("partial_close", f"{reason_base} fundamental_first_warning", partial_pct)
            if trend_broken or score_fade:
                return action("partial_close", f"{reason_base} fundamental_profit_protect", partial_pct)
            return None

        if class_key in {"narrative", "meme"}:
            if pnl_pct <= 0:
                return None
            return action("partial_close", f"{reason_base} {class_key}_profit_protect", partial_pct)

        if pnl_pct <= small_profit:
            if pnl_pct <= 0:
                return None
            return action("partial_close", f"{reason_base} default_exit", partial_pct)
        return action("partial_close", f"{reason_base} default_profit_protect", partial_pct)

    def _build_position_actions(self, top_symbols: list, current_positions: list, run_id: str | None = None) -> list:
        from shared.db import get_position_history

        actions = []
        latest_map = _latest_by_symbol(top_symbols)
        for pos in current_positions:
            sym = pos["symbol"]
            latest = latest_map.get(sym) or {}
            raw = _raw_features(latest)
            hist_perf = raw.get("historical_performance") or {}
            alpha = raw.get("alpha") or {}
            tech = raw.get("technical") or {}
            depth = raw.get("depth") or {}
            hist = get_position_history(sym) or {}

            entry_price = float(pos.get("entry_price") or 0)
            mark_price = float(pos.get("mark_price") or 0)
            quantity = float(pos.get("quantity") or 0)
            leverage = max(float(pos.get("leverage") or 1), 1)
            margin = entry_price * quantity / leverage
            pnl = float(pos.get("unrealized_pnl") or 0)
            pnl_pct = pnl / margin * 100 if margin else 0.0
            side = pos.get("side")
            close_side = "SELL" if side == "LONG" else "BUY"

            try:
                from trader.roll_policy import is_residual_position

                residual = is_residual_position(
                    quantity=quantity,
                    mark_price=mark_price,
                    leverage=leverage,
                    exchange_info=self.ex.get_symbol_info(sym),
                    config=self.cfg.get("roll_trading") or {},
                )
            except Exception as e:
                residual = False
                logger.warning(f"  {sym}: current residual check unavailable: {e}")
            if residual:
                actions.append({
                    "action": "close",
                    "symbol": sym,
                    "side": close_side,
                    "reason": "residual_position_cleanup",
                    "close_price": mark_price,
                    "strategy_source": hist.get("strategy_source") or "normal",
                    "signal_source": hist.get("signal_source"),
                    "run_id": run_id,
                })
                self._record_decision(
                    sym,
                    run_id=run_id,
                    side=side,
                    decision_stage="position_management",
                    decision_result="planned_close",
                    filter_reason="residual_position_cleanup",
                    quantity=quantity,
                    price=mark_price,
                    risk_params={
                        "effective_margin": abs(quantity * mark_price) / leverage,
                        "notional": abs(quantity * mark_price),
                    },
                )
                continue

            score = float(latest.get("composite_score") or 0)
            hold_alpha = float(latest.get("hold_alpha") or raw.get("hold_alpha") or 50)
            entry_score = float(hist.get("entry_score") or score)
            score_decay = entry_score - score
            age_h = _age_hours(hist.get("entry_time"))
            expectancy = float(hist_perf.get("expectancy") or 0)
            total_pnl = float(hist_perf.get("total_pnl") or 0)
            profit_factor = float(hist_perf.get("profit_factor") or 1)
            p_drawdown = float(alpha.get("p_drawdown") or 0)
            ret_6h = float(tech.get("return_6h") or 0)
            ret_24h = float(tech.get("return_24h") or 0)
            depth_score = float(depth.get("depth_ratio_score") or 50)
            robot_signature = bool(depth.get("robot_signature"))
            tracker = self._pos_tracker.get(sym, {})
            highest_price = float(tracker.get("highest_price") or mark_price)
            lowest_price = float(tracker.get("lowest_price") or mark_price)
            atr = float(hist.get("atr_value") or pos.get("atr_value") or mark_price * 0.02)
            soft_exit_profit_pct = float(self.cfg.get("soft_exit_profit_pct", 2.0))
            soft_exit_loss_pct = -float(self.cfg.get("soft_exit_max_loss_pct", 3.5))
            r_state = _position_r_state(side, entry_price, mark_price, hist, atr, highest_price=highest_price, lowest_price=lowest_price)
            try:
                from shared.db import update_position_management

                update_position_management(
                    sym,
                    stop_model=hist.get("stop_model") or "atr_clamped_legacy_default",
                    stop_pct=r_state.get("stop_pct"),
                    current_stop_loss=r_state.get("current_stop_loss"),
                    trailing_stop_price=r_state.get("trailing_stop_price"),
                    trailing_enabled=1 if r_state.get("trailing_enabled") else 0,
                    r_multiple=round(float(r_state.get("r_multiple") or 0), 3),
                )
            except Exception:
                pass

            def add(action, reason, close_pct=None, is_stop=False):
                item = {
                    "action": action,
                    "symbol": sym,
                    "side": close_side,
                    "reason": reason,
                    "close_price": mark_price,
                    "score": score,
                    "strategy_source": hist.get("strategy_source") or "normal",
                    "signal_source": hist.get("signal_source"),
                }
                if close_pct is not None:
                    item["close_pct"] = close_pct
                if is_stop:
                    item["is_stop"] = True
                actions.append(item)

            soft_cfg = self.cfg.get("normal_soft_exit") or {}
            soft_cooldown = _normal_soft_exit_in_cooldown(
                sym, float(soft_cfg.get("cooldown_minutes", 60))
            )
            soft_trend_state = _normal_soft_trend_state(side, mark_price, tech)

            def handle_soft_exit(source: str) -> None:
                if soft_cooldown:
                    self._record_decision(
                        latest, run_id=run_id, side=side,
                        decision_stage="position_management", decision_result="hold",
                        filter_reason=f"soft_exit_cooldown source={source}",
                    )
                    return
                if soft_trend_state == "strong" and pnl_pct >= soft_exit_profit_pct:
                    add(
                        "partial_close",
                        f"normal_soft_exit strong_trend source={source} pnl={pnl_pct:.1f}%",
                        float(soft_cfg.get("strong_trend_close_pct", 0.20)),
                    )
                    return
                if soft_trend_state == "weak":
                    add(
                        "partial_close",
                        f"normal_soft_exit trend_weak source={source} pnl={pnl_pct:.1f}%",
                        float(soft_cfg.get("weak_trend_close_pct", 0.25)),
                    )
                    return
                self._record_decision(
                    latest, run_id=run_id, side=side,
                    decision_stage="position_management", decision_result="hold",
                    filter_reason=f"soft_exit_trend_ambiguous source={source} state={soft_trend_state}",
                )

            if (hist.get("strategy_source") or "normal") == "alpha":
                alpha_action = self._build_alpha_position_action(
                    pos,
                    hist,
                    pnl_pct,
                    mark_price,
                    close_side,
                    highest_price,
                    atr,
                    age_h,
                    run_id=run_id,
                )
                if alpha_action:
                    actions.append(alpha_action)
                continue

            hard_stop_pct = float(self.cfg.get("hard_stop_pct", 0.12)) * 100
            if hist.get("signal_source") == "bluechip_trend":
                bluechip_cfg = _bluechip_cfg()
                hard_stop_pct = float(bluechip_cfg.get("hard_stop_pct", 0.12)) * 100

            if pnl_pct <= -hard_stop_pct:
                add(
                    "close",
                    f"margin_hard_stop roi={pnl_pct:.2f}% threshold=-{hard_stop_pct:.2f}% "
                    f"margin={margin:.2f} pnl={pnl:.2f} leverage={leverage:g}",
                    is_stop=True,
                )
                continue
            if pnl_pct < 0:
                self._record_decision(
                    latest,
                    run_id=run_id,
                    side=side,
                    decision_stage="position_management",
                    decision_result="hold",
                    filter_reason=(
                        f"loss_hold_until_margin_hard_stop roi={pnl_pct:.2f}% "
                        f"threshold=-{hard_stop_pct:.2f}%"
                    ),
                )
                continue

            if hist.get("signal_source") == "bluechip_trend":
                bluechip_cfg = _bluechip_cfg()
                min_entry_alpha = float(bluechip_cfg.get("exit_min_entry_alpha", 35))
                ema_ratio = float(tech.get("ema20_50_ratio") or 1.0)
                ema_slope = float(tech.get("ema20_slope") or 0)
                if r_state.get("stop_triggered"):
                    add("close", f"bluechip_trailing_stop r={float(r_state.get('r_multiple') or 0):.2f} stop={float(r_state.get('current_stop_loss') or 0):.4f}")
                    continue
                if float(r_state.get("r_multiple") or 0) >= 2 and not int(hist.get("tp2_hit") or 0):
                    add("partial_close", "bluechip_TP2 r>=2", float(bluechip_cfg.get("tp2_pct", 0.25)))
                    continue
                if float(r_state.get("r_multiple") or 0) >= 1 and not int(hist.get("tp1_hit") or 0):
                    add("partial_close", "bluechip_TP1 r>=1", float(bluechip_cfg.get("tp1_pct", 0.25)))
                    continue
                if age_h is not None and age_h >= float(bluechip_cfg.get("time_stop_hours", 6)):
                    if pnl_pct < float(bluechip_cfg.get("time_stop_min_return", 0.008)) * 100:
                        self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_bluechip_time_stop age={age_h:.1f}h pnl={pnl_pct:.1f}%")
                        continue
                if hold_alpha < min_entry_alpha and pnl_pct <= 1:
                    self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_bluechip_entry_alpha_fade {hold_alpha:.1f} pnl={pnl_pct:.1f}%")
                    continue
                if side == "LONG" and (ema_ratio < 1.0 or ema_slope < 0) and pnl_pct <= 2:
                    if pnl_pct <= 0:
                        self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_bluechip_trend_invalid ema_ratio={ema_ratio:.4f} slope={ema_slope:.4f} pnl={pnl_pct:.1f}%")
                    else:
                        add("partial_close", f"bluechip_trend_warning ema_ratio={ema_ratio:.4f} slope={ema_slope:.4f} pnl={pnl_pct:.1f}%", 0.25)
                    continue
            if r_state.get("stop_triggered"):
                add("close", f"trailing_stop r={float(r_state.get('r_multiple') or 0):.2f} stop={float(r_state.get('current_stop_loss') or 0):.4f}")
                continue
            if float(r_state.get("r_multiple") or 0) >= 2 and not int(hist.get("tp2_hit") or 0):
                add("partial_close", "TP2 r>=2", 0.25)
                continue
            if float(r_state.get("r_multiple") or 0) >= 1 and not int(hist.get("tp1_hit") or 0):
                add("partial_close", "TP1 r>=1", 0.25)
                continue
            if robot_signature and hold_alpha < 55 and pnl_pct <= soft_exit_loss_pct:
                add("partial_close", f"orderbook_robot_signature_risk hold_alpha={hold_alpha:.1f} pnl={pnl_pct:.1f}%", 0.25)
                continue
            if robot_signature and hold_alpha < 55:
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_orderbook_robot_signature hold_alpha={hold_alpha:.1f} pnl={pnl_pct:.1f}%")
                continue
            if hold_alpha <= 25 and pnl_pct <= soft_exit_loss_pct:
                add("partial_close", f"hold_alpha_collapse_risk {hold_alpha:.1f} pnl={pnl_pct:.1f}%", 0.25)
                continue
            if hold_alpha <= 25:
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_alpha_collapse {hold_alpha:.1f} pnl={pnl_pct:.1f}%")
                continue
            if hold_alpha <= 35 and pnl_pct >= soft_exit_profit_pct:
                handle_soft_exit(f"hold_alpha_{hold_alpha:.1f}")
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_full", 40)):
                handle_soft_exit(f"score_decay_{score_decay:.1f}")
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_half", 30)) and not int(hist.get("tp2_hit") or 0):
                handle_soft_exit(f"score_decay_{score_decay:.1f}")
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_qtr", 20)) and not int(hist.get("tp1_hit") or 0):
                handle_soft_exit(f"score_decay_{score_decay:.1f}")
                continue
            if int(hist_perf.get("total") or 0) >= 5 and (expectancy <= 0 or total_pnl < 0 or profit_factor < 1):
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_history_expectancy_bad exp={expectancy:.4f} pf={profit_factor:.2f} pnl={pnl_pct:.1f}%")
                continue
            if p_drawdown >= 0.60 and pnl_pct <= soft_exit_loss_pct:
                add("partial_close", f"drawdown_risk_reduce {p_drawdown:.2f}, pnl={pnl_pct:.1f}%", 0.25)
                continue
            if p_drawdown >= 0.60 and pnl_pct < soft_exit_profit_pct:
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_drawdown_risk {p_drawdown:.2f} pnl={pnl_pct:.1f}%")
                continue
            if depth_score < 35 and pnl_pct <= soft_exit_loss_pct:
                add("partial_close", f"orderbook_depth_weak_reduce score={depth_score:.1f} pnl={pnl_pct:.1f}%", 0.25)
                continue
            if depth_score < 35 and pnl_pct < soft_exit_profit_pct:
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_orderbook_depth_weak score={depth_score:.1f} pnl={pnl_pct:.1f}%")
                continue

            time_stop_h = float(self.cfg.get("time_stop_hours", 12))
            min_ret = float(self.cfg.get("time_stop_min_return", 0.02)) * 100
            if age_h is not None and age_h >= time_stop_h and pnl_pct < min_ret:
                self._record_decision(latest, run_id=run_id, side=side, decision_stage="position_management", decision_result="hold", filter_reason=f"soft_hold_time_stop age={age_h:.1f}h pnl={pnl_pct:.1f}%")
                continue
            category_exit = self._build_category_momentum_exit(
                sym,
                side,
                close_side,
                mark_price,
                pnl_pct,
                hold_alpha,
                ret_6h,
                ret_24h,
                score,
                score_decay,
                tech,
                hist,
            )
            if category_exit:
                actions.append(category_exit)
                continue
        return actions

    def _spread_limit_for_profile(self, entry_profile: dict | None = None) -> tuple[float, float, str, str]:
        profile = (entry_profile or {}).get("template") or "default"
        network = "testnet" if EXCHANGE_CONFIG.get("testnet") else "prod"
        limits = (self.cfg.get("spread_limits") or {}).get(network) or {}
        soft_limit = float(limits.get(profile) or limits.get("default") or 0.003)
        hard_max = float(limits.get("hard_max") or max(soft_limit, 0.006))
        return soft_limit, hard_max, network, profile

    def _check_live_orderbook(self, symbol: str, side: str, entry_profile: dict | None = None) -> tuple[bool, str, dict]:
        data = self.ex.get_depth(symbol, 20)
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return False, "binance depth unavailable", {}

        bid_depth = sum(float(x[1]) for x in bids)
        ask_depth = sum(float(x[1]) for x in asks)
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        spread_pct = (best_ask - best_bid) / mid if mid else 1
        bid_ask_ratio = bid_depth / ask_depth if ask_depth else 999
        ask_bid_ratio = ask_depth / bid_depth if bid_depth else 999
        info = {
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "spread_pct": spread_pct,
            "bid_ask_ratio": bid_ask_ratio,
            "ask_bid_ratio": ask_bid_ratio,
        }
        soft_limit, hard_max, network, profile = self._spread_limit_for_profile(entry_profile)
        info.update({
            "spread_soft_limit": soft_limit,
            "spread_hard_max": hard_max,
            "spread_network": network,
            "entry_profile": profile,
        })
        if spread_pct > hard_max:
            return False, f"spread too wide: {spread_pct:.4%} > hard_max {hard_max:.2%} ({network}/{profile})", info
        if spread_pct > soft_limit:
            if network == "testnet":
                info["spread_degraded"] = True
                return True, f"testnet spread degraded: {spread_pct:.4%} > soft {soft_limit:.2%} ({profile})", info
            return False, f"spread too wide: {spread_pct:.4%} > soft {soft_limit:.2%} ({network}/{profile})", info
        if side == "LONG" and bid_ask_ratio < 0.65:
            return False, f"live depth against LONG: bid/ask={bid_ask_ratio:.2f}", info
        if side == "SHORT" and ask_bid_ratio < 0.65:
            return False, f"live depth against SHORT: ask/bid={ask_bid_ratio:.2f}", info
        return True, "OK", info

    def _build_alpha_open_actions(
        self,
        current_positions: list,
        balance: float,
        avail: int,
        run_id: str | None = None,
    ) -> list:
        cfg = _alpha_cfg()
        if not cfg.get("enabled", False):
            return []
        if cfg.get("testnet_only", True) and not EXCHANGE_CONFIG.get("testnet"):
            logger.info("Alpha trading disabled outside testnet")
            return []
        account_slots_full_reason = "account position slots full" if avail <= 0 else None

        try:
            from shared.db import (
                fetch_latest_alpha_scan,
                fetch_latest_score_for_symbol,
                get_alpha_cooldown,
                get_position_history,
                upsert_alpha_trade_candidate,
            )
        except Exception as e:
            logger.warning(f"Alpha trading db import failed: {e}")
            return []

        scan, rows = fetch_latest_alpha_scan()
        if not scan or not rows:
            return []

        trading = self._get_trading_symbols()
        pos_symbols = {p["symbol"] for p in current_positions}
        alpha_position_count = 0
        for p in current_positions:
            hist = get_position_history(p["symbol"]) or {}
            if hist.get("strategy_source") == "alpha":
                alpha_position_count += 1

        max_alpha_positions = int(cfg.get("max_positions", 3))
        remaining_alpha_slots = max(0, max_alpha_positions - alpha_position_count)
        remaining_slots = min(avail, remaining_alpha_slots)
        no_open_slots_reason = account_slots_full_reason or (
            f"alpha slots full: {alpha_position_count}/{max_alpha_positions}"
            if remaining_slots <= 0
            else None
        )

        blocked_profiles = set(cfg.get("blocked_profiles") or ("high_risk_watch",))
        ttl = float(cfg.get("signal_ttl_minutes", 15))
        vp_ttl = float(cfg.get("volume_price_ttl_minutes", 20))
        min_score = float(cfg.get("min_score", 68))
        actions = []

        candidates = sorted(
            [dict(r) for r in rows],
            key=lambda r: (
                2 if (_raw_features(r).get("volume_price") or {}).get("action") == "normal_review" else 1 if (_raw_features(r).get("volume_price") or {}).get("action") == "normal_review_probe" else 0,
                float(r.get("alpha_score") or 0),
                float(r.get("liquidity_score") or 0),
                float(r.get("risk_score") or 0),
            ),
            reverse=True,
        )

        for row in candidates:
            alpha_symbol = row.get("alpha_symbol")
            symbol = row.get("futures_symbol")
            profile = row.get("alpha_profile")
            discovery_score = float(row.get("alpha_score") or row.get("discovery_score") or 0)
            raw_alpha = _raw_features(row)
            side = None
            normal_row = None
            adapter_quality = 100.0
            missing_fields = []
            entry_profile = {}
            volume_price = raw_alpha.get("volume_price") or evaluate_alpha_volume_price(
                raw_alpha,
                row.get("market_price") or 0,
            )

            def record_candidate(reason=None, status="filtered"):
                try:
                    upsert_alpha_trade_candidate(
                        scan_id=row.get("scan_id"),
                        time=row.get("time"),
                        alpha_symbol=alpha_symbol,
                        futures_symbol=symbol,
                        base_asset=row.get("base_asset"),
                        alpha_discovery_score=discovery_score,
                        alpha_profile=profile,
                        alpha_reason=row.get("decision"),
                        raw_alpha=raw_alpha,
                        normal_score=(normal_row or {}).get("composite_score"),
                        normal_grade=(normal_row or {}).get("composite_summary") or (normal_row or {}).get("grade"),
                        normal_side=side,
                        entry_profile=entry_profile,
                        entry_status=status,
                        block_reason=reason,
                        adapter_quality=adapter_quality,
                        missing_fields=missing_fields,
                        volume_price=volume_price,
                    )
                except Exception as e:
                    logger.warning(f"Alpha candidate write failed: {e}")

            def reject(reason, risk_params=None):
                record_candidate(reason, status="filtered")
                self._record_decision(
                    {
                        "symbol": symbol or alpha_symbol,
                        "scan_id": row.get("scan_id"),
                        "time": row.get("time"),
                        "composite_score": (normal_row or {}).get("composite_score", discovery_score),
                        "grade": row.get("grade"),
                        "raw_features": (normal_row or {}).get("raw_features", raw_alpha),
                    },
                    run_id=run_id,
                    side=side,
                    mode="alpha_live",
                    decision_stage="alpha_candidate_filter",
                    decision_result="filtered",
                    filter_reason=reason,
                    risk_params={
                        "alpha_symbol": alpha_symbol,
                        "alpha_profile": profile,
                        "alpha_discovery_score": discovery_score,
                        "adapter_quality": adapter_quality,
                        "missing_fields": missing_fields,
                        **(risk_params or {}),
                    },
                    reason={"decision": row.get("decision")},
                )
                logger.info(f"  Alpha {alpha_symbol}: {reason}")

            age = _signal_age_minutes(row)
            if ttl and age is not None and age > ttl:
                reject(f"stale alpha signal age={age:.1f}m > {ttl:.0f}m")
                continue
            if vp_ttl and age is not None and age > vp_ttl:
                reject(f"stale volume-price state age={age:.1f}m > {vp_ttl:.0f}m")
                continue
            if profile in blocked_profiles:
                reject(f"blocked alpha profile: {profile}")
                continue
            if discovery_score < min_score:
                reject(f"alpha_discovery_score {discovery_score:.1f} < {min_score:.1f}")
                continue
            if not symbol:
                reject("missing futures_symbol")
                continue
            if symbol not in trading:
                reject("not tradable on Binance Futures")
                continue
            if symbol in pos_symbols:
                reject("already in position")
                continue
            if any(a.get("symbol") == symbol for a in actions):
                reject("already planned this loop")
                continue

            learning_action = build_learning_action(
                row,
                side="LONG",
                strategy_source="alpha",
                category="alpha",
                symbol=symbol,
            )
            if learning_action:
                self.ai_learning_actions.append(learning_action)

            cooldown = get_alpha_cooldown(symbol) or get_alpha_cooldown("*")
            if cooldown:
                reject(f"alpha cooldown active: {cooldown.get('reason')} until {cooldown.get('cooldown_until')}")
                continue

            confirmation_cfg = cfg.get("entry_confirmation") or {}
            breakout_ok, breakout_reason, breakout_info = True, None, {}
            if confirmation_cfg.get("require_15m_breakout_confirmation", True):
                breakout_ok, breakout_reason, breakout_info = _check_alpha_futures_breakout_confirmation(symbol)
            volume_price = _promote_confirmed_alpha_probe(volume_price, raw_alpha, breakout_ok)
            vp_action = volume_price.get("action")
            if vp_action == "cooldown":
                try:
                    from shared.db import set_alpha_cooldown
                    set_alpha_cooldown(
                        symbol,
                        "volume_price_overheated",
                        "; ".join(volume_price.get("reasons") or ["alpha volume-price overheated"]),
                        int(volume_price.get("cooldown_minutes") or 60),
                    )
                except Exception:
                    pass
                reject("量价过热冷静：" + "；".join(volume_price.get("reasons") or []), {"volume_price": volume_price})
                continue
            if vp_action == "observe":
                reject("量价中性观察：" + "；".join(volume_price.get("reasons") or []), {"volume_price": volume_price})
                continue
            fast_returns = (raw_alpha.get("returns") or {})
            if float(fast_returns.get("ret_15m") or 0) > 8 or float(fast_returns.get("ret_1h") or 0) > 15:
                try:
                    from shared.db import set_alpha_cooldown
                    set_alpha_cooldown(symbol, "chase_guard", "alpha short-term pump too hot", 45)
                except Exception:
                    pass
                reject("alpha追涨冷静：15m/1h 涨幅过大，等待回踩")
                continue

            if volume_price.get("allow_short"):
                reject("alpha short disabled; long-only alpha mode", {"volume_price": volume_price})
                continue
            if not volume_price.get("allow_long"):
                reject(f"alpha long not allowed by volume trend gate: {volume_price.get('state')}", {"volume_price": volume_price})
                continue
            if confirmation_cfg.get("require_15m_breakout_confirmation", True) and not breakout_ok:
                reject(breakout_reason, {"alpha_15m_confirmation": breakout_info, "volume_price": volume_price})
                continue
            side = "LONG"
            action_side = "BUY"
            if no_open_slots_reason:
                reject(
                    no_open_slots_reason,
                    {
                        "current_positions": len(current_positions),
                        "alpha_position_count": alpha_position_count,
                        "max_alpha_positions": max_alpha_positions,
                    },
                )
                continue
            symbol_risk = get_symbol_risk(symbol)

            vp_factor = max(0.0, min(1.0, float(volume_price.get("max_position_factor") or 0)))
            entry_status = "probe" if vp_action == "normal_review_probe" or vp_factor <= 0.25 else "pass"
            entry_profile = {
                "status": entry_status,
                "template": f"alpha_{volume_price.get('state') or 'volume_price'}",
                "reason": "alpha volume-price gate passed; normal trading review skipped",
                "thresholds": {
                    "position_size_factor": 1.0,
                    "probe_position_size_factor": 1.0,
                },
                "risk_profile": symbol_risk,
                "volume_price_state": volume_price.get("state"),
                "volume_price_action": vp_action,
            }
            market_phase = _market_phase_gate(raw_alpha)
            alpha_entry_ok, alpha_entry_reason = _alpha_probe_entry_decision(
                raw_alpha,
                entry_profile.get("status"),
                breakout_ok,
            )
            if not alpha_entry_ok:
                reject(
                    alpha_entry_reason,
                    {
                        "dual_market_volume": raw_alpha.get("dual_market_volume") or {},
                        "alpha_15m_confirmation": breakout_info,
                        "market_phase": market_phase,
                    },
                )
                continue
            phase_ok, phase_status, phase_reason = _market_phase_entry_decision(
                market_phase,
                entry_profile.get("status"),
            )
            if not phase_ok:
                reject(phase_reason, {"market_phase": market_phase})
                continue
            if phase_status != entry_profile.get("status"):
                entry_profile = {
                    **entry_profile,
                    "status": phase_status,
                    "reason": f"{entry_profile.get('reason')} · {phase_reason}",
                    "market_phase": market_phase,
                }
            elif market_phase:
                entry_profile = {**entry_profile, "market_phase": market_phase}
            normal_row = _adapt_alpha_to_normal_row(row)
            normal_row["composite_score"] = round(max(0.0, min(100.0, discovery_score)), 1)
            normal_row["composite_summary"] = row.get("grade") or _grade_from_score(discovery_score)

            try:
                ob_ok, ob_reason, ob_info = self._check_live_orderbook(symbol, side, entry_profile)
            except Exception as e:
                ob_ok, ob_reason, ob_info = False, f"binance depth error: {e}", {}
            if not ob_ok:
                try:
                    from shared.db import set_alpha_cooldown
                    set_alpha_cooldown(symbol, "orderbook_reject", ob_reason, 20)
                except Exception:
                    pass
                reject(ob_reason, ob_info)
                continue

            price = float(normal_row.get("market_price") or row.get("market_price") or 0)
            try:
                mark_price = float(self.ex.get_mark_price(symbol) or 0)
                if mark_price > 0:
                    price = mark_price
            except Exception:
                pass
            if price <= 0:
                reject("invalid alpha price")
                continue

            alpha_execution_score = float(discovery_score or 0)
            alpha_entry_mode = "probe" if entry_profile.get("status") == "probe" else ("strong" if vp_factor >= 0.30 else "confirmed")
            size_multiplier = 0.75 if ob_info.get("spread_degraded") else 1.0
            if market_phase.get("phase") == "range":
                size_multiplier *= 0.75
            pos_info = calculate_position(
                self.ex,
                symbol,
                price,
                balance,
                alpha_execution_score,
                category="alpha",
                entry_mode=alpha_entry_mode,
                size_multiplier=size_multiplier,
            )
            lev = int(pos_info.get("leverage") or self.cfg.get("leverage_max", 3))
            qty = float(pos_info.get("quantity") or 0)
            qty = self.ex.adjust_quantity(symbol, qty)
            if qty <= 0:
                reject("quantity <= 0", {"pos_info": pos_info, "volume_price_factor": vp_factor, "symbol_risk": symbol_risk})
                continue

            atr = float(pos_info.get("atr_value") or 0)
            if atr <= 0:
                atr = price * 0.02
            stop_distance = float(pos_info.get("stop_loss") or price * float(pos_info.get("stop_pct") or 0.05))
            stop_price = (price - stop_distance) if side == "LONG" else (price + stop_distance)
            tp_levels = calc_tp_levels(price, side, pos_info.get("stop_pct"))
            invested = round(price * qty, 2)

            reason = f"alpha_volume_price->{entry_profile.get('template')} alpha_score={alpha_execution_score:.1f} {side}"
            action = {
                "action": "open",
                "symbol": symbol,
                "side": action_side,
                "position_side": side,
                "quantity": qty,
                "entry_price": price,
                "stop_loss": stop_price,
                "leverage": lev,
                "tp1_price": tp_levels["tp1_price"],
                "tp2_price": tp_levels["tp2_price"],
                "tp1_qty_pct": tp_levels["tp1_qty_pct"],
                "tp2_qty_pct": tp_levels["tp2_qty_pct"],
                "atr_value": atr,
                "stop_model": pos_info.get("stop_model"),
                "stop_pct": pos_info.get("stop_pct"),
                "trailing_atr_multiplier": pos_info.get("trailing_atr_multiplier"),
                "reason": reason,
                "grade": normal_row.get("composite_summary") or normal_row.get("grade", ""),
                "score": alpha_execution_score,
                "invested": invested,
                "run_id": run_id,
                "scan_id": row.get("scan_id"),
                "entry_mode": entry_profile.get("status"),
                "strategy_source": "alpha",
                "signal_source": profile,
                "alpha_symbol": alpha_symbol,
                "alpha_profile": profile,
                "alpha_entry_level": entry_profile.get("status"),
                "alpha_score": discovery_score,
                "alpha_suggested_position_pct": round(float(pos_info.get("margin") or 0) / balance, 4) if balance else 0,
                "ai_features": {
                    **(normal_row.get("raw_features") or {}),
                    "trend_score": entry_profile.get("trend_score") or normal_row.get("trend_score") or 0,
                    "spread_pct": ob_info.get("spread_pct") or 0,
                    "volume_sync_score": volume_price.get("sync_score") or 0,
                },
            }
            actions.append(action)
            record_candidate(None, status="planned_open")
            self._record_decision(
                {
                    "symbol": symbol,
                    "scan_id": row.get("scan_id"),
                    "time": row.get("time"),
                    "composite_score": alpha_execution_score,
                    "grade": normal_row.get("composite_summary") or row.get("grade"),
                    "raw_features": normal_row.get("raw_features"),
                },
                run_id=run_id,
                side=side,
                mode="alpha_live",
                decision_stage="alpha_open_decision",
                decision_result="planned_open",
                quantity=qty,
                entry_price=price,
                risk_params={
                    "strategy_source": "alpha",
                    "alpha_symbol": alpha_symbol,
                    "alpha_profile": profile,
                    "alpha_discovery_score": discovery_score,
                    "adapter_quality": adapter_quality,
                    "missing_fields": missing_fields,
                    "entry_profile": entry_profile,
                    "volume_price_factor": vp_factor,
                    "symbol_risk": symbol_risk,
                    "volume_price": volume_price,
                    "leverage": lev,
                    "sizing": pos_info,
                    "stop_loss": stop_price,
                    "tp1_price": tp_levels["tp1_price"],
                    "tp2_price": tp_levels["tp2_price"],
                    "orderbook": ob_info,
                },
                reason={"reason": reason},
            )
            if len(actions) >= remaining_slots:
                break

        return actions

    def _roll_profile_allowed(self, hist: dict, latest: dict, raw: dict) -> tuple[bool, str]:
        cfg = self.cfg.get("roll_trading") or {}
        strategy_source = hist.get("strategy_source") or "normal"
        entry_reason = str(hist.get("entry_reason") or "").lower()
        if strategy_source == "alpha":
            alpha_profile = hist.get("alpha_profile")
            if alpha_profile in set(cfg.get("blocked_alpha_profiles") or []):
                return False, f"alpha profile blocked: {alpha_profile}"
            allowed_states = [str(x).lower() for x in (cfg.get("allowed_alpha_states") or [])]
            if any(state in entry_reason for state in allowed_states):
                return True, "alpha volume-price trend state"
            if float(hist.get("alpha_score") or 0) >= 75 and alpha_profile in ("momentum_continuation", "futures_mapped"):
                return True, "alpha strong mapped trend"
            return False, "alpha profile is not roll-enabled"

        keywords = [str(x).lower() for x in (cfg.get("allowed_normal_keywords") or [])]
        trend_text = " ".join(
            str(x or "").lower()
            for x in (
                hist.get("entry_reason"),
                latest.get("trend_state"),
                latest.get("trend_direction"),
                latest.get("risk_label"),
                latest.get("price_position"),
            )
        )
        if any(k in trend_text for k in keywords):
            return True, "normal trend/breakout profile"
        if float(latest.get("composite_score") or hist.get("entry_score") or 0) >= 72:
            return True, "normal high score trend candidate"
        return False, "position type is not roll-enabled"

    def _build_roll_actions(self, top_symbols: list, current_positions: list, planned_actions: list, balance: float, run_id: str | None = None) -> list:
        from shared.db import get_position_history, update_position_management
        from trader.roll_policy import calculate_protected_stop, calculate_roll_quantity, evaluate_roll

        cfg = self.cfg.get("roll_trading") or {}
        if not cfg.get("enabled", False):
            return []

        actions = []
        latest_map = _latest_by_symbol(top_symbols)
        blocked_symbols = {
            a.get("symbol") for a in planned_actions
            if a.get("action") in ("close", "partial_close")
        }

        for pos in current_positions:
            sym = pos.get("symbol")
            if not sym or sym in blocked_symbols:
                continue

            hist = get_position_history(sym) or {}
            latest = latest_map.get(sym) or {}
            raw = _raw_features(latest)
            tech = raw.get("technical") or {}
            market_phase = _market_phase_gate(raw)
            strategy_source = hist.get("strategy_source") or "normal"
            side = pos.get("side")
            mark_price = float(pos.get("mark_price") or 0)
            raw_sync = raw.get("dual_market_volume") or {}
            alpha_sync = bool(raw_sync.get("synchronized", raw_sync.get("sync_confirmed", False)))

            def block(reason):
                update_position_management(
                    sym,
                    roll_enabled=0,
                    roll_block_reason=reason,
                )
                self._record_decision(
                    sym,
                    run_id=run_id,
                    side=side,
                    decision_stage="roll_position",
                    decision_result="filtered",
                    filter_reason=reason,
                    risk_params={
                        "strategy_source": strategy_source,
                    },
                )

            if market_phase and not bool(market_phase.get("allow_roll")):
                phase_name = str(market_phase.get("phase") or "unknown")
                block(f"market_phase_{phase_name}")
                continue

            decision = evaluate_roll(
                {**pos, "strategy_source": strategy_source, "alpha_profile": hist.get("alpha_profile")},
                hist,
                tech,
                alpha_sync=alpha_sync,
                config=cfg,
            )
            if not decision.eligible:
                block(decision.status)
                continue

            exchange_info = self.ex.get_symbol_info(sym)
            add_qty = calculate_roll_quantity(
                hist.get("initial_quantity"), exchange_info, mark_price, cfg
            )
            if add_qty <= 0:
                block("roll quantity <= 0")
                continue

            action_side = "BUY" if side == "LONG" else "SELL"
            current_qty = float(pos.get("quantity") or 0)
            entry_price = float(pos.get("entry_price") or 0)
            blended_entry = ((entry_price * current_qty) + (mark_price * add_qty)) / (current_qty + add_qty)
            stop_price = calculate_protected_stop(side, blended_entry, cfg)
            reason = f"roll_add_once: tp1_confirmed current_r={decision.current_r:.2f} trend_confirmed"
            actions.append({
                "action": "roll_add",
                "symbol": sym,
                "side": action_side,
                "position_side": side,
                "quantity": add_qty,
                "entry_price": mark_price,
                "stop_loss": stop_price,
                "leverage": int(pos.get("leverage") or 1),
                "atr_value": float(hist.get("atr_value") or 0),
                "reason": reason,
                "score": float(latest.get("composite_score") or hist.get("entry_score") or 0),
                "run_id": run_id,
                "position_id": hist.get("position_id"),
                "strategy_source": strategy_source,
                "signal_source": hist.get("signal_source"),
                "alpha_symbol": hist.get("alpha_symbol"),
                "alpha_profile": hist.get("alpha_profile"),
                "alpha_entry_level": hist.get("alpha_entry_level"),
                "alpha_score": hist.get("alpha_score"),
                "alpha_suggested_position_pct": hist.get("alpha_suggested_position_pct"),
                "roll_layer": 1,
                "current_r": decision.current_r,
                "risk_before": {
                    "quantity": current_qty,
                    "current_r": decision.current_r,
                    "current_entry": entry_price,
                },
                "risk_after": {
                    "add_qty": add_qty,
                    "estimated_blended_entry": blended_entry,
                    "estimated_protected_stop": stop_price,
                },
            })
            update_position_management(
                sym,
                roll_enabled=1,
                roll_block_reason=None,
            )
            self._record_decision(
                sym,
                run_id=run_id,
                side=side,
                decision_stage="roll_position",
                decision_result="planned_roll_add",
                quantity=add_qty,
                entry_price=mark_price,
                risk_params=actions[-1]["risk_after"],
                reason={"reason": reason},
            )

        return actions

    def decide(self, top_symbols: list, current_positions: list, run_id: str | None = None) -> list:
        """Build open, close and partial-close actions."""
        self.ai_learning_actions = []
        actions = []
        pos_symbols = {p["symbol"] for p in current_positions}
        balance = self.get_balance()
        controls = self.account_controls or _runtime_controls()
        normal_enabled = bool(controls.get("normal_trading_enabled", True))
        alpha_enabled = bool(controls.get("alpha_trading_enabled", False))

        self._sync_tracker(current_positions)
        actions.extend(self._build_position_actions(top_symbols, current_positions, run_id=run_id))
        actions.extend(self._build_roll_actions(top_symbols, current_positions, actions, balance, run_id=run_id))

        # === 1. 妫鏌ュ凡鏈夋寔浠?===
        for pos in current_positions:
            continue
            sym = pos["symbol"]
            entry_price = pos["entry_price"]
            current_price = pos["mark_price"]
            margin = entry_price * pos["quantity"] / max(pos.get("leverage", 1), 1)
            pnl_pct = pos["unrealized_pnl"] / margin * 100 if margin else 0
            
            # === V3.1: 鍥涜薄闄愰昏緫 ===
            if pnl_pct >= 0:
                # --- 鐩堝埄鐘舵?---
                # 鏌ョ湅鏈鏂拌瘎鍒?                latest = None
                for s in top_symbols:
                    if s["symbol"] == sym:
                        latest = _row_to_dict(s)
                        break
                
                if latest:
                    trend_state = latest.get("trend_state", "")
                    chip_phase = latest.get("chip_phase", "")
                    
                    # 瓒嬪娍瀹屽ソ + 鍚哥 鈫?鎸佹湁
                    if "up" in trend_state.lower() and chip_phase in ["accumulation", "reaccumulation"]:
                        # 妫鏌ユ槸鍚﹁Е鍙慣P1/TP2
                        if pnl_pct >= 5:
                            # TP1: 鐩堝埄5%锛屽钩50%
                            actions.append({
                                "action": "partial_close",
                                "symbol": sym,
                                "side": "SELL" if pos["side"] == "LONG" else "BUY",
                                "reason": f"TP1:娴泩{pnl_pct:.1f}%骞?0%",
                                "close_pct": 0.50,
                            })
                        elif pnl_pct >= 10:
                            # TP2: 鐩堝埄10%锛屽钩鍓╀綑50%锛堝叏閮ㄥ钩瀹岋級
                            actions.append({
                                "action": "partial_close",
                                "symbol": sym,
                                "side": "SELL" if pos["side"] == "LONG" else "BUY",
                                "reason": f"TP2:娴泩{pnl_pct:.1f}%骞冲墿浣?0%",
                                "close_pct": 0.50,
                            })
                        # 鍚﹀垯鎸佹湁
                    else:
                        if pnl_pct >= 5:
                            actions.append({
                                "action": "close",
                                "symbol": sym,
                                "side": "SELL" if pos["side"] == "LONG" else "BUY",
                                "reason": f"take_profit_trend_break:{pnl_pct:.1f}%",
                                "close_price": current_price,
                            })
                        else:
                            pass
                else:
                    if pnl_pct >= 5:
                        actions.append({
                            "action": "partial_close",
                            "symbol": sym,
                            "side": "SELL" if pos["side"] == "LONG" else "BUY",
                            "reason": f"TP1:娴泩{pnl_pct:.1f}%骞?0%",
                            "close_pct": 0.50,
                        })
                    elif pnl_pct >= 10:
                        actions.append({
                            "action": "partial_close",
                            "symbol": sym,
                            "side": "SELL" if pos["side"] == "LONG" else "BUY",
                            "reason": f"TP2:娴泩{pnl_pct:.1f}%骞冲墿浣?0%",
                            "close_pct": 0.50,
                        })
            else:
                # --- 浜忔崯鐘舵?---
                if pnl_pct <= -5:
                    # 瑙﹀強5%姝㈡崯锛岀‖姝㈡崯
                    actions.append({
                        "action": "close",
                        "symbol": sym,
                        "side": "SELL" if pos["side"] == "LONG" else "BUY",
                        "reason": f"纭鎹?娴簭{pnl_pct:.1f}%",
                        "close_price": current_price,
                        "is_stop": True,
                    })
                else:
                    # 鏈Е鍙?%姝㈡崯锛屾寔鏈夛紙淇′换绯荤粺锛?                    # 妫鏌ョЩ鍔ㄦ鐩圓TR脳1.5
                    tracker = self._pos_tracker.get(sym, {})
                    highest_price = tracker.get("highest_price", current_price)
                    atr = pos.get("atr_value", current_price * 0.02)
                    
                    if calc_trailing_stop(current_price, highest_price, atr, 1.5):
                        actions.append({
                            "action": "close",
                            "symbol": sym,
                            "side": "SELL" if pos["side"] == "LONG" else "BUY",
                            "reason": f"trailing_stop high={highest_price:.4f} now={current_price:.4f}",
                            "close_price": current_price,
                        })

        # === 2. 寮鏂颁粨 ===
        for act in actions:
            if act.get("action") in ("close", "partial_close"):
                act["run_id"] = run_id
                self._record_decision(
                    act["symbol"],
                    run_id=run_id,
                    side=act.get("side"),
                    decision_stage="position_management",
                    decision_result=f"planned_{act.get('action')}",
                    filter_reason=act.get("reason"),
                    reason=act,
                )

        avail = self.cfg["max_positions"] - len(current_positions) + len(
            [a for a in actions if a["action"] == "close"]
        )
        
        # Market Regime Engine
        regime = detect_current_regime()
        adj_min_score, adj_max_pos, adj_reason = adjust_strategy_for_regime(
            regime, min_score=self.cfg.get("min_score", 60), max_positions=avail
        )
        logger.info(f"  馃實 甯傚満鐘舵? {get_regime_adjustment_message(regime)} 鈫?{adj_reason}")
        profile_gate_note = (
            f"profile_gate_active; legacy_regime_score={adj_min_score}; "
            "score gate is handled by entry profile"
        )
        bluechip_cfg = _bluechip_cfg()
        bluechip_symbols = {str(x).upper() for x in bluechip_cfg.get("symbols", [])}
        bluechip_open_count = sum(1 for p in current_positions if str(p.get("symbol") or "").upper() in bluechip_symbols)
        bluechip_reserve = 1 if bluechip_cfg.get("enabled", False) and bluechip_open_count < int(bluechip_cfg.get("max_positions", 1)) and avail > 1 else 0

        # === V5: 浣跨敤鍊欓夐夋嫨鍣?===
        if avail > 0 and normal_enabled:
            selector = CandidateSelector()
            normal_slots = max(0, avail - bluechip_reserve)
            candidates = selector.select_candidates(
                top_symbols, current_positions, max_positions=normal_slots
            )
            
            trading = self._get_trading_symbols()
            
            for s in candidates:
                s = _row_to_dict(s)
                sym = s["symbol"]
                
                if sym not in trading:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason="not_tradable_on_exchange",
                    )
                    continue
                if sym in pos_symbols:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason="already_in_position",
                    )
                    continue
                if any(a["symbol"] == sym for a in actions):
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason="already_has_pending_action",
                    )
                    continue

                learning_side = determine_side(s)
                learning_action = build_learning_action(
                    s,
                    side=learning_side,
                    strategy_source="normal",
                    category=(get_symbol_risk(sym) or {}).get("class"),
                )
                if learning_action:
                    self.ai_learning_actions.append(learning_action)

                ok, filter_reason = meets_safety_filters(s)
                if not ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="safety_filter",
                        decision_result="filtered",
                        filter_reason=filter_reason,
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {filter_reason}, 璺宠繃")
                    continue

                score = float(s.get("composite_score") or 0)
                # V5: 绠鍖栨鏌?                can_open, reason = selector.can_open(sym)
                can_open, reason = selector.can_open(sym)
                if not can_open:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason=reason,
                    )
                    logger.info(f"  {sym}: {reason}, 璺宠繃")
                    continue

                # 鏂瑰悜鍒ゅ畾
                side = determine_side(s)
                if side is None:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        decision_stage="side_decision",
                        decision_result="skipped",
                        filter_reason="no_confident_side",
                    )
                    continue

                policy_ok, policy_reason, matched_policies = evaluate_entry_policy(s, side)
                if not policy_ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="entry_policy",
                        decision_result="filtered",
                        filter_reason=policy_reason,
                        risk_params={"matched_policies": matched_policies},
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {policy_reason}, entry policy filtered")
                    continue

                v3_signals = _compute_entry_v3_signals(sym, s)
                if v3_signals.get("error"):
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="entry_profile",
                        decision_result="filtered",
                        filter_reason=f"entry_profile_signal_error:{v3_signals.get('error')}",
                        risk_params=v3_signals,
                        market_regime=regime,
                    )
                    continue
                entry_profile = evaluate_profile_entry(s, v3_signals, side)
                if entry_profile.get("status") not in ("pass", "probe"):
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="entry_profile",
                        decision_result="observe" if entry_profile.get("status") == "observe" else "filtered",
                        filter_reason=entry_profile.get("reason"),
                        risk_params={
                            "entry_profile": entry_profile,
                            "v3_signals": v3_signals,
                            "gate_mode": "per_symbol_entry_profile",
                            "regime_gate_note": profile_gate_note,
                        },
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {entry_profile.get('reason')}, entry profile {entry_profile.get('status')}")
                    continue

                market_phase = _market_phase_gate(_raw_features(s))
                phase_ok, phase_status, phase_reason = _market_phase_entry_decision(
                    market_phase,
                    entry_profile.get("status"),
                )
                if not phase_ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="market_phase",
                        decision_result="filtered",
                        filter_reason=phase_reason,
                        risk_params={"market_phase": market_phase},
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {phase_reason}, market phase filtered")
                    continue
                if phase_status != entry_profile.get("status"):
                    entry_profile = {
                        **entry_profile,
                        "status": phase_status,
                        "reason": f"{entry_profile.get('reason')} · {phase_reason}",
                        "market_phase": market_phase,
                    }
                elif market_phase:
                    entry_profile = {**entry_profile, "market_phase": market_phase}

                layer_ok, layer_reason, score_layers = _score_layer_gate(s, entry_profile)
                if not layer_ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="score_layers",
                        decision_result="filtered",
                        filter_reason=layer_reason,
                        risk_params={
                            "entry_profile": entry_profile,
                            "score_layers": score_layers,
                            "v3_signals": v3_signals,
                        },
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {layer_reason}, score layer filtered")
                    continue

                try:
                    ob_ok, ob_reason, ob_info = self._check_live_orderbook(sym, side, entry_profile)
                except Exception as e:
                    ob_ok, ob_reason, ob_info = False, f"binance depth error: {e}", {}
                if not ob_ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason=ob_reason,
                        risk_params=ob_info,
                    )
                    logger.info(f"  {sym}: {ob_reason}, skip")
                    continue
                if ob_info.get("spread_degraded"):
                    logger.info(f"  {sym}: {ob_reason}, allow with testnet spread profile")

                price = s.get("price", 0) or s.get("market_price", 0)
                if price <= 0:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="candidate_filter",
                        decision_result="filtered",
                        filter_reason="invalid_price",
                    )
                    continue

                symbol_risk = entry_profile.get("risk_profile") or get_symbol_risk(sym)
                sizing_mode = "probe" if entry_profile.get("status") == "probe" else "confirmed"
                size_multiplier = 0.75 if ob_info.get("spread_degraded") else 1.0
                if market_phase.get("phase") == "range":
                    size_multiplier *= 0.75
                pos_info = calculate_position(
                    self.ex,
                    sym,
                    price,
                    balance,
                    score,
                    category=symbol_risk.get("class"),
                    entry_mode=sizing_mode,
                    size_multiplier=size_multiplier,
                )
                pos_info["profile_position_size_factor"] = size_multiplier
                pos_info["symbol_risk"] = entry_profile.get("risk_profile")
                pos_info["spread_degraded"] = bool(ob_info.get("spread_degraded"))
                qty = pos_info["quantity"]
                if qty <= 0:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        decision_stage="risk_sizing",
                        decision_result="filtered",
                        filter_reason="quantity <= 0",
                        risk_params=pos_info,
                    )
                    continue

                lev = pos_info.get("leverage", 3)
                stop_distance = float(pos_info.get("stop_loss") or price * float(pos_info.get("stop_pct") or 0.05))
                stop_price = (price - stop_distance) if side == "LONG" else (price + stop_distance)
                tp_levels = calc_tp_levels(price, side, pos_info.get("stop_pct"))
                tp_price = tp_levels["tp2_price"]
                invested = round(price * qty, 2)
                
                logger.info(f"  {sym}: 涔板叆{side} ${invested} x{qty:.4f} @${price:.4f}")
                logger.info(f"    姝㈡崯@{stop_price:.4f} 姝㈢泩@{tp_price:.4f}")

                actions.append({
                    "action": "open",
                    "symbol": sym,
                    "side": "BUY" if side == "LONG" else "SELL",
                    "position_side": side,
                    "quantity": qty,
                    "entry_price": price,
                    "stop_loss": stop_price,
                    "leverage": lev,
                    "tp1_price": tp_levels["tp1_price"],
                    "tp2_price": tp_levels["tp2_price"],
                    "tp1_qty_pct": tp_levels["tp1_qty_pct"],
                    "tp2_qty_pct": tp_levels["tp2_qty_pct"],
                    "atr_value": pos_info["atr_value"],
                    "stop_model": pos_info.get("stop_model"),
                    "stop_pct": pos_info.get("stop_pct"),
                    "trailing_atr_multiplier": pos_info.get("trailing_atr_multiplier"),
                    "reason": f"{'probe_entry:' if entry_profile.get('status') == 'probe' else ''}璇勫垎{score:.1f} {side}",
                    "grade": s.get("grade", ""),
                    "score": score,
                    "invested": invested,
                    "run_id": run_id,
                    "scan_id": s.get("scan_id"),
                    "entry_mode": entry_profile.get("status"),
                    "strategy_source": "normal",
                    "signal_source": entry_profile.get("template"),
                    "category": symbol_risk.get("class"),
                })
                self._record_decision(
                    s,
                    run_id=run_id,
                    side=side,
                    decision_stage="open_decision",
                    decision_result="planned_probe_entry" if entry_profile.get("status") == "probe" else "planned_open",
                    quantity=qty,
                    entry_price=price,
                    risk_params={
                        "leverage": lev,
                        "stop_loss": stop_price,
                        "tp1_price": tp_levels["tp1_price"],
                        "tp2_price": tp_levels["tp2_price"],
                        "atr_value": pos_info["atr_value"],
                        "invested": invested,
                        "entry_profile": entry_profile,
                        "v3_signals": v3_signals,
                        "gate_mode": "per_symbol_entry_profile",
                        "regime_gate_note": profile_gate_note,
                        "score_layers": score_layers,
                        "sizing": pos_info,
                    },
                    reason={"reason": f"score {score:.1f} {side}", "regime": regime, "entry_profile": entry_profile.get("template")},
                    market_regime=regime,
                )
                if len([a for a in actions if a.get("action") == "open"]) >= adj_max_pos:
                    break
        elif avail > 0:
            logger.info("  normal trading runtime switch is OFF; skip normal open candidates")

        open_count = len([a for a in actions if a.get("action") == "open"])
        bluechip_avail = max(0, adj_max_pos - open_count)
        if bluechip_avail > 0 and normal_enabled and bluechip_cfg.get("enabled", False):
            bluechip_selector = BluechipTrendSelector(bluechip_cfg)
            bluechip_candidates = bluechip_selector.select_candidates(
                top_symbols,
                current_positions,
                max_positions=min(bluechip_avail, int(bluechip_cfg.get("max_positions", 1))),
            )
            for rejected in [x for x in bluechip_selector.last_evaluations if x.get("bluechip_reject_reason")][:3]:
                self._record_decision(
                    rejected,
                    run_id=run_id,
                    side="LONG",
                    mode="bluechip_trend",
                    decision_stage="bluechip_trend",
                    decision_result="filtered",
                    filter_reason=rejected.get("bluechip_reject_reason"),
                    risk_params={"metrics": rejected.get("bluechip_metrics")},
                    market_regime=regime,
                )
            trading = self._get_trading_symbols()
            cooldown_selector = CandidateSelector()
            for s in bluechip_candidates:
                s = _row_to_dict(s)
                sym = s["symbol"]
                side = "LONG"
                if sym not in trading:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason="not_tradable_on_exchange",
                        risk_params={"metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue
                if sym in pos_symbols:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason="already_in_position",
                        risk_params={"metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue
                if any(a["symbol"] == sym for a in actions):
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason="already_has_pending_action",
                        risk_params={"metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue
                learning_action = build_learning_action(
                    s,
                    side=side,
                    strategy_source="normal",
                    category="core_bluechip",
                )
                if learning_action:
                    self.ai_learning_actions.append(learning_action)

                can_open, reason = cooldown_selector.can_open(sym)
                if not can_open:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason=reason,
                        risk_params={"metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue

                entry_profile = {
                    "status": "probe" if s.get("bluechip_entry_mode") == "probe" else "pass",
                    "template": "bluechip_trend",
                    "template_name": "Bluechip Trend",
                }
                try:
                    ob_ok, ob_reason, ob_info = self._check_live_orderbook(sym, side, entry_profile)
                except Exception as e:
                    ob_ok, ob_reason, ob_info = False, f"binance depth error: {e}", {}
                if not ob_ok:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason=ob_reason,
                        risk_params={"metrics": s.get("bluechip_metrics"), "orderbook": ob_info},
                        market_regime=regime,
                    )
                    logger.info(f"  {sym}: {ob_reason}, bluechip trend skip")
                    continue

                price = s.get("price", 0) or s.get("market_price", 0)
                if price <= 0:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason="invalid_price",
                        risk_params={"metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue

                score = float(s.get("bluechip_trend_score") or s.get("composite_score") or 0)
                bluechip_mode = "strong" if s.get("bluechip_entry_mode") == "trend_confirmed" else "probe"
                size_multiplier = 0.75 if ob_info.get("spread_degraded") else 1.0
                pos_info = calculate_position(
                    self.ex,
                    sym,
                    price,
                    balance,
                    score,
                    category="core_bluechip",
                    entry_mode=bluechip_mode,
                    size_multiplier=size_multiplier,
                )
                qty = pos_info["quantity"]
                if qty <= 0:
                    self._record_decision(
                        s,
                        run_id=run_id,
                        side=side,
                        mode="bluechip_trend",
                        decision_stage="bluechip_trend",
                        decision_result="filtered",
                        filter_reason="quantity <= 0",
                        risk_params={**pos_info, "metrics": s.get("bluechip_metrics")},
                        market_regime=regime,
                    )
                    continue

                stop_distance = float(pos_info.get("stop_loss") or price * float(pos_info.get("stop_pct") or 0.03))
                stop_price = (price - stop_distance) if side == "LONG" else (price + stop_distance)
                tp_levels = _bluechip_tp_levels(price, side, pos_info.get("stop_pct"))
                invested = round(price * qty, 2)
                action = {
                    "action": "open",
                    "symbol": sym,
                    "side": "BUY",
                    "position_side": side,
                    "quantity": qty,
                    "entry_price": price,
                    "stop_loss": stop_price,
                    "leverage": pos_info.get("leverage", 3),
                    "tp1_price": tp_levels["tp1_price"],
                    "tp2_price": tp_levels["tp2_price"],
                    "tp1_qty_pct": tp_levels["tp1_qty_pct"],
                    "tp2_qty_pct": tp_levels["tp2_qty_pct"],
                    "atr_value": pos_info["atr_value"],
                    "stop_model": pos_info.get("stop_model"),
                    "stop_pct": pos_info.get("stop_pct"),
                    "trailing_atr_multiplier": pos_info.get("trailing_atr_multiplier"),
                    "reason": f"bluechip_trend:{s.get('bluechip_entry_mode')} score={score:.1f}",
                    "grade": s.get("grade") or s.get("composite_summary") or "",
                    "score": score,
                    "invested": invested,
                    "run_id": run_id,
                    "scan_id": s.get("scan_id"),
                    "entry_mode": s.get("bluechip_entry_mode"),
                    "strategy_source": "normal",
                    "signal_source": "bluechip_trend",
                    "category": "core_bluechip",
                }
                actions.append(action)
                self._record_decision(
                    s,
                    run_id=run_id,
                    side=side,
                    mode="bluechip_trend",
                    decision_stage="bluechip_trend",
                    decision_result="planned_probe" if s.get("bluechip_entry_mode") == "probe" else "planned_open",
                    quantity=qty,
                    entry_price=price,
                    risk_params={
                        "metrics": s.get("bluechip_metrics"),
                        "size_factor": size_multiplier,
                        "leverage": pos_info.get("leverage", 3),
                        "sizing": pos_info,
                        "stop_loss": stop_price,
                        "tp1_price": tp_levels["tp1_price"],
                        "tp2_price": tp_levels["tp2_price"],
                        "atr_value": pos_info["atr_value"],
                        "invested": invested,
                        "orderbook": ob_info,
                    },
                    reason={"reason": action["reason"], "regime": regime},
                    market_regime=regime,
                )
                logger.info(f"  {sym}: bluechip trend {s.get('bluechip_entry_mode')} ${invested} x{qty:.4f} @${price:.4f}")
                if len([a for a in actions if a.get("action") == "open"]) >= adj_max_pos:
                    break

        open_count = len([a for a in actions if a.get("action") == "open"])
        alpha_avail = max(0, adj_max_pos - open_count)
        if alpha_enabled:
            alpha_actions = self._build_alpha_open_actions(
                current_positions,
                balance,
                alpha_avail,
                run_id=run_id,
            )
            if alpha_actions:
                actions.extend(alpha_actions)
        elif alpha_avail > 0:
            logger.info("  alpha trading runtime switch is OFF; skip alpha open candidates")

        return actions

    def execute(self, actions: list) -> list:
        """Execute planned actions."""
        results = []
        for act in actions:
            try:
                if act["action"] == "open":
                    from shared.db import is_market_entry_ready
                    ready, data_error = is_market_entry_ready(
                        act["symbol"],
                        act.get("strategy_source", "normal"),
                        act.get("alpha_symbol"),
                    )
                    if not ready:
                        self._record_decision(
                            act["symbol"], run_id=act.get("run_id"), scan_id=act.get("scan_id"),
                            side=act.get("position_side"), decision_stage="execution",
                            decision_result="blocked", filter_reason="market_data_not_ready",
                            reason={"data_error": data_error},
                        )
                        logger.warning("  skip open %s: market_data_not_ready (%s)", act["symbol"], data_error)
                        results.append({"status": "blocked", "error": "market_data_not_ready", "data_error": data_error, **act})
                        continue
                    self._execute_open(act, results)
                elif act["action"] == "roll_add":
                    self._execute_roll_add(act, results)
                elif act["action"] == "close":
                    self._execute_close(act, results)
                elif act["action"] == "partial_close":
                    if self._execute_partial_close(act, results):
                        self._mark_partial_close_state(act)
            except Exception as e:
                self._record_decision(
                    act.get("symbol", "?"),
                    run_id=act.get("run_id"),
                    scan_id=act.get("scan_id"),
                    side=act.get("position_side") or act.get("side"),
                    decision_stage="execution",
                    decision_result="error",
                    filter_reason=str(e),
                    quantity=act.get("quantity"),
                    entry_price=act.get("entry_price"),
                    reason=act,
                )
                logger.error(f"  鎵ц澶辫触 {act['symbol']}: {e}")
                results.append({"status": "error", "error": str(e), **act})
        return results

    def _mark_partial_close_state(self, act):
        try:
            from shared.db import update_position_management

            positions = self.ex.get_positions()
            pos = next((p for p in positions if p["symbol"] == act["symbol"]), None)
            reason = str(act.get("reason", ""))
            updates = {"last_exit_reason": reason}
            if pos:
                updates["quantity"] = pos.get("quantity")
            if "TP1" in reason or "score_decay_qtr" in reason:
                updates["tp1_hit"] = 1
            if "TP2" in reason or "score_decay_half" in reason:
                updates["tp2_hit"] = 1
            regime_match = re.search(
                r"alpha_volume_regime_profit_protect\s+regime=(suspicious|overheated|extreme)",
                reason,
            )
            if regime_match:
                updates["alpha_volume_protect_regime"] = regime_match.group(1)
                updates["alpha_volume_protect_time"] = datetime.now(timezone.utc).isoformat()
            update_position_management(act["symbol"], **updates)
        except Exception as e:
            logger.warning(f"    position management update failed: {e}")

    def _execute_open(self, act, results):
        logger.info(f"  寮浠?{act['position_side']} {act['symbol']} x{act['quantity']} @${act['entry_price']:.4f} 鎶曞叆${act.get('invested',0):.2f}")
        self.ex.set_leverage(act["symbol"], act.get("leverage", 3))
        try:
            from shared.db import new_position_id
            act["position_id"] = act.get("position_id") or new_position_id(act["symbol"], act["position_side"])
        except Exception:
            pass
        order = self.ex.place_market_order(act["symbol"], act["side"], act["quantity"])
        try:
            from shared.db import insert_order
            insert_order(
                act["symbol"],
                act["side"],
                "MARKET",
                act["quantity"],
                act["entry_price"],
                status="submitted",
                reason=act.get("reason"),
                position_id=act.get("position_id"),
                strategy_source=act.get("strategy_source", "normal"),
                signal_source=act.get("signal_source"),
                alpha_symbol=act.get("alpha_symbol"),
                alpha_profile=act.get("alpha_profile"),
                alpha_entry_level=act.get("alpha_entry_level"),
                alpha_score=act.get("alpha_score"),
                alpha_suggested_position_pct=act.get("alpha_suggested_position_pct"),
            )
        except Exception as e:
            logger.warning(f"    local order write failed: {e}")
        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            scan_id=act.get("scan_id"),
            side=act.get("position_side"),
            decision_stage="execution",
            decision_result="opened",
            quantity=act.get("quantity"),
            entry_price=act.get("entry_price"),
            risk_params={
                "leverage": act.get("leverage"),
                "stop_loss": act.get("stop_loss"),
                "tp1_price": act.get("tp1_price"),
                "tp2_price": act.get("tp2_price"),
                "atr_value": act.get("atr_value"),
                "order_id": order.get("orderId"),
                "ai_quality_status": act.get("ai_quality_status"),
                "ai_quality_decision": act.get("ai_quality_decision"),
                "ai_quality_score": act.get("ai_quality_score"),
                "ai_model_version": act.get("ai_model_version"),
                "ai_quality_reasons": act.get("ai_quality_reasons"),
            },
            reason={"reason": act.get("reason")},
        )
        logger.info(f"    寮浠撴垚浜? {order.get('orderId')}")

        # 鎸傛鎹熷崟
        stop_side = "SELL" if act["position_side"] == "LONG" else "BUY"
        stop_order = self.ex.place_stop_order(act["symbol"], stop_side, act["quantity"], act["stop_loss"])
        try:
            from shared.db import insert_order
            insert_order(
                act["symbol"],
                stop_side,
                "STOP_MARKET",
                act["quantity"],
                act["stop_loss"],
                status="submitted",
                reason="stop_loss",
                position_id=act.get("position_id"),
                strategy_source=act.get("strategy_source", "normal"),
                signal_source=act.get("signal_source"),
                alpha_symbol=act.get("alpha_symbol"),
                alpha_profile=act.get("alpha_profile"),
                alpha_entry_level=act.get("alpha_entry_level"),
                alpha_score=act.get("alpha_score"),
                alpha_suggested_position_pct=act.get("alpha_suggested_position_pct"),
            )
        except Exception as e:
            logger.warning(f"    local stop order write failed: {e}")
        logger.info(f"    姝㈡崯鎸傚崟 @${act['stop_loss']:.4f}: {stop_order.get('orderId')}")

        # 璁板綍寮浠撳喎鍗?30鍒嗛挓)
        record_profit(act['symbol'], is_weak_exit=False)
        try:
            from shared.db import new_position_id, upsert_position_history
            position_id = act.get("position_id") or new_position_id(act["symbol"], act["position_side"])
            upsert_position_history(
                act["symbol"],
                act["position_side"],
                act["quantity"],
                act["entry_price"],
                act.get("reason", ""),
                act.get("score", 0),
                act.get("tp2_price", 0),
                act.get("atr_value", 0),
                position_id=position_id,
                strategy_source=act.get("strategy_source", "normal"),
                signal_source=act.get("signal_source"),
                alpha_symbol=act.get("alpha_symbol"),
                alpha_profile=act.get("alpha_profile"),
                alpha_entry_level=act.get("alpha_entry_level"),
                alpha_score=act.get("alpha_score"),
                alpha_suggested_position_pct=act.get("alpha_suggested_position_pct"),
                stop_model=act.get("stop_model"),
                initial_stop_loss=act.get("stop_loss"),
                stop_pct=act.get("stop_pct"),
                trailing_atr_multiplier=act.get("trailing_atr_multiplier"),
            )
            act["position_id"] = position_id
            try:
                from shared.db import record_entry_review_snapshot
                from shared.policy_loop import build_execution_entry_snapshot

                record_entry_review_snapshot(build_execution_entry_snapshot(act))
            except Exception as snapshot_error:
                logger.warning(f"    entry review snapshot write failed: {snapshot_error}")
        except Exception as e:
            logger.warning(f"    position history write failed: {e}")
        
        # 杩借釜鐘舵佸垵濮嬪寲
        self._pos_tracker[act["symbol"]] = {
            "highest_price": act["entry_price"],
            "lowest_price": act["entry_price"],
            "entry_price": act["entry_price"],
        }
        results.append({"status": "ok", **act})

    def _execute_roll_add(self, act, results):
        from shared.db import insert_order, record_position_roll_event, update_position_management
        from trader.roll_policy import calculate_protected_stop

        logger.info(
            f"  滚仓 {act['position_side']} {act['symbol']} layer={act.get('roll_layer')} "
            f"x{act['quantity']} @${act['entry_price']:.4f}"
        )
        before_positions = self.ex.get_positions()
        before = next((p for p in before_positions if p["symbol"] == act["symbol"]), None)
        before_qty = float(before.get("quantity") or 0) if before else 0.0
        order = self.ex.place_market_order(act["symbol"], act["side"], act["quantity"])
        try:
            insert_order(
                act["symbol"],
                act["side"],
                "MARKET",
                act["quantity"],
                act["entry_price"],
                status="submitted",
                reason=act.get("reason"),
                position_id=act.get("position_id"),
                strategy_source=act.get("strategy_source", "normal"),
                signal_source=act.get("signal_source"),
                alpha_symbol=act.get("alpha_symbol"),
                alpha_profile=act.get("alpha_profile"),
                alpha_entry_level=act.get("alpha_entry_level"),
                alpha_score=act.get("alpha_score"),
                alpha_suggested_position_pct=act.get("alpha_suggested_position_pct"),
            )
        except Exception as e:
            logger.warning(f"    local roll order write failed: {e}")

        try:
            confirmed_add_qty = float((order or {}).get("executedQty") or 0)
        except (TypeError, ValueError):
            confirmed_add_qty = 0.0
        positions = self.ex.get_positions()
        pos = next((p for p in positions if p["symbol"] == act["symbol"]), None)
        if not pos:
            raise RuntimeError(f"roll add position refresh failed: {act['symbol']}")
        actual_qty = float(pos.get("quantity") or 0)
        if confirmed_add_qty <= 0:
            confirmed_add_qty = max(0.0, actual_qty - before_qty)
        if confirmed_add_qty <= 0 or actual_qty <= before_qty:
            raise RuntimeError(f"roll add execution unconfirmed: {act['symbol']}")

        actual_entry = float(pos.get("entry_price") or 0)
        mark_price = float(pos.get("mark_price") or 0)
        protected_stop = calculate_protected_stop(
            act["position_side"], actual_entry, self.cfg.get("roll_trading") or {}
        )
        stop_is_valid = (
            act["position_side"] == "LONG" and 0 < protected_stop < mark_price
        ) or (
            act["position_side"] == "SHORT" and protected_stop > mark_price > 0
        )
        stop_side = "SELL" if act["position_side"] == "LONG" else "BUY"
        try:
            if not stop_is_valid:
                raise RuntimeError(
                    f"protected stop would trigger immediately: stop={protected_stop} mark={mark_price}"
                )
            stop_order = self.ex.place_stop_order(
                act["symbol"], stop_side, actual_qty, protected_stop
            )
            if not (stop_order or {}).get("orderId") and not (stop_order or {}).get("algoId"):
                raise RuntimeError("protective stop acknowledgement missing")
        except Exception as e:
            try:
                self.ex.close_position_market(act["symbol"], stop_side, confirmed_add_qty)
            finally:
                update_position_management(
                    act["symbol"], roll_enabled=0, roll_block_reason="roll_protection_failed"
                )
                self._record_decision(
                    act["symbol"], run_id=act.get("run_id"), side=act.get("position_side"),
                    decision_stage="execution", decision_result="roll_protection_failed",
                    quantity=confirmed_add_qty, reason={"error": str(e)},
                )
            raise RuntimeError(f"roll protection failed: {e}") from e

        try:
            self.ex.cancel_other_protective_stops(
                act["symbol"], (stop_order or {}).get("algoId") or (stop_order or {}).get("orderId")
            )
        except Exception as e:
            logger.warning(f"    old protective stop cleanup failed: {e}")

        try:
            insert_order(
                act["symbol"], stop_side, "STOP_MARKET", actual_qty, protected_stop,
                status="submitted", reason="roll_full_position_protection",
                position_id=act.get("position_id"),
                strategy_source=act.get("strategy_source", "normal"),
                signal_source=act.get("signal_source"),
                alpha_symbol=act.get("alpha_symbol"),
                alpha_profile=act.get("alpha_profile"),
                alpha_entry_level=act.get("alpha_entry_level"),
                alpha_score=act.get("alpha_score"),
                alpha_suggested_position_pct=act.get("alpha_suggested_position_pct"),
            )
        except Exception as e:
            logger.warning(f"    local roll protection order write failed: {e}")

        roll_price = float((order or {}).get("avgPrice") or act.get("entry_price") or 0)
        update_position_management(
            act["symbol"],
            roll_layer=1,
            last_roll_time=datetime.now(timezone.utc).isoformat(),
            roll_price=roll_price,
            protected_stop=protected_stop,
            current_stop_loss=protected_stop,
            trailing_enabled=1,
            trailing_atr_multiplier=float((self.cfg.get("roll_trading") or {}).get("trailing_atr_multiplier", 2.0)),
            roll_enabled=1,
            roll_block_reason=None,
            last_exit_reason=act.get("reason"),
            quantity=actual_qty,
            entry_price=actual_entry,
        )
        record_position_roll_event(
            symbol=act["symbol"], position_side=act.get("position_side"),
            strategy_source=act.get("strategy_source", "normal"), roll_layer=1,
            roll_qty=confirmed_add_qty, roll_price=roll_price, roll_reason=act.get("reason"),
            position_id=act.get("position_id"), risk_before=act.get("risk_before"),
            risk_after={**(act.get("risk_after") or {}), "protected_stop": protected_stop, "total_quantity": actual_qty},
        )

        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            side=act.get("position_side"),
            decision_stage="execution",
            decision_result="rolled_add",
            quantity=act.get("quantity"),
            entry_price=roll_price,
            risk_params=act.get("risk_after"),
            reason={"reason": act.get("reason"), "order_id": order.get("orderId")},
        )
        logger.info(f"    滚仓成交并已全仓保护: {order.get('orderId')} stop={protected_stop:.6f}")
        results.append({"status": "ok", **act, "roll_price": roll_price, "protected_stop": protected_stop})

    def _execute_close(self, act, results):
        logger.info(f"  骞充粨 {act['symbol']}: {act['reason']}")
        before = self.ex.get_positions()
        pos = next((p for p in before if p["symbol"] == act["symbol"]), None)
        if not pos:
            logger.warning(f"    {act['symbol']}: no current position found")
            return
        self.ex.close_position_market(act["symbol"], act["side"], pos["quantity"])
        from shared.db import delete_position_history, get_position_history, record_trade
        hist = get_position_history(act["symbol"]) or {}
        pnl = pos.get("unrealized_pnl", 0)
        margin = pos["entry_price"] * pos["quantity"] / max(pos.get("leverage", 1), 1)
        pnl_pct = round(pnl / margin * 100, 2) if margin else 0
        record_trade(
            symbol=act["symbol"], side=pos["side"],
            qty=pos["quantity"], entry_price=pos["entry_price"],
            exit_price=act.get("close_price", pos["mark_price"]),
            pnl=round(pnl, 2), pnl_pct=pnl_pct,
            exit_reason=act.get("reason", ""),
            grade=act.get("grade", ""), score=act.get("score", 0),
            entry_reason=hist.get("entry_reason"),
            position_id=hist.get("position_id"),
            strategy_source=hist.get("strategy_source") or act.get("strategy_source", "normal"),
            signal_source=hist.get("signal_source") or act.get("signal_source"),
            alpha_symbol=hist.get("alpha_symbol") or act.get("alpha_symbol"),
            alpha_profile=hist.get("alpha_profile") or act.get("alpha_profile"),
            alpha_entry_level=hist.get("alpha_entry_level") or act.get("alpha_entry_level"),
            alpha_score=hist.get("alpha_score") or act.get("alpha_score"),
            alpha_suggested_position_pct=hist.get("alpha_suggested_position_pct") or act.get("alpha_suggested_position_pct"),
        )
        if (hist.get("strategy_source") or act.get("strategy_source")) == "alpha":
            _write_alpha_post_close_cooldown(
                act["symbol"],
                pnl=pnl,
                reason=act.get("reason", ""),
                is_stop=bool(act.get("is_stop")),
                prefix="alpha execution close",
            )
        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            side=pos.get("side"),
            decision_stage="execution",
            decision_result="closed",
            quantity=pos.get("quantity"),
            entry_price=pos.get("entry_price"),
            price=act.get("close_price", pos.get("mark_price")),
            reason={"exit_reason": act.get("reason", ""), "pnl": pnl, "pnl_pct": pnl_pct},
        )
        delete_position_history(act["symbol"])
        logger.info(f"    PnL={pnl:.2f} ({pnl_pct:.1f}%) {act['reason']}")
        
        if act.get("is_stop"):
            record_stop(act["symbol"], pnl)
            logger.info(f"    姝㈡崯鍐峰嵈: {act['symbol']} 24h")
        
        if act["symbol"] in self._pos_tracker:
            del self._pos_tracker[act["symbol"]]
        results.append({"status": "ok", **act})

    def _execute_partial_close(self, act, results):
        """Partially close a position."""
        positions = self.ex.get_positions()
        pos = next((p for p in positions if p["symbol"] == act["symbol"]), None)
        if not pos:
            logger.warning(f"  {act['symbol']}: 鍑忎粨鏃舵湭鎵惧埌鎸佷粨")
            return False

        pct = act.get("close_pct", 0.50)
        close_qty = round(pos["quantity"] * pct, 3)
        if close_qty <= 0:
            return False

        cleanup_residual = False
        try:
            from trader.roll_policy import is_residual_position

            remaining_qty = max(0.0, float(pos["quantity"]) - close_qty)
            cleanup_residual = remaining_qty > 0 and is_residual_position(
                quantity=remaining_qty,
                mark_price=float(pos.get("mark_price") or 0),
                leverage=float(pos.get("leverage") or 1),
                exchange_info=self.ex.get_symbol_info(act["symbol"]),
                config=self.cfg.get("roll_trading") or {},
            )
        except Exception as e:
            logger.warning(f"  {act['symbol']}: residual prediction unavailable: {e}")
        if cleanup_residual:
            close_qty = float(pos["quantity"])
            pct = 1.0
            act["close_pct"] = 1.0
            act["reason"] = "residual_position_cleanup"

        logger.info(
            f"  鍑忎粨 {act['symbol']}: {pct*100:.0f}% (x{close_qty}) {act.get('reason', '')}"
        )
        order = self.ex.close_position_market(act["symbol"], act["side"], close_qty)
        try:
            executed_qty = float((order or {}).get("executedQty") or close_qty)
        except Exception:
            executed_qty = 0.0
        if executed_qty <= 0:
            after_positions = self.ex.get_positions()
            after_pos = next(
                (item for item in after_positions if item["symbol"] == act["symbol"]),
                None,
            )
            remaining_qty = float(after_pos.get("quantity") or 0) if after_pos else 0.0
            executed_qty = max(0.0, float(pos["quantity"]) - remaining_qty)
            if executed_qty <= 0:
                raise RuntimeError(
                    f"partial close execution unconfirmed: {act['symbol']} "
                    f"order_id={(order or {}).get('orderId')}"
                )
        try:
            exit_price = float((order or {}).get("avgPrice") or pos["mark_price"])
        except Exception:
            exit_price = pos["mark_price"]
        if exit_price <= 0:
            exit_price = pos["mark_price"]

        if cleanup_residual:
            after_positions = self.ex.get_positions()
            after_pos = next(
                (item for item in after_positions if item["symbol"] == act["symbol"]),
                None,
            )
            remaining_qty = float(after_pos.get("quantity") or 0) if after_pos else 0.0
            if remaining_qty > 0:
                cleanup_order = self.ex.close_position_market(
                    act["symbol"], act["side"], remaining_qty
                )
                try:
                    cleanup_executed = float((cleanup_order or {}).get("executedQty") or 0)
                except (TypeError, ValueError):
                    cleanup_executed = 0.0
                if cleanup_executed <= 0:
                    raise RuntimeError(
                        f"residual cleanup execution unconfirmed: {act['symbol']}"
                    )
                cleanup_price = float((cleanup_order or {}).get("avgPrice") or exit_price)
                exit_price = (
                    (exit_price * executed_qty + cleanup_price * cleanup_executed)
                    / (executed_qty + cleanup_executed)
                )
                executed_qty += cleanup_executed

            verified_positions = self.ex.get_positions()
            verified = next(
                (item for item in verified_positions if item["symbol"] == act["symbol"]),
                None,
            )
            if verified and float(verified.get("quantity") or 0) > 0:
                raise RuntimeError(
                    f"residual cleanup left open quantity: {act['symbol']} "
                    f"qty={verified.get('quantity')}"
                )

        # Local PnL is an estimate; the exchange income ledger reconciles the final value.
        margin = pos["entry_price"] * pos["quantity"] / max(pos.get("leverage", 1), 1)
        executed_pct = executed_qty / pos["quantity"] if pos["quantity"] else 0
        pnl = pos["unrealized_pnl"] * executed_pct
        pnl_pct_v = round(pnl / (margin * executed_pct) * 100, 2) if margin and executed_pct else 0
        try:
            from shared.db import get_position_history, record_trade

            hist = get_position_history(act["symbol"]) or {}
            record_trade(
                symbol=act["symbol"], side=pos["side"],
                qty=executed_qty, entry_price=pos["entry_price"],
                exit_price=exit_price,
                pnl=round(pnl, 2), pnl_pct=pnl_pct_v,
                exit_reason=act.get("reason", ""),
                grade=act.get("grade", ""), score=act.get("score", 0),
                entry_reason=hist.get("entry_reason"),
                position_id=hist.get("position_id"),
                strategy_source=hist.get("strategy_source") or act.get("strategy_source", "normal"),
                signal_source=hist.get("signal_source") or act.get("signal_source"),
                alpha_symbol=hist.get("alpha_symbol") or act.get("alpha_symbol"),
                alpha_profile=hist.get("alpha_profile") or act.get("alpha_profile"),
                alpha_entry_level=hist.get("alpha_entry_level") or act.get("alpha_entry_level"),
                alpha_score=hist.get("alpha_score") or act.get("alpha_score"),
                alpha_suggested_position_pct=hist.get("alpha_suggested_position_pct") or act.get("alpha_suggested_position_pct"),
            )
        except Exception as e:
            logger.error(
                "Partial close succeeded but local trade write failed for %s: %s",
                act["symbol"],
                e,
            )
        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            side=pos.get("side"),
            decision_stage="execution",
            decision_result="partial_closed",
            quantity=executed_qty,
            entry_price=pos.get("entry_price"),
            price=exit_price,
            reason={"exit_reason": act.get("reason", ""), "pnl": pnl, "pnl_pct": pnl_pct_v},
        )

        logger.info(f"    鍑忎粨鎴愪氦: x{executed_qty}")
        results.append({"status": "ok", **act})
        if cleanup_residual:
            try:
                from shared.db import delete_position_history

                delete_position_history(act["symbol"])
            except Exception as e:
                logger.warning(f"    residual position history cleanup failed: {e}")
            self._pos_tracker.pop(act["symbol"], None)
        return True
