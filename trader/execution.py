"""Live execution engine."""
import json
import logging
from datetime import datetime, timezone

from trader.exchange import BinanceFutures
from trader.risk import (
    calculate_position, determine_side, meets_safety_filters,
    calc_tp_levels, calc_trailing_stop, evaluate_entry_policy
)
from trader.entry_profiles import evaluate_profile_entry
from trader.config import EXCHANGE_CONFIG, TRADING_CONFIG
from trader.cooldown_manager import is_in_cooldown, record_stop, record_profit
from trader.market_regime import detect_current_regime, adjust_strategy_for_regime, get_regime_adjustment_message
from trader.selection import CandidateSelector  # V5: 鍊欓夐夋嫨鍣?
from alpha_engine.volume_price import evaluate_alpha_volume_price
logger = logging.getLogger("execution")


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
        self._trading_symbols = None
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
            current_high = max(stored_high, float(p.get("mark_price") or 0))
            if sym in self._pos_tracker:
                current_high = max(current_high, float(self._pos_tracker[sym].get("highest_price") or 0))
            self._pos_tracker[sym] = {
                "highest_price": current_high,
                "entry_price": p["entry_price"],
            }
            update_position_management(sym, highest_price=current_high, quantity=p.get("quantity"))

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

        def add(reason, is_stop=False, score=None):
            item = {
                "action": "close",
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
            if is_stop:
                item["is_stop"] = True
            return item

        if pnl_pct <= -float(self.cfg.get("hard_stop_pct", 0.05)) * 100:
            return add(f"alpha_hard_stop pnl={pnl_pct:.1f}%", is_stop=True)

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
        max_spread_pct = float((_alpha_cfg() or {}).get("max_spread_pct", 0.0012)) * 100

        weak_states = {"failed_breakout", "distribution", "dumping", "breakdown"}
        if vp_state in weak_states and pnl_pct <= 2:
            return add(f"alpha_volume_price_failed state={vp_state} pnl={pnl_pct:.1f}%", score=current_score)
        if vp_action in {"observe", "cooldown"} and pnl_pct <= 0 and (ret_15m < 0 or ret_1h < 0):
            return add(f"alpha_volume_price_weak action={vp_action} state={vp_state} pnl={pnl_pct:.1f}%", score=current_score)
        if spread_pct > max_spread_pct and pnl_pct <= 0:
            return add(f"alpha_spread_widened spread={spread_pct:.3f}% pnl={pnl_pct:.1f}%", score=current_score)
        if side == "LONG" and ret_15m < 0 and ret_1h < 0 and ret_6h < 0 and pnl_pct <= 1:
            return add(f"alpha_long_momentum_reversal ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}%", score=current_score)
        if side == "SHORT" and ret_15m > 0 and ret_1h > 0 and ret_6h > 0 and pnl_pct <= 1:
            return add(f"alpha_short_momentum_reversal ret15={ret_15m:.2f}% ret1h={ret_1h:.2f}% ret6h={ret_6h:.2f}%", score=current_score)

        time_stop_h = float(self.cfg.get("time_stop_hours", 12))
        min_ret = float(self.cfg.get("time_stop_min_return", 0.02)) * 100
        if age_h is not None and age_h >= time_stop_h and pnl_pct < min_ret and vp_action not in {"normal_review", "normal_review_probe"}:
            return add(f"alpha_time_stop age={age_h:.1f}h pnl={pnl_pct:.1f}% state={vp_state}", score=current_score)
        if pnl_pct >= float(self.cfg.get("tp2_target_pct", 0.10)) * 100 and calc_trailing_stop(mark_price, highest_price, atr, self.cfg.get("trailing_stop_atr_multiplier", 1.5)):
            return add(f"alpha_trailing_stop high={highest_price:.4f} now={mark_price:.4f}", score=current_score)

        hold_reason = f"alpha hold volume_price={vp_state or '-'} action={vp_action or '-'} score={current_score:.1f}"
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
            atr = float(hist.get("atr_value") or pos.get("atr_value") or mark_price * 0.02)

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

            def add(action, reason, close_pct=None, is_stop=False):
                item = {
                    "action": action,
                    "symbol": sym,
                    "side": close_side,
                    "reason": reason,
                    "close_price": mark_price,
                    "score": score,
                }
                if close_pct is not None:
                    item["close_pct"] = close_pct
                if is_stop:
                    item["is_stop"] = True
                actions.append(item)

            if pnl_pct <= -float(self.cfg.get("hard_stop_pct", 0.05)) * 100:
                add("close", f"hard_stop pnl={pnl_pct:.1f}%", is_stop=True)
                continue
            if robot_signature and hold_alpha < 55:
                add("close", f"orderbook_robot_signature hold_alpha={hold_alpha:.1f}")
                continue
            if hold_alpha <= 25:
                add("close", f"hold_alpha_collapse {hold_alpha:.1f}")
                continue
            if hold_alpha <= 35 and pnl_pct < 2:
                add("close", f"hold_alpha_weak {hold_alpha:.1f}, pnl={pnl_pct:.1f}%")
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_full", 40)):
                add("close", f"score_decay_full entry={entry_score:.1f} now={score:.1f}")
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_half", 30)) and not int(hist.get("tp2_hit") or 0):
                add("partial_close", f"score_decay_half entry={entry_score:.1f} now={score:.1f}", 0.50)
                continue
            if score_decay >= float(self.cfg.get("score_decay_exit_qtr", 20)) and not int(hist.get("tp1_hit") or 0):
                add("partial_close", f"score_decay_qtr entry={entry_score:.1f} now={score:.1f}", 0.25)
                continue
            if int(hist_perf.get("total") or 0) >= 5 and (expectancy <= 0 or total_pnl < 0 or profit_factor < 1):
                if hold_alpha < 45 or pnl_pct <= 0:
                    add("close", f"history_expectancy_turns_bad exp={expectancy:.4f} pf={profit_factor:.2f}")
                    continue
            if p_drawdown >= 0.60 and pnl_pct <= 1:
                add("close", f"drawdown_risk {p_drawdown:.2f}, pnl={pnl_pct:.1f}%")
                continue
            if depth_score < 35 and pnl_pct <= 1:
                add("close", f"orderbook_depth_weak score={depth_score:.1f}")
                continue

            time_stop_h = float(self.cfg.get("time_stop_hours", 12))
            min_ret = float(self.cfg.get("time_stop_min_return", 0.02)) * 100
            if age_h is not None and age_h >= time_stop_h and pnl_pct < min_ret:
                add("close", f"time_stop age={age_h:.1f}h pnl={pnl_pct:.1f}%")
                continue
            if side == "LONG" and (ret_6h < 0 and ret_24h < 0) and hold_alpha < 50 and pnl_pct <= 2:
                add("close", f"long_momentum_reversal hold={hold_alpha:.1f}")
                continue
            if side == "SHORT" and (ret_6h > 0 and ret_24h > 0) and hold_alpha < 50 and pnl_pct <= 2:
                add("close", f"short_momentum_reversal hold={hold_alpha:.1f}")
                continue
            if pnl_pct >= float(self.cfg.get("tp2_target_pct", 0.10)) * 100 and not int(hist.get("tp2_hit") or 0):
                add("partial_close", f"TP2 pnl={pnl_pct:.1f}%", float(self.cfg.get("tp2_pct", 0.50)))
                continue
            if pnl_pct >= float(self.cfg.get("tp1_target_pct", 0.05)) * 100 and not int(hist.get("tp1_hit") or 0):
                add("partial_close", f"TP1 pnl={pnl_pct:.1f}%", float(self.cfg.get("tp1_pct", 0.50)))
                continue
            if pnl_pct > 0 and calc_trailing_stop(mark_price, highest_price, atr, self.cfg.get("trailing_stop_atr_multiplier", 1.5)):
                add("close", f"trailing_stop high={highest_price:.4f} now={mark_price:.4f}")

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
        if avail <= 0:
            return []

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
        if remaining_slots <= 0:
            return []

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

            cooldown = get_alpha_cooldown(symbol) or get_alpha_cooldown("*")
            if cooldown:
                reject(f"alpha cooldown active: {cooldown.get('reason')} until {cooldown.get('cooldown_until')}")
                continue

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

            if volume_price.get("allow_short") and not volume_price.get("allow_long"):
                side = "SHORT"
            elif volume_price.get("allow_long") and not volume_price.get("allow_short"):
                side = "LONG"
            else:
                reject(f"volume_price has no executable side: {volume_price.get('state')}", {"volume_price": volume_price})
                continue
            if side == "LONG" and not volume_price.get("allow_long"):
                reject(f"volume_price blocks LONG: {volume_price.get('state')}", {"volume_price": volume_price})
                continue
            if side == "SHORT" and not volume_price.get("allow_short"):
                reject(f"volume_price blocks SHORT: {volume_price.get('state')}", {"volume_price": volume_price})
                continue
            if side == "SHORT" and not cfg.get("allow_short", False):
                reject("alpha SHORT disabled by config")
                continue
            action_side = "BUY" if side == "LONG" else "SELL"

            vp_factor = max(0.0, min(1.0, float(volume_price.get("max_position_factor") or 0)))
            entry_status = "probe" if vp_action in ("normal_review_probe", "short_review_only") or vp_factor <= 0.25 else "pass"
            entry_profile = {
                "status": entry_status,
                "template": f"alpha_{volume_price.get('state') or 'volume_price'}",
                "reason": "alpha volume-price gate passed; normal trading review skipped",
                "thresholds": {
                    "position_size_factor": 1.0,
                    "probe_position_size_factor": 1.0,
                },
                "volume_price_state": volume_price.get("state"),
                "volume_price_action": vp_action,
            }
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
            pos_info = calculate_position(self.ex, symbol, price, balance, alpha_execution_score)
            lev = min(int(pos_info.get("leverage") or self.cfg.get("leverage_max", 3)), 3)
            source_factor = 0.25 if entry_profile.get("status") == "probe" else 0.50
            quality_factor = 1.0
            qty = float(pos_info.get("quantity") or 0) * source_factor * quality_factor * vp_factor
            qty = self.ex.adjust_quantity(symbol, qty)
            if ob_info.get("spread_degraded"):
                qty = self.ex.adjust_quantity(symbol, qty * 0.5)
            if qty <= 0:
                reject("quantity <= 0", {"pos_info": pos_info, "source_factor": source_factor, "quality_factor": quality_factor, "volume_price_factor": vp_factor})
                continue

            atr = float(pos_info.get("atr_value") or 0)
            if atr <= 0:
                atr = price * 0.02
            stop_price = (price * 0.95) if side == "LONG" else (price * 1.05)
            tp_levels = calc_tp_levels(price, side, atr)
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
                "alpha_suggested_position_pct": source_factor * quality_factor * vp_factor,
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
                    "source_factor": source_factor,
                    "quality_factor": quality_factor,
                    "volume_price_factor": vp_factor,
                    "volume_price": volume_price,
                    "leverage": lev,
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

        cfg = self.cfg.get("roll_trading") or {}
        if not cfg.get("enabled", False):
            return []

        actions = []
        latest_map = _latest_by_symbol(top_symbols)
        blocked_symbols = {a.get("symbol") for a in planned_actions if a.get("action") in ("close", "partial_close")}
        max_layers = int(cfg.get("max_layers", 2))
        size_factors = list(cfg.get("size_factors") or [0.5, 0.25])
        min_profit_pct = float(cfg.get("min_profit_pct", 5.0))
        max_giveback_pct = float(cfg.get("max_giveback_pct", 35.0))
        cooldown_minutes = float(cfg.get("cooldown_minutes", 60))
        max_15m = float(cfg.get("max_15m_return_pct", 4.0))
        max_1h = float(cfg.get("max_1h_return_pct", 8.0))
        max_spread = float(cfg.get("max_spread_pct", 0.0012))
        alpha_size_factor = float(cfg.get("alpha_size_factor", 0.5))
        spread_degraded_factor = float(cfg.get("spread_degraded_size_factor", 0.5))
        lock_profit_pct = float(cfg.get("lock_profit_pct", 0.30))

        for pos in current_positions:
            sym = pos.get("symbol")
            if not sym or sym in blocked_symbols:
                continue

            hist = get_position_history(sym) or {}
            latest = latest_map.get(sym) or {}
            raw = _raw_features(latest)
            tech = raw.get("technical") or {}
            depth = raw.get("depth") or {}
            strategy_source = hist.get("strategy_source") or "normal"
            side = pos.get("side")
            mark_price = float(pos.get("mark_price") or 0)
            entry_price = float(pos.get("entry_price") or 0)
            quantity = float(pos.get("quantity") or 0)
            leverage = max(float(pos.get("leverage") or 1), 1)
            pnl = float(pos.get("unrealized_pnl") or 0)
            margin = entry_price * quantity / leverage if entry_price and quantity else 0
            pnl_pct = pnl / margin * 100 if margin else 0.0
            roll_layer = int(hist.get("roll_layer") or 0)
            max_floating_pnl = max(float(hist.get("max_floating_pnl") or 0), pnl)
            giveback_pct = ((max_floating_pnl - pnl) / max_floating_pnl * 100) if max_floating_pnl > 0 else 0.0

            def block(reason):
                update_position_management(
                    sym,
                    roll_enabled=0,
                    roll_block_reason=reason,
                    max_floating_pnl=max_floating_pnl,
                )
                self._record_decision(
                    sym,
                    run_id=run_id,
                    side=side,
                    decision_stage="roll_position",
                    decision_result="filtered",
                    filter_reason=reason,
                    risk_params={
                        "roll_layer": roll_layer,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "max_floating_pnl": max_floating_pnl,
                        "giveback_pct": giveback_pct,
                        "strategy_source": strategy_source,
                    },
                )

            if roll_layer >= max_layers:
                block(f"max roll layers reached {roll_layer}/{max_layers}")
                continue
            if pnl_pct < min_profit_pct:
                block(f"profit {pnl_pct:.1f}% < roll trigger {min_profit_pct:.1f}%")
                continue
            if not int(hist.get("tp1_hit") or 0):
                block("TP1 not locked yet; wait before roll")
                continue
            if max_floating_pnl > 0 and giveback_pct > max_giveback_pct:
                block(f"profit giveback {giveback_pct:.1f}% > {max_giveback_pct:.1f}%")
                continue
            last_roll_age = _age_hours(hist.get("last_roll_time"))
            if last_roll_age is not None and last_roll_age * 60 < cooldown_minutes:
                block(f"roll cooldown {last_roll_age * 60:.0f}m < {cooldown_minutes:.0f}m")
                continue
            allowed, allowed_reason = self._roll_profile_allowed(hist, latest, raw)
            if not allowed:
                block(allowed_reason)
                continue

            ret_15m = float(tech.get("return_15m") or raw.get("ret_15m") or 0)
            ret_1h = float(tech.get("return_1h") or 0)
            if abs(ret_15m) * (100 if abs(ret_15m) < 1 else 1) > max_15m:
                block(f"15m move too hot for roll: {ret_15m:.2f}")
                continue
            if abs(ret_1h) * (100 if abs(ret_1h) < 1 else 1) > max_1h:
                block(f"1h move too hot for roll: {ret_1h:.2f}")
                continue

            entry_profile = {"template": "roll_trend_continuation", "status": "probe" if roll_layer > 0 else "pass"}
            try:
                ob_ok, ob_reason, ob_info = self._check_live_orderbook(sym, side, entry_profile)
            except Exception as e:
                ob_ok, ob_reason, ob_info = False, f"binance depth error: {e}", {}
            if not ob_ok:
                block(ob_reason)
                continue
            spread_pct = float(ob_info.get("spread_pct") or 1)
            if spread_pct > max_spread and not ob_info.get("spread_degraded"):
                block(f"roll spread too wide: {spread_pct:.4%} > {max_spread:.2%}")
                continue

            next_layer = roll_layer + 1
            layer_factor = float(size_factors[roll_layer] if roll_layer < len(size_factors) else size_factors[-1])
            source_factor = alpha_size_factor if strategy_source == "alpha" else 1.0
            spread_factor = spread_degraded_factor if ob_info.get("spread_degraded") else 1.0
            add_qty = self.ex.adjust_quantity(sym, quantity * layer_factor * source_factor * spread_factor)
            if add_qty <= 0:
                block("roll quantity <= 0")
                continue

            hard_stop_pct = float(self.cfg.get("hard_stop_pct", 0.05))
            added_notional = add_qty * mark_price
            added_worst_loss = added_notional * hard_stop_pct
            allowed_giveback = max(0.0, pnl * max_giveback_pct / 100)
            if added_worst_loss > allowed_giveback:
                block(f"roll risk {added_worst_loss:.2f} > allowed profit giveback {allowed_giveback:.2f}")
                continue

            atr = float(hist.get("atr_value") or pos.get("atr_value") or mark_price * 0.02)
            stop_price = mark_price * (1 - hard_stop_pct) if side == "LONG" else mark_price * (1 + hard_stop_pct)
            protected_profit = max(float(hist.get("protected_profit") or 0), pnl * lock_profit_pct)
            action_side = "BUY" if side == "LONG" else "SELL"
            reason = f"roll_layer_{next_layer}: {allowed_reason}; pnl={pnl_pct:.1f}% giveback={giveback_pct:.1f}%"
            actions.append({
                "action": "roll_add",
                "symbol": sym,
                "side": action_side,
                "position_side": side,
                "quantity": add_qty,
                "entry_price": mark_price,
                "stop_loss": stop_price,
                "leverage": int(pos.get("leverage") or self.cfg.get("leverage_max", 3)),
                "atr_value": atr,
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
                "roll_layer": next_layer,
                "protected_profit": protected_profit,
                "max_floating_pnl": max_floating_pnl,
                "risk_before": {
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "quantity": quantity,
                    "max_floating_pnl": max_floating_pnl,
                    "giveback_pct": giveback_pct,
                },
                "risk_after": {
                    "add_qty": add_qty,
                    "added_notional": added_notional,
                    "added_worst_loss": added_worst_loss,
                    "allowed_giveback": allowed_giveback,
                    "source_factor": source_factor,
                    "layer_factor": layer_factor,
                    "spread_factor": spread_factor,
                    "orderbook": ob_info,
                },
            })
            update_position_management(
                sym,
                roll_enabled=1,
                roll_block_reason=None,
                max_floating_pnl=max_floating_pnl,
                protected_profit=protected_profit,
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
        actions = []
        pos_symbols = {p["symbol"] for p in current_positions}
        balance = self.get_balance()
        controls = _runtime_controls()
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

        # === V5: 浣跨敤鍊欓夐夋嫨鍣?===
        if avail > 0 and normal_enabled:
            selector = CandidateSelector()
            candidates = selector.select_candidates(
                top_symbols, current_positions, max_positions=avail
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

                # V5: fixed leverage and minimum position sizing
                pos_info = calculate_position(self.ex, sym, price, balance, score)
                if entry_profile.get("status") == "probe":
                    size_factor = float(entry_profile.get("thresholds", {}).get("probe_position_size_factor") or 0.3)
                else:
                    size_factor = float(entry_profile.get("thresholds", {}).get("position_size_factor") or 1.0)
                if ob_info.get("spread_degraded"):
                    size_factor *= 0.5
                pos_info["quantity"] = round(float(pos_info.get("quantity") or 0) * size_factor, 3)
                pos_info["profile_position_size_factor"] = size_factor
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
                stop_price = (price * 0.95) if side == "LONG" else (price * 1.05)  # 鍥哄畾5%姝㈡崯
                tp_levels = calc_tp_levels(price, side, pos_info["atr_value"])
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
                    "reason": f"{'probe_entry:' if entry_profile.get('status') == 'probe' else ''}璇勫垎{score:.1f} {side}",
                    "grade": s.get("grade", ""),
                    "score": score,
                    "invested": invested,
                    "run_id": run_id,
                    "scan_id": s.get("scan_id"),
                    "entry_mode": entry_profile.get("status"),
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
                    },
                    reason={"reason": f"score {score:.1f} {side}", "regime": regime, "entry_profile": entry_profile.get("template")},
                    market_regime=regime,
                )
                if len([a for a in actions if a.get("action") == "open"]) >= adj_max_pos:
                    break
        elif avail > 0:
            logger.info("  normal trading runtime switch is OFF; skip normal open candidates")

        open_count = len([a for a in actions if a.get("action") == "open"])
        alpha_avail = max(0, adj_max_pos - open_count)
        if alpha_avail > 0 and alpha_enabled:
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
                    self._execute_open(act, results)
                elif act["action"] == "roll_add":
                    self._execute_roll_add(act, results)
                elif act["action"] == "close":
                    self._execute_close(act, results)
                elif act["action"] == "partial_close":
                    self._execute_partial_close(act, results)
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
            )
            act["position_id"] = position_id
        except Exception as e:
            logger.warning(f"    position history write failed: {e}")
        
        # 杩借釜鐘舵佸垵濮嬪寲
        self._pos_tracker[act["symbol"]] = {
            "highest_price": act["entry_price"],
            "entry_price": act["entry_price"],
        }
        results.append({"status": "ok", **act})

    def _execute_roll_add(self, act, results):
        logger.info(
            f"  滚仓 {act['position_side']} {act['symbol']} layer={act.get('roll_layer')} "
            f"x{act['quantity']} @${act['entry_price']:.4f}"
        )
        self.ex.set_leverage(act["symbol"], act.get("leverage", 3))
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
            logger.warning(f"    local roll order write failed: {e}")

        stop_side = "SELL" if act["position_side"] == "LONG" else "BUY"
        try:
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
                    reason="roll_stop_loss",
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
                logger.warning(f"    local roll stop order write failed: {e}")
            logger.info(f"    滚仓止损挂单 @${act['stop_loss']:.4f}: {stop_order.get('orderId')}")
        except Exception as e:
            logger.warning(f"    roll stop order failed: {e}")

        try:
            from shared.db import record_position_roll_event, update_position_management
            positions = self.ex.get_positions()
            pos = next((p for p in positions if p["symbol"] == act["symbol"]), None)
            update_fields = {
                "roll_layer": act.get("roll_layer"),
                "last_roll_time": datetime.now(timezone.utc).isoformat(),
                "protected_profit": act.get("protected_profit"),
                "max_floating_pnl": act.get("max_floating_pnl"),
                "roll_enabled": 1,
                "roll_block_reason": None,
                "last_exit_reason": act.get("reason"),
            }
            if pos:
                update_fields["quantity"] = pos.get("quantity")
                update_fields["entry_price"] = pos.get("entry_price")
            update_position_management(act["symbol"], **update_fields)
            record_position_roll_event(
                symbol=act["symbol"],
                position_side=act.get("position_side"),
                strategy_source=act.get("strategy_source", "normal"),
                roll_layer=act.get("roll_layer"),
                roll_qty=act.get("quantity"),
                roll_price=act.get("entry_price"),
                roll_reason=act.get("reason"),
                position_id=act.get("position_id"),
                risk_before=act.get("risk_before"),
                risk_after=act.get("risk_after"),
            )
        except Exception as e:
            logger.warning(f"    roll state write failed: {e}")

        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            side=act.get("position_side"),
            decision_stage="execution",
            decision_result="rolled_add",
            quantity=act.get("quantity"),
            entry_price=act.get("entry_price"),
            risk_params=act.get("risk_after"),
            reason={"reason": act.get("reason"), "order_id": order.get("orderId")},
        )
        logger.info(f"    滚仓成交: {order.get('orderId')}")
        results.append({"status": "ok", **act})

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
        if (hist.get("strategy_source") or act.get("strategy_source")) == "alpha" and pnl < 0:
            try:
                from shared.db import set_alpha_cooldown
                set_alpha_cooldown(act["symbol"], "loss", f"alpha loss pnl={pnl:.2f}", 120, loss_count=1)
            except Exception as e:
                logger.warning(f"    alpha cooldown write failed: {e}")
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
            return

        pct = act.get("close_pct", 0.50)
        close_qty = round(pos["quantity"] * pct, 3)
        if close_qty <= 0:
            return

        # 浼扮畻PNL锛堟敞鎰忥細杩欐槸浼扮畻鍊硷紝闈炲竵瀹夌湡瀹炲硷級
        margin = pos["entry_price"] * pos["quantity"] / max(pos.get("leverage", 1), 1)
        pnl = pos["unrealized_pnl"] * pct
        pnl_pct_v = round(pnl / (margin * pct) * 100, 2) if margin else 0
        from shared.db import get_position_history, record_trade
        hist = get_position_history(act["symbol"]) or {}
        record_trade(
            symbol=act["symbol"], side=pos["side"],
            qty=close_qty, entry_price=pos["entry_price"],
            exit_price=pos["mark_price"],
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
        self._record_decision(
            act["symbol"],
            run_id=act.get("run_id"),
            side=pos.get("side"),
            decision_stage="execution",
            decision_result="partial_closed",
            quantity=close_qty,
            entry_price=pos.get("entry_price"),
            price=pos.get("mark_price"),
            reason={"exit_reason": act.get("reason", ""), "pnl": pnl, "pnl_pct": pnl_pct_v},
        )

        logger.info(f"  鍑忎粨 {act['symbol']}: {pct*100:.0f}% (x{close_qty}) {act['reason']}")
        self.ex.close_position_market(act["symbol"], act["side"], close_qty)
        logger.info(f"    鍑忎粨鎴愪氦: x{close_qty}")
        results.append({"status": "ok", **act})
