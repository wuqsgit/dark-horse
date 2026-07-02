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

    def _build_position_actions(self, top_symbols: list, current_positions: list) -> list:
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

    def decide(self, top_symbols: list, current_positions: list, run_id: str | None = None) -> list:
        """Build open, close and partial-close actions."""
        actions = []
        pos_symbols = {p["symbol"] for p in current_positions}
        balance = self.get_balance()

        self._sync_tracker(current_positions)
        actions.extend(self._build_position_actions(top_symbols, current_positions))

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
        if avail > 0:
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

        return actions

    def execute(self, actions: list) -> list:
        """Execute planned actions."""
        results = []
        for act in actions:
            try:
                if act["action"] == "open":
                    self._execute_open(act, results)
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
        order = self.ex.place_market_order(act["symbol"], act["side"], act["quantity"])
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
