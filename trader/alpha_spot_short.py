"""Direct spot-distribution short signals, without candle-shape confirmation."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


MARKER = "alpha_spot_distribution_24h"


def evaluate_spot_distribution(ticker, flow, *, now):
    from trader.lifecycle_policy import _time
    try:
        change = float(ticker["percent_change_24h"])
        if not math.isfinite(change) or change > -30:
            return False, "spot_24h_drop_below_30", {}
        age = (now - _time(ticker["last_seen"])).total_seconds()
        if not 0 <= age <= 900:
            return False, "spot_ticker_stale", {}
        if not flow:
            return False, "real_spot_flow_missing", {}
        buy = total = 0.0
        for row in flow:
            age = (now - _time(row["time"])).total_seconds()
            source = str(row.get("source") or "")
            if not 0 <= age <= 180 or source not in {"rest", "backfill", "websocket"}:
                return False, "real_spot_flow_missing", {}
            q, b = float(row["quote_vol"]), float(row["taker_buy_quote_vol"])
            if not all(math.isfinite(v) for v in (q, b)) or q < 0 or not 0 <= b <= q:
                return False, "invalid_spot_flow", {}
            total += q
            buy += b
        details = {"spot_return_24h_pct": change, "buy_quote": buy, "sell_quote": total-buy,
                   "source": "reported_spot_taker_volume"}
        if total <= 0 or total-buy <= buy:
            return False, "spot_sellers_not_dominant", details
        return True, MARKER, details
    except (KeyError, ValueError, TypeError):
        return False, "spot_distribution_data_missing", {}


def load_spot_distribution(alpha_symbol, *, now):
    from shared.db import get_conn
    with get_conn() as conn:
        ticker = conn.execute("SELECT * FROM alpha_symbols WHERE alpha_symbol=?", (alpha_symbol,)).fetchone()
        flow = conn.execute(
            "SELECT time,quote_vol,taker_buy_quote_vol,source FROM alpha_candles_1m "
            "WHERE alpha_symbol=? AND source_env='mainnet' AND is_closed=1 ORDER BY time DESC LIMIT 2",
            (alpha_symbol,),
        ).fetchall()
    return evaluate_spot_distribution(dict(ticker) if ticker else {}, [dict(r) for r in flow], now=now)


def publish_spot_short_events(mode, *, now=None):
    """One event per continuous qualifying episode; account claims stay separate."""
    from shared.db import get_conn
    now = now or datetime.now(timezone.utc)
    with get_conn() as conn:
        symbols = [dict(r) for r in conn.execute(
            "SELECT a.alpha_symbol,a.futures_symbol,a.percent_change_24h FROM alpha_symbols a "
            "JOIN market_universe u ON u.pool_type='alpha' AND u.source_symbol=a.alpha_symbol "
            "WHERE (u.selected=1 OR u.forced_position=1) AND a.futures_symbol IS NOT NULL"
        )]
    for item in symbols:
        alpha = item["alpha_symbol"]
        allowed, reason, details = load_spot_distribution(alpha, now=now)
        key = f"{MARKER}:{mode}:{alpha}"
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT value FROM trading_runtime_controls WHERE key=?", (key,)).fetchone()
            if not allowed:
                # Missing data is not proof that the bearish episode ended.
                if reason in {"spot_24h_drop_below_30", "spot_sellers_not_dominant"}:
                    conn.execute("DELETE FROM trading_runtime_controls WHERE key=?", (key,))
                continue
            if prior:
                continue
            anchor = now.isoformat()
            event_id = "spot-short-" + hashlib.sha256(f"{key}:{anchor}".encode()).hexdigest()[:24]
            conn.execute("INSERT INTO trading_runtime_controls(key,value) VALUES(?,?)", (key, event_id))
            conn.execute(
                "INSERT INTO alpha_signal_events(event_id,market_env,strategy_mode,futures_symbol,alpha_symbol,"
                "setup_id,to_state,state_version,action_type,event_time,candle_close_time,max_position_factor,"
                "expires_at,reason_codes_json,ai_decision_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id,"mainnet",mode,item["futures_symbol"],alpha,event_id,"CONFIRMED",1,"CONFIRM_SHORT",
                 anchor,anchor,1.0,(now+timedelta(minutes=5)).isoformat(),json.dumps([MARKER]),json.dumps(details),anchor),
            )
