"""AlphaDog API Server 鈥?FastAPI (SQLite)"""
import os, sys, json, time
from fastapi import FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    fetch_alpha_score_history,
    fetch_alpha_symbol_detail,
    fetch_latest_alpha_scan,
    fetch_latest_alpha_trade_candidates,
    fetch_active_alpha_cooldowns,
    fetch_latest_scan,
    fetch_symbol_detail,
    fetch_score_history,
    fetch_backtest_summary,
    fetch_recent_signals,
    fetch_factor_performance,
    fetch_signal_outcome_summary,
    get_trading_runtime_controls,
    set_trading_runtime_control,
    init_db,
)

def plain_reason(reason):
    text = str(reason or "")
    if not text:
        return "暂时没有明确阻断原因"
    mapping = [
        ("score ", "综合分还没过开仓线"),
        ("entry_alpha", "开仓信号强度不够"),
        ("negative expectancy", "最近同类信号回报不好"),
        ("drawdown probability", "回撤风险偏高"),
        ("stale signal", "信号太旧，系统不追旧机会"),
        ("orderbook robot signature", "盘口像机器刷单，先不碰"),
        ("depth ratio score too weak", "盘口承接弱"),
        ("big order score too weak", "大单支持不足"),
        ("no_confident_side", "多空方向不够一致"),
        ("live depth against LONG", "实时盘口不支持做多"),
        ("live depth against SHORT", "实时盘口不支持做空"),
        ("spread too wide", "买卖价差偏大"),
        ("not_tradable_on_exchange", "交易所当前不可交易"),
        ("already_in_position", "已经有这个币的持仓"),
        ("quantity <= 0", "按风控算出来仓位太小"),
        ("hard_stop", "触发硬止损"),
        ("hold_alpha_collapse", "持仓质量明显变差"),
        ("hold_alpha_weak", "持仓质量偏弱且没有明显盈利"),
        ("score_decay_full", "评分大幅衰减，系统会全平"),
        ("score_decay_half", "评分明显衰减，系统会减半"),
        ("score_decay_qtr", "评分开始衰减，系统会小幅减仓"),
        ("history_expectancy_turns_bad", "历史期望转差"),
        ("orderbook_depth_weak", "盘口深度变弱"),
        ("time_stop", "持仓太久但收益不大"),
        ("momentum_reversal", "短线动量反向"),
        ("TP1", "达到第一档止盈"),
        ("TP2", "达到第二档止盈"),
        ("trailing_stop", "触发移动止盈"),
    ]
    for needle, label in mapping:
        if needle in text:
            return label
    return text


def explain_scan_row(row):
    try:
        from trader.risk import meets_safety_filters, determine_side

        row_dict = dict(row)
        ok, reason = meets_safety_filters(row_dict)
        side = determine_side(row_dict)
        score = float(row_dict.get("composite_score") or 0)
        entry_alpha = float(row_dict.get("entry_alpha") or 0)
        hold_alpha = float(row_dict.get("hold_alpha") or 0)
        if ok and side:
            side_text = "多" if side == "LONG" else "空"
            headline = f"可观察开仓，方向偏{side_text}"
            detail = "分数、信号强度和方向基本一致，实盘下单前还会再查一次 Binance 盘口。"
        elif not ok:
            headline = "暂不开仓"
            detail = plain_reason(reason)
        else:
            headline = "暂不开仓"
            detail = "方向还不够统一，系统不会硬猜多空。"
        return {
            "headline": headline,
            "detail": detail,
            "raw_reason": reason,
            "side": side,
            "score_text": f"评分 {score:.1f}",
            "entry_text": f"开仓信号 {entry_alpha:.1f}",
            "hold_text": f"持仓质量 {hold_alpha:.1f}",
        }
    except Exception as e:
        return {"headline": "暂无解读", "detail": str(e), "raw_reason": str(e), "side": None}


def apply_entry_profile_plain_signal(plain, entry_profile):
    if not plain or not entry_profile:
        return plain
    status = entry_profile.get("status")
    if status == "pass":
        side_text = "多" if plain.get("side") == "LONG" else "空"
        plain["headline"] = f"模板通过，方向偏{side_text}"
        plain["detail"] = entry_profile.get("reason") or plain.get("detail")
    elif status == "probe":
        plain["headline"] = "试探仓候选"
        plain["detail"] = entry_profile.get("reason") or "基础条件成立，但确认不足，只允许小仓试探。"
    elif status == "observe":
        plain["headline"] = "观察，不开仓"
        plain["detail"] = entry_profile.get("reason") or "基础条件成立，但还缺少模板确认。"
    elif status == "block":
        plain["headline"] = "暂不开仓"
        plain["detail"] = entry_profile.get("reason") or plain.get("detail")
    return plain


def compute_market_section(row):
    strength = float(row.get("relative_strength") or 50)
    score = float(row.get("composite_score") or 0)
    entry_alpha = float(row.get("entry_alpha") or 0)
    risk = str(row["risk_label"] or "")
    phase = str(row["chip_phase"] or "")
    pos = str(row["price_position"] or "")

    if strength >= 75 and score >= 45 and entry_alpha >= 55 and "风险" not in risk and "出货" not in phase and "高位" not in pos:
        return "进攻"
    if strength >= 45 and "出货" not in phase:
        return "观察"
    if strength >= 25 and ("出货" in risk or "高位" in pos):
        return "谨慎"
    return "风险"

app = FastAPI(title="AlphaDog API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_api_cache = {}
_response_cache = {}
_scan_payload_cache = {"scan_id": None, "payload": None}
_SCAN_CACHE_TTL = 5
_BACKTEST_CACHE_TTL = 300
_TRADING_CACHE_TTL = 10
_FAST_CACHE_PATHS = {
    "/api/scan/latest",
    "/api/alpha/scan/latest",
    "/api/scan/details",
    "/api/backtest/summary",
    "/api/backtest/recent",
    "/api/backtest/signals",
    "/api/backtest/factor_weights",
    "/api/strategy/learning",
    "/api/trading/status",
    "/api/trading/statu",
}


def compute_v3_signals(symbol, row, tech):
    try:
        from engine.breakout_detector import check_breakout_confirmation, compute_rr_detail, compute_breakout_metrics
        from trader.cooldown_manager import is_in_cooldown

        price = float(row["market_price"] or 0)
        atr = tech.get("atr", 0)

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
            "tp_levels": {
                "tp1": round(price + 2*atr, 4) if price > 0 else 0,
                "tp2": round(price + 4*atr, 4) if price > 0 else 0,
                "tp3": round(price + 6*atr, 4) if price > 0 else 0,
                "stop": round(price - 2*atr, 4) if price > 0 else 0,
            } if atr > 0 else None,
        }
    except Exception as e:
        return {"error": str(e)}


def format_backtest_signals(rows):
    return [
        {
            "symbol": r["symbol"],
            "time": r["grade_time"],
            "grade": r["grade"],
            "score": float(r["grade_score"] or 0),
            "price": float(r["price_at_grade"] or 0),
            "return_12h": float(r["return_12h"] or 0) if r["return_12h"] is not None else None,
            "return_24h": float(r["return_24h"] or 0) if r["return_24h"] is not None else None,
            "win_12h": bool(r["win_12h"]) if r["win_12h"] is not None else None,
            "win_24h": bool(r["win_24h"]) if r["win_24h"] is not None else None,
        }
        for r in rows
    ]


def cache_get(key, ttl):
    item = _api_cache.get(key)
    if item and time.time() - item["time"] < ttl:
        return item["data"]
    return None


def cache_set(key, data):
    _api_cache[key] = {"time": time.time(), "data": data}
    return data


def seed_response_cache(path, data):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    for host in ("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:3000", "http://localhost:3000"):
        _response_cache[f"{host}{path}"] = {
            "time": time.time(),
            "body": body,
            "status_code": 200,
            "media_type": "application/json",
        }


@app.middleware("http")
async def fast_path_cache(request, call_next):
    if request.method != "GET" or request.url.path not in _FAST_CACHE_PATHS:
        return await call_next(request)
    key = str(request.url)
    item = _response_cache.get(key)
    if request.url.path.startswith("/api/trading/"):
        ttl = _TRADING_CACHE_TTL
    elif request.url.path.startswith("/api/scan/"):
        ttl = _SCAN_CACHE_TTL
    elif request.url.path in {"/api/backtest/review", "/api/backtest/factor_analysis"}:
        ttl = 5
    else:
        ttl = _BACKTEST_CACHE_TTL
    if item and time.time() - item["time"] < ttl:
        return Response(
            content=item["body"],
            status_code=item["status_code"],
            media_type=item["media_type"],
            headers={"X-Cache": "HIT"},
        )
    response = await call_next(request)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    if response.status_code == 200:
        payload = {
            "time": time.time(),
            "body": body,
            "status_code": response.status_code,
            "media_type": response.media_type or response.headers.get("content-type", "application/json"),
        }
        _response_cache[key] = payload
        if request.url.path == "/api/trading/status":
            _response_cache[key.replace("/api/trading/status", "/api/trading/statu")] = payload
        elif request.url.path == "/api/trading/statu":
            _response_cache[key.replace("/api/trading/statu", "/api/trading/status")] = payload
    return Response(
        content=body,
        status_code=response.status_code,
        media_type=response.media_type,
        headers=dict(response.headers),
    )


@app.on_event("startup")
async def startup():
    init_db()
    try:
        seed_response_cache("/api/backtest/summary", await get_backtest_summary(user="admin"))
        rows = fetch_recent_signals("S1", 50)
        seed_response_cache("/api/backtest/recent?grade=S1&limit=50", [
            {
                "symbol": r["symbol"], "time": r["grade_time"], "grade": r["grade"],
                "score": float(r["grade_score"] or 0), "price": float(r["price_at_grade"] or 0),
                "return_12h": float(r["return_12h"] or 0) if r["return_12h"] is not None else None,
                "return_24h": float(r["return_24h"] or 0) if r["return_24h"] is not None else None,
                "win_12h": bool(r["win_12h"]) if r["win_12h"] is not None else None,
                "win_24h": bool(r["win_24h"]) if r["win_24h"] is not None else None,
            }
            for r in rows
        ])
        weights_path = os.path.join(os.path.dirname(__file__), "..", "engine", "factor_weights.json")
        with open(weights_path, encoding="utf-8") as f:
            seed_response_cache("/api/backtest/factor_weights", json.load(f))
    except Exception:
        pass


async def get_user():
    return "admin"


@app.get("/api/scan/latest")
async def get_latest_scan(user=Depends(get_user)):
    scan, rows = fetch_latest_scan()
    if not scan:
        return {"scan_time": None, "symbols": []}
    if _scan_payload_cache["scan_id"] == scan["scan_id"] and _scan_payload_cache["payload"] is not None:
        return _scan_payload_cache["payload"]
    symbols = []
    for r in rows:
        plain = explain_scan_row(r)
        features = {}
        if r["raw_features"]:
            try:
                features = json.loads(r["raw_features"])
            except Exception:
                features = {}
        tech = features.get("technical", {})
        v3_signals = compute_v3_signals(r["symbol"], r, tech)
        try:
            from trader.entry_profiles import evaluate_profile_entry
            entry_profile = evaluate_profile_entry(r, v3_signals, plain.get("side"))
        except Exception as e:
            entry_profile = {"status": "error", "reason": str(e), "template": "unknown", "template_name": "鏈煡"}
        plain = apply_entry_profile_plain_signal(plain, entry_profile)
        symbols.append({
            "symbol": r["symbol"],
            "price": float(r["market_price"] or 0),
            "composite_score": float(r["composite_score"] or 0),
            "grade": r["composite_summary"],
            "risk_label": r["risk_label"],
            "chip_phase": r["chip_phase"],
            "trend_state": r["trend_state"],
            "trend_direction": r["trend_direction"],
            "volatility_level": r["volatility_level"],
            "price_position": r["price_position"],
            "relative_strength": float(r["relative_strength"] or 50),
            "entry_alpha": float(r["entry_alpha"] or 0),
            "hold_alpha": float(r["hold_alpha"] or 0),
            "plain_signal": plain,
            "v3_signals": v3_signals,
            "entry_profile": entry_profile,
            "score_layers": features.get("score_layers", {}),
            "market_section": compute_market_section({
                "relative_strength": r["relative_strength"],
                "composite_score": r["composite_score"],
                "entry_alpha": r["entry_alpha"],
                "risk_label": r["risk_label"],
                "chip_phase": r["chip_phase"],
                "price_position": r["price_position"],
                "volatility_level": r["volatility_level"],
            }),
        })
    symbols.sort(key=lambda x: -x["composite_score"])
    payload = {"scan_time": scan["time"], "count": len(symbols), "symbols": symbols}
    _scan_payload_cache["scan_id"] = scan["scan_id"]
    _scan_payload_cache["payload"] = payload
    return payload


@app.get("/api/scan/details")
async def get_scan_details(user=Depends(get_user)):
    return await get_latest_scan(user)


def _parse_json(value, default=None):
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


@app.get("/api/alpha/scan/latest")
async def get_latest_alpha_scan(user=Depends(get_user)):
    scan, rows = fetch_latest_alpha_scan()
    if not scan:
        return {"scan_time": None, "count": 0, "symbols": []}
    candidate_rows = fetch_latest_alpha_trade_candidates(500)
    candidate_by_alpha = {}
    for c in candidate_rows:
        if c.get("alpha_symbol") not in candidate_by_alpha:
            candidate_by_alpha[c.get("alpha_symbol")] = c
    cooldowns = fetch_active_alpha_cooldowns(100)
    symbols = []
    for r in rows:
        raw = _parse_json(r["raw_features"])
        candidate = candidate_by_alpha.get(r["alpha_symbol"]) or {}
        volume_price = raw.get("volume_price") or {}
        symbols.append({
            "alpha_symbol": r["alpha_symbol"],
            "base_asset": r["base_asset"],
            "name": r["alpha_name"],
            "futures_symbol": r["futures_symbol"],
            "tradeability": r["tradeability"],
            "status": r["status"],
            "price": float(r["market_price"] or 0),
            "alpha_score": float(r["alpha_score"] or 0),
            "discovery_score": float(r["discovery_score"] or 0),
            "momentum_score": float(r["momentum_score"] or 0),
            "liquidity_score": float(r["liquidity_score"] or 0),
            "risk_score": float(r["risk_score"] or 0),
            "tradeability_score": float(r["tradeability_score"] or 0),
            "grade": r["grade"],
            "decision": r["decision"],
            "alpha_profile": r["alpha_profile"],
            "entry_level": r["entry_level"],
            "suggested_position_pct": float(r["suggested_position_pct"] or 0),
            "block_reasons": _parse_json(r["block_reasons"], []),
            "profile_thresholds": _parse_json(r["profile_thresholds"], {}),
            "volume_24h": float(r["volume_24h"] or 0),
            "liquidity": float(r["liquidity"] or 0),
            "percent_change_24h": float(r["percent_change_24h"] or 0),
            "spread_pct": (raw.get("depth") or {}).get("spread_pct"),
            "volume_growth_6h": (raw.get("volume") or {}).get("volume_growth_6h"),
            "volume_price": volume_price,
            "normal_review": {
                "normal_score": candidate.get("normal_score"),
                "normal_grade": candidate.get("normal_grade"),
                "normal_side": candidate.get("normal_side"),
                "entry_profile": _parse_json(candidate.get("entry_profile"), {}),
                "entry_status": candidate.get("entry_status"),
                "block_reason": candidate.get("block_reason"),
                "adapter_quality": candidate.get("adapter_quality"),
                "missing_fields": _parse_json(candidate.get("missing_fields_json"), []),
                "volume_price": {
                    "state": candidate.get("volume_price_state"),
                    "action": candidate.get("volume_price_action"),
                    "reasons": _parse_json(candidate.get("volume_price_reasons_json"), []),
                    "metrics": _parse_json(candidate.get("volume_price_metrics_json"), {}),
                    "max_position_factor": candidate.get("volume_price_max_position_factor"),
                },
                "updated_at": candidate.get("updated_at"),
            } if candidate else None,
        })
    return {"scan_time": scan["time"], "count": len(symbols), "symbols": symbols, "cooldowns": cooldowns}


@app.get("/api/alpha/scan/by_symbol/{alpha_symbol}")
async def get_alpha_symbol_detail(alpha_symbol: str, user=Depends(get_user)):
    row = fetch_alpha_symbol_detail(alpha_symbol.upper())
    if not row:
        return {"error": "Not found", "symbol": alpha_symbol}
    raw = _parse_json(row["raw_features"])
    symbol_raw = _parse_json(row["symbol_raw_json"])
    candidate = next(
        (c for c in fetch_latest_alpha_trade_candidates(500) if c.get("alpha_symbol") == row["alpha_symbol"]),
        None,
    )
    history = [
        {
            "time": r["time"],
            "score": float(r["alpha_score"] or 0),
            "grade": r["grade"],
            "price": float(r["market_price"] or 0),
        }
        for r in fetch_alpha_score_history(row["alpha_symbol"], 100)
    ]
    return {
        "alpha_symbol": row["alpha_symbol"],
        "base_asset": row["base_asset"],
        "name": row["alpha_name"],
        "token_id": row["token_id"],
        "futures_symbol": row["futures_symbol"],
        "tradeability": row["tradeability"],
        "status": row["status"],
        "time": row["time"],
        "price": float(row["market_price"] or 0),
        "alpha_score": float(row["alpha_score"] or 0),
        "grade": row["grade"],
        "decision": row["decision"],
        "profile": {
            "name": row["alpha_profile"],
            "entry_level": row["entry_level"],
            "suggested_position_pct": float(row["suggested_position_pct"] or 0),
            "block_reasons": _parse_json(row["block_reasons"], []),
            "thresholds": _parse_json(row["profile_thresholds"], {}),
        },
        "scores": {
            "discovery": float(row["discovery_score"] or 0),
            "momentum": float(row["momentum_score"] or 0),
            "liquidity": float(row["liquidity_score"] or 0),
            "risk": float(row["risk_score"] or 0),
            "tradeability": float(row["tradeability_score"] or 0),
        },
        "volume_24h": float(row["volume_24h"] or 0),
        "liquidity": float(row["liquidity"] or 0),
        "percent_change_24h": float(row["percent_change_24h"] or 0),
        "raw_features": raw,
        "symbol_raw": symbol_raw,
        "history": history,
        "volume_price": raw.get("volume_price") or {},
        "normal_review": {
            "normal_score": candidate.get("normal_score"),
            "normal_grade": candidate.get("normal_grade"),
            "normal_side": candidate.get("normal_side"),
            "entry_profile": _parse_json(candidate.get("entry_profile"), {}),
            "entry_status": candidate.get("entry_status"),
            "block_reason": candidate.get("block_reason"),
            "adapter_quality": candidate.get("adapter_quality"),
            "missing_fields": _parse_json(candidate.get("missing_fields_json"), []),
            "volume_price": {
                "state": candidate.get("volume_price_state"),
                "action": candidate.get("volume_price_action"),
                "reasons": _parse_json(candidate.get("volume_price_reasons_json"), []),
                "metrics": _parse_json(candidate.get("volume_price_metrics_json"), {}),
                "max_position_factor": candidate.get("volume_price_max_position_factor"),
            },
            "updated_at": candidate.get("updated_at"),
        } if candidate else None,
    }


def _build_factor_analysis_from_performance(conn):
    run_row = conn.execute(
        "SELECT DISTINCT run_time FROM factor_performance ORDER BY run_time DESC LIMIT 1"
    ).fetchone()
    if not run_row:
        return {"error": "no analysis data", "recommendations": [], "candidate_recommendations": []}
    run_time = run_row["run_time"]
    rows = conn.execute(
        """SELECT factor_name, bucket, samples, win_rate, avg_return,
                  avg_drawdown, ev, ic, ir
           FROM factor_performance
           WHERE run_time = ?
           ORDER BY factor_name, bucket""",
        (run_time,),
    ).fetchall()
    by_factor = {}
    for row in rows:
        by_factor.setdefault(row["factor_name"], []).append(dict(row))

    current_factors = []
    recommendations = []
    category_stats = {}
    for name, buckets in by_factor.items():
        usable = [b for b in buckets if int(b.get("samples") or 0) >= 3 and b.get("win_rate") is not None]
        category_stats[name] = {
            "buckets": buckets,
            "samples": sum(int(b.get("samples") or 0) for b in buckets),
        }
        if not usable:
            continue
        high = max(usable, key=lambda b: (float(b.get("win_rate") or 0), float(b.get("avg_return") or 0)))
        low = min(usable, key=lambda b: (float(b.get("win_rate") or 0), float(b.get("avg_return") or 0)))
        discrimination = (float(high["win_rate"] or 0) - float(low["win_rate"] or 0)) * 100
        factor_item = {
            "name": name,
            "discrimination": round(discrimination, 1),
            "high_bucket": high["bucket"],
            "low_bucket": low["bucket"],
            "high_win_rate": round(float(high["win_rate"] or 0) * 100, 1),
            "low_win_rate": round(float(low["win_rate"] or 0) * 100, 1),
            "high_avg_return": round(float(high.get("avg_return") or 0) * 100, 2),
            "low_avg_return": round(float(low.get("avg_return") or 0) * 100, 2),
            "samples": sum(int(b.get("samples") or 0) for b in usable),
        }
        current_factors.append(factor_item)
        if abs(discrimination) >= 3:
            recommendations.append({
                "factor": name,
                "description": f"{name} 的 {high['bucket']} 桶胜率 {factor_item['high_win_rate']}%，明显优于 {low['bucket']} 桶 {factor_item['low_win_rate']}%",
                "correlation": high.get("ev"),
                "discrimination": round(discrimination, 1),
                "suggestion": f"优先提高 {name}={high['bucket']} 的权重，降低 {low['bucket']} 的开仓优先级。",
            })

    current_factors.sort(key=lambda x: abs(x["discrimination"]), reverse=True)
    recommendations.sort(key=lambda x: abs(x["discrimination"]), reverse=True)
    high_scores = [x["high_win_rate"] for x in current_factors]
    low_scores = [x["low_win_rate"] for x in current_factors]
    overall = (sum(high_scores) / len(high_scores) - sum(low_scores) / len(low_scores)) if high_scores and low_scores else 0
    return {
        "run_time": run_time,
        "source": "factor_performance_fallback",
        "total_signals": sum(int(r["samples"] or 0) for r in rows),
        "current_factors": current_factors,
        "recommendations": recommendations[:10],
        "candidate_recommendations": recommendations[:10],
        "category_stats": category_stats,
        "overall_discrimination": round(overall, 1),
    }


def _enrich_backtest_review(conn, review):
    review = review or {}
    rows = conn.execute(
        """SELECT symbol, grade, grade_score, grade_time, max_drawdown,
                  return_6h, return_12h, return_24h, return_48h, win_12h, win_24h
           FROM backtest_results
           WHERE datetime(substr(replace(grade_time, 'T', ' '), 1, 19)) >= datetime('now', '-1 day')
           ORDER BY datetime(substr(replace(grade_time, 'T', ' '), 1, 19)) DESC
           LIMIT 5000"""
    ).fetchall()
    trades = conn.execute(
        """SELECT symbol, side, pnl_pct, pnl, exit_reason, entry_time, exit_time,
                  grade_at_entry, score_at_entry
           FROM trades
           WHERE datetime(substr(replace(exit_time, 'T', ' '), 1, 19)) >= datetime('now', '-1 day')
           ORDER BY datetime(substr(replace(exit_time, 'T', ' '), 1, 19)) DESC
           LIMIT 200"""
    ).fetchall()
    entry_issues = []
    exit_issues = []
    good_exits = []
    for r in rows:
        returns = [float(v) for v in (r["return_6h"], r["return_12h"], r["return_24h"], r["return_48h"]) if v is not None]
        if not returns:
            continue
        max_gain = max(returns)
        final_ret = next((float(v) for v in (r["return_24h"], r["return_12h"], r["return_6h"]) if v is not None), returns[-1])
        max_dd = float(r["max_drawdown"] or 0)
        item = {
            "symbol": r["symbol"],
            "grade": r["grade"],
            "score": round(float(r["grade_score"] or 0), 1),
            "grade_time": r["grade_time"],
            "max_gain_pct": round(max_gain * 100, 2),
            "max_dd_pct": round(max_dd * 100, 2),
            "ret_6h_pct": round(float(r["return_6h"] or 0) * 100, 2),
            "ret_24h_pct": round(final_ret * 100, 2),
        }
        if max_gain < 0.015 or final_ret < -0.02 or max_dd <= -0.035:
            severity = abs(min(final_ret, 0)) * 100 + abs(min(max_dd, 0)) * 50 + max(0, 0.015 - max_gain) * 100
            entry_issues.append({**item, "entry_quality": "需要改进", "exit_quality": "观察", "reason": "最近 24h 入场后空间不足或回撤偏大", "_severity": severity, "_type": "entry"})
        elif max_gain >= 0.035 and final_ret >= 0.015:
            severity = max_gain * 100 + final_ret * 50
            exit_issues.append({**item, "entry_quality": "基本正确", "exit_quality": "可能偏早", "reason": "最近 24h 信号后仍有上行空间", "_severity": severity, "_type": "exit"})
        if max_dd <= -0.04 or final_ret <= -0.025:
            good_exits.append({**item, "entry_quality": "风险偏高", "exit_quality": "保护有效", "reason": "最近 24h 后续回撤或转负明显，保护退出有价值", "_severity": abs(min(max_dd, final_ret)) * 100})

    live_good = []
    for t in trades:
        pnl_pct = float(t["pnl_pct"] or 0)
        reason = str(t["exit_reason"] or "")
        if pnl_pct > 0 or any(x in reason for x in ("TP1", "TP2", "trailing_stop")):
            live_good.append({
                "symbol": t["symbol"],
                "grade": t["grade_at_entry"] or "-",
                "score": round(float(t["score_at_entry"] or 0), 1),
                "grade_time": t["exit_time"],
                "max_gain_pct": round(max(pnl_pct, 0), 2),
                "max_dd_pct": 0,
                "ret_6h_pct": round(pnl_pct, 2),
                "ret_24h_pct": round(pnl_pct, 2),
                "entry_quality": "实盘验证",
                "exit_quality": "有效做法",
                "reason": reason or "最近 24h 盈利退出",
                "_severity": abs(pnl_pct),
            })

    issue_pool = entry_issues + exit_issues
    issue_pool.sort(key=lambda x: x.get("_severity", 0), reverse=True)
    unique_issues = []
    seen_symbols = set()
    for item in issue_pool:
        symbol_key = item.get("symbol")
        if symbol_key in seen_symbols:
            continue
        seen_symbols.add(symbol_key)
        unique_issues.append(item)
        if len(unique_issues) >= 10:
            break
    if len(unique_issues) < 10:
        seen_ids = {(x.get("symbol"), x.get("grade_time"), x.get("_type")) for x in unique_issues}
        for item in issue_pool:
            item_id = (item.get("symbol"), item.get("grade_time"), item.get("_type"))
            if item_id in seen_ids:
                continue
            unique_issues.append(item)
            seen_ids.add(item_id)
            if len(unique_issues) >= 10:
                break
    issue_pool = unique_issues
    review["entry_issues"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in issue_pool if x["_type"] == "entry"]
    review["exit_issues"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in issue_pool if x["_type"] == "exit"]
    good_pool = live_good + good_exits
    good_pool.sort(key=lambda x: x.get("_severity", 0), reverse=True)
    unique_good = []
    seen_good_symbols = set()
    for item in good_pool:
        symbol_key = item.get("symbol")
        if symbol_key in seen_good_symbols:
            continue
        seen_good_symbols.add(symbol_key)
        unique_good.append(item)
        if len(unique_good) >= 5:
            break
    review["good_exits"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in unique_good]
    overview = (review.setdefault("summary", {}).setdefault("overview", {}))
    overview["total_samples"] = len(rows)
    overview["gave_space_5pct"] = sum(
        1 for r in rows
        if max([float(v) for v in (r["return_6h"], r["return_12h"], r["return_24h"], r["return_48h"]) if v is not None] or [0]) >= 0.05
    )
    overview["had_drawdown_8pct"] = sum(1 for r in rows if abs(float(r["max_drawdown"] or 0)) >= 0.08)
    overview["trade_count"] = len(trades)
    overview["review_window"] = "最近 1 天"
    overview["grade_time_min"] = min((r["grade_time"] for r in rows), default=None)
    overview["grade_time_max"] = max((r["grade_time"] for r in rows), default=None)
    review["total_signals"] = len(rows)
    review["total_trades"] = len(trades)
    review["live_trades"] = [
        {
            "symbol": t["symbol"],
            "side": t["side"],
            "pnl_pct": round(float(t["pnl_pct"] or 0), 2),
            "pnl": round(float(t["pnl"] or 0), 2),
            "exit_reason": t["exit_reason"],
            "entry_time": t["entry_time"],
            "exit_time": t["exit_time"],
            "grade": t["grade_at_entry"],
            "score": t["score_at_entry"],
        }
        for t in trades
    ]
    overview["generated_issue_count"] = len(review["entry_issues"]) + len(review["exit_issues"])
    review["rules"] = [
        {
            "section": "总体判断",
            "text": f"本轮只看最近 1 天样本 {len(rows)} 个；重点问题只展示 {len(issue_pool)} 个，其中开仓问题 {len(review['entry_issues'])} 个、可能偏早退出 {len(review['exit_issues'])} 个；有效做法 {len(review['good_exits'])} 个。",
        },
        {"section": "开仓问题", "text": "最近需要特别注意的是入场后最大浮盈不足、或先出现较大回撤的信号。"},
        {"section": "平仓问题", "text": "如果最近样本仍有上行空间，尾仓应更多依赖移动止盈和多因子同步转弱。"},
        {"section": "有效做法", "text": "最近 1 天盈利退出、TP 或保护型退出会进入有效做法，用来保留真正有用的规则。"},
    ]
    return review


@app.get("/api/alpha/trade_candidates")
async def get_alpha_trade_candidates(user=Depends(get_user)):
    return {
        "candidates": fetch_latest_alpha_trade_candidates(200),
        "cooldowns": fetch_active_alpha_cooldowns(100),
    }


def compute_heat_score(tech: dict, row: dict, fut: dict | None = None) -> dict:
    fut = fut or {}
    volume_change = float(tech.get("volume_change_pct") or 0)
    rs = float(row.get("relative_strength") or 50)
    volatility_score = float(tech.get("volatility_score") or 50)
    price_position = str(row.get("price_position") or tech.get("price_position") or "")
    funding = float(fut.get("funding_rate") or 0)

    if volume_change <= -0.50:
        volume_score = 20
    elif volume_change <= 0:
        volume_score = 40
    elif volume_change <= 0.50:
        volume_score = 55
    elif volume_change <= 1.50:
        volume_score = 70
    elif volume_change <= 3.00:
        volume_score = 85
    else:
        volume_score = 95

    liquidity_score = max(0, min(100, volatility_score))
    overheat_penalty = 0
    high_position = any(x in price_position for x in ("高位", "偏高", "overbought"))
    if high_position and volume_change > 1.5:
        overheat_penalty += 12
    if funding > 0.001:
        overheat_penalty += 10
    if volatility_score < 25 and volume_change > 3:
        overheat_penalty += 8

    score = rs * 0.40 + volume_score * 0.30 + liquidity_score * 0.20 + (100 - overheat_penalty) * 0.10
    score = max(0, min(100, score))
    return {
        "score": round(score, 1),
        "volume_score": round(volume_score, 1),
        "volume_change_pct": round(volume_change, 4),
        "overheat_penalty": round(overheat_penalty, 1),
    }


@app.get("/api/scan/by_symbol/{symbol}")
async def get_symbol_detail(symbol: str, user=Depends(get_user)):
    row = fetch_symbol_detail(symbol.upper())
    if not row:
        return {"error": "Not found", "symbol": symbol}
    row = dict(row)  # V3.0: convert Row to dict for .get()
    features = {}
    if row["raw_features"]:
        try: features = json.loads(row["raw_features"])
        except: pass
    tech = features.get("technical", {})
    fut = features.get("futures", {})
    onchain = features.get("onchain", {})
    depth = features.get("depth", {})

    absorption_score = tech.get("absorption_score", tech.get("abs_score", 50))
    chip_score = tech.get("chip_score", 50)
    support_score = tech.get("support_score", 50)
    tech_score = tech.get("volatility_score", 0) * 0.2 + tech.get("trend_score", 0) * 0.2\
        + tech.get("vol_quality_score", 0) * 0.15 + tech.get("position_score", 0) * 0.2\
        + absorption_score * 0.15 + support_score * 0.1
    futures_score = fut.get("funding_score", 0) * 0.5 + fut.get("oi_score", 0) * 0.5
    heat = compute_heat_score(tech, row, fut)
    heat_score = heat["score"]

    interp = {
        "technical": {"label": "技术面", "score": round(tech_score, 1), "detail": f"{tech.get('trend_state','')} / {tech.get('chip_phase','')}", "color": "#6366f1"},
        "futures": {"label": "合约", "score": round(futures_score, 1), "detail": f"费率{fut.get('funding_rate',0)*100:.4f}%", "color": "#8b5cf6"},
        "position": {"label": "位置", "score": tech.get("position_score", 50), "detail": f"{row['price_position']} / {tech.get('support_quality','')}", "color": "#22c55e"},
        "chip": {"label": "筹码", "score": chip_score, "detail": f"{tech.get('chip_phase','')} / {tech.get('absorption_quality','')}", "color": "#eab308"},
        "heat": {"label": "热度", "score": round(heat_score, 1), "detail": f"RS {float(row['relative_strength'] or 50):.0f} / 量能 {heat['volume_score']:.0f}", "color": "#f97316"},
    }


    result = {
        "symbol": row["symbol"], "time": row["time"],
        "market_price": float(row["market_price"] or 0),
        "composite_score": float(row["composite_score"] or 0),
        "grade": row["composite_summary"], "risk_label": row["risk_label"],
        "chip_phase": row["chip_phase"], "trend_state": row["trend_state"],
        "trend_direction": row["trend_direction"],
        "volatility_level": row["volatility_level"],
        "price_position": row["price_position"],
        "relative_strength": float(row["relative_strength"] or 50),
        "interpretation": interp,
        "technical": tech,
        "futures": fut,
        "onchain": onchain,
        "depth": depth,
        "score_layers": features.get("score_layers", {}),
        "heat_detail": heat,
        "plain_signal": explain_scan_row(row),
        # V3.0
        "entry_alpha": float(row.get("entry_alpha") or 0),
        "hold_alpha": float(row.get("hold_alpha") or 0),
    }
    
    # V3.0 signal state (real-time calculation)
    try:
        from trader.entry_profiles import evaluate_profile_entry
        sym = symbol.upper()
        result["v3_signals"] = compute_v3_signals(sym, row, tech)
        result["entry_profile"] = evaluate_profile_entry(row, result["v3_signals"], result["plain_signal"].get("side"))
        result["plain_signal"] = apply_entry_profile_plain_signal(result["plain_signal"], result["entry_profile"])
    except Exception as e:
        result["v3_signals"] = {"error": str(e)}
        result["entry_profile"] = {"status": "error", "reason": str(e), "template": "unknown", "template_name": "未知"}
    history = fetch_score_history(symbol.upper())
    result["score_history"] = [{"time": h["time"], "score": float(h["composite_score"] or 0),
                                 "grade": h["composite_summary"], "price": float(h["market_price"] or 0)} for h in history]
    return result


@app.get("/api/scan/csv")
async def export_csv(user=Depends(get_user)):
    scan, rows = fetch_latest_scan()
    import csv, io
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["symbol","grade","score","price","risk","chip_phase","trend_state","volatility","position","strength"])
    for r in rows:
        w.writerow([r["symbol"], r["composite_summary"] or "",
                    f"{float(r['composite_score'] or 0):.1f}", f"{float(r['market_price'] or 0):.6f}",
                    r["risk_label"] or "", r["chip_phase"] or "", r["trend_state"] or "",
                    r["volatility_level"] or "", r["price_position"] or "",
                    f"{float(r['relative_strength'] or 50):.1f}"])
    return {"csv": output.getvalue()}


@app.get("/api/backtest/summary")
async def get_backtest_summary(user=Depends(get_user)):
    cached = cache_get("backtest_summary", 60)
    if cached is not None:
        return cached
    grades, latest = fetch_backtest_summary()
    results = []
    for r in grades:
        results.append({
            "grade": r["grade"], "count": r["count"],
            "avg_return_12h": float(r["avg_return_12h"] or 0),
            "avg_return_24h": float(r["avg_return_24h"] or 0),
            "avg_return_48h": float(r["avg_return_48h"] or 0),
            "win_rate_12h": float(r["win_rate_12h"] or 0),
            "win_rate_24h": float(r["win_rate_24h"] or 0),
            "avg_drawdown": float(r["avg_drawdown"] or 0),
            "avg_score": float(r["avg_score"] or 0),
        })
    decision_summary = {
        "total": 0,
        "latest_run_id": None,
        "latest_time": None,
        "stage_counts": [],
        "result_counts": [],
        "top_filter_reasons": [],
        "recent": [],
    }
    try:
        from shared.db import get_conn
        conn = get_conn()
        latest_decision = conn.execute(
            """SELECT run_id, time
               FROM strategy_decisions
               ORDER BY time DESC, id DESC
               LIMIT 1"""
        ).fetchone()
        if latest_decision:
            run_id = latest_decision["run_id"]
            decision_summary["latest_run_id"] = run_id
            decision_summary["latest_time"] = latest_decision["time"]
            decision_summary["total"] = conn.execute(
                "SELECT COUNT(*) FROM strategy_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            decision_summary["stage_counts"] = [
                dict(r) for r in conn.execute(
                    """SELECT decision_stage AS stage, COUNT(*) AS count
                       FROM strategy_decisions
                       WHERE run_id = ?
                       GROUP BY decision_stage
                       ORDER BY count DESC""",
                    (run_id,),
                ).fetchall()
            ]
            decision_summary["result_counts"] = [
                dict(r) for r in conn.execute(
                    """SELECT decision_result AS result, COUNT(*) AS count
                       FROM strategy_decisions
                       WHERE run_id = ?
                       GROUP BY decision_result
                       ORDER BY count DESC""",
                    (run_id,),
                ).fetchall()
            ]
            decision_summary["top_filter_reasons"] = [
                dict(r) for r in conn.execute(
                    """SELECT filter_reason AS reason, COUNT(*) AS count
                       FROM strategy_decisions
                       WHERE run_id = ?
                         AND filter_reason IS NOT NULL
                         AND filter_reason != ''
                       GROUP BY filter_reason
                       ORDER BY count DESC
                       LIMIT 8""",
                    (run_id,),
                ).fetchall()
            ]
            decision_summary["recent"] = [
                dict(r) for r in conn.execute(
                    """SELECT time, symbol, side, decision_stage, decision_result,
                              filter_reason, composite_score
                       FROM strategy_decisions
                       WHERE run_id = ?
                       ORDER BY id DESC
                       LIMIT 12""",
                    (run_id,),
                ).fetchall()
            ]
        conn.close()
    except Exception as e:
        decision_summary["error"] = str(e)
    outcome_summary = {"total": 0, "complete": 0, "by_best_side": []}
    try:
        run_id = decision_summary.get("latest_run_id")
        summary_row, by_side = fetch_signal_outcome_summary(run_id=run_id)
        outcome_summary = {
            "total": int(summary_row.get("total") or 0),
            "complete": int(summary_row.get("complete") or 0),
            "avg_return_1h": float(summary_row.get("avg_return_1h") or 0),
            "avg_return_4h": float(summary_row.get("avg_return_4h") or 0),
            "avg_return_12h": float(summary_row.get("avg_return_12h") or 0),
            "avg_return_24h": float(summary_row.get("avg_return_24h") or 0),
            "avg_mfe": float(summary_row.get("avg_mfe") or 0),
            "avg_mae": float(summary_row.get("avg_mae") or 0),
            "direction_accuracy": (
                float(summary_row["direction_accuracy"])
                if summary_row.get("direction_accuracy") is not None else None
            ),
            "by_best_side": by_side,
        }
    except Exception as e:
        outcome_summary["error"] = str(e)
    backtest_status = {
        "backtest_rows": 0,
        "review_rows": 0,
        "factor_rows": 0,
        "score_count": 0,
        "score_min_time": None,
        "score_max_time": None,
        "latest_price_time": None,
        "latest_review_time": None,
        "latest_factor_time": None,
        "waiting_for_mature_returns": False,
        "plain": "暂无成熟回测结果。",
    }
    try:
        from shared.db import get_conn

        conn = get_conn()
        bt = conn.execute(
            "SELECT COUNT(*) AS count, MAX(run_time) AS latest_run FROM backtest_results"
        ).fetchone()
        review_row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(run_time) AS latest_run FROM backtest_review"
        ).fetchone()
        factor_row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(run_time) AS latest_run FROM factor_performance"
        ).fetchone()
        score_row = conn.execute(
            "SELECT COUNT(*) AS count, MIN(time) AS min_time, MAX(time) AS max_time FROM alpha_scores"
        ).fetchone()
        price_row = conn.execute(
            "SELECT MAX(time) AS max_time FROM candles_1h"
        ).fetchone()

        backtest_rows = int(bt["count"] or 0)
        score_count = int(score_row["count"] or 0)
        latest_price_time = price_row["max_time"] if price_row else None
        waiting = backtest_rows == 0 and score_count > 0 and bool(latest_price_time)
        if waiting:
            plain = (
                "扫描评分和行情已经有数据，但当前信号还没走完 6h/12h/24h "
                "未来收益验证窗口，所以暂时不会生成等级收益概览。"
            )
        elif score_count == 0:
            plain = "暂无扫描评分样本，回测还没有可验证的信号来源。"
        elif not latest_price_time:
            plain = "暂无 1h 行情数据，回测无法计算未来收益。"
        else:
            plain = "暂无成熟回测结果，等待下一轮回测任务产出。"

        backtest_status = {
            "backtest_rows": backtest_rows,
            "review_rows": int(review_row["count"] or 0),
            "factor_rows": int(factor_row["count"] or 0),
            "score_count": score_count,
            "score_min_time": score_row["min_time"],
            "score_max_time": score_row["max_time"],
            "latest_price_time": latest_price_time,
            "latest_review_time": review_row["latest_run"],
            "latest_factor_time": factor_row["latest_run"],
            "waiting_for_mature_returns": waiting,
            "plain": plain,
        }
        conn.close()
    except Exception as e:
        backtest_status["error"] = str(e)
    return cache_set("backtest_summary", {
        "latest_run": latest,
        "grades": results,
        "decision_summary": decision_summary,
        "outcome_summary": outcome_summary,
        "backtest_status": backtest_status,
    })


@app.get("/api/factor/performance")
async def get_factor_performance(factor: str = None, user=Depends(get_user)):
    """馃敡 鍥犲瓙褰掑洜鍒嗘瀽缁撴灉"""
    try:
        rows = fetch_factor_performance(limit=200)
        payload = [dict(r) for r in rows]
        if factor:
            payload = [r for r in payload if r.get("factor_name") == factor]
        return {"rows": payload, "count": len(payload)}
    except Exception as e:
        return {"error": str(e), "rows": []}


@app.get("/api/backtest/recent")
async def get_recent_signals(grade: str = "S1", limit: int = 50, user=Depends(get_user)):
    cache_key = f"backtest_recent:{grade}:{limit}"
    cached = cache_get(cache_key, 60)
    if cached is not None:
        return cached
    rows = fetch_recent_signals(grade, limit)
    return cache_set(cache_key, format_backtest_signals(rows))


@app.get("/api/backtest/signals")
async def get_backtest_signals(grade: str = "all", limit: int = 200, user=Depends(get_user)):
    cache_key = f"backtest_signals:{grade}:{limit}"
    cached = cache_get(cache_key, 60)
    if cached is not None:
        return cached
    rows = fetch_recent_signals(grade, limit)
    return cache_set(cache_key, format_backtest_signals(rows))
# ---- 瀹炵洏浜ゆ槗 API ----


@app.get("/api/backtest/factor_analysis")
async def get_factor_analysis(user=Depends(get_user)):
    """Return factor analysis result."""
    from shared.db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT result FROM factor_analysis ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row and row["result"]:
            return json.loads(row["result"])
        return _build_factor_analysis_from_performance(conn)
    except Exception as e:
        return {"error": str(e), "recommendations": [], "candidate_recommendations": []}
    finally:
        conn.close()


@app.get("/api/backtest/review")
async def get_backtest_review(user=Depends(get_user)):
    """Return latest persisted backtest review."""
    from shared.db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT run_time, review_json FROM backtest_review ORDER BY run_time DESC LIMIT 1"
        ).fetchone()
        if row:
            data = json.loads(row["review_json"])
            data["_run_time"] = row["run_time"]
            return _enrich_backtest_review(conn, data)
        return {"error": "鏆傛棤澶嶇洏鏁版嵁锛岃杩愯鍥炴祴"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


@app.get("/api/backtest/factor_weights")
async def get_factor_weights(user=Depends(get_user)):
    """Return current factor weight config."""
    import json
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "engine", "factor_weights.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e), "custom_factors": {}, "sub_weights": {}, "category_weights": {}}


@app.post("/api/backtest/factor_weights")
async def save_factor_weights(body: dict, user=Depends(get_user)):
    """Save factor weight config, including custom factors."""
    import json
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "engine", "factor_weights.json")
        # Merge only fields provided by the caller.
        existing = {"version": 1, "custom_factors": {}, "sub_weights": {}, "category_weights": {}}
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except:
            pass
        for key in ["custom_factors", "sub_weights", "category_weights"]:
            if key in body:
                existing[key] = body[key]
        existing["version"] = body.get("version", existing.get("version", 1))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


# ---- 瀹炵洏浜ゆ槗 API + 缂撳瓨 ----
def _seed_learning_candidates_from_latest():
    from shared.db import get_conn
    from shared.strategy_learning import generate_policy_candidates

    conn = get_conn()
    try:
        review = None
        factor = None
        row = conn.execute(
            "SELECT run_time, review_json FROM backtest_review ORDER BY run_time DESC LIMIT 1"
        ).fetchone()
        if row and row["review_json"]:
            review = json.loads(row["review_json"])
            review["_run_time"] = row["run_time"]
        frow = conn.execute(
            "SELECT result FROM factor_analysis ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if frow and frow["result"]:
            factor = json.loads(frow["result"])
    finally:
        conn.close()
    if review or factor:
        generate_policy_candidates(review=review, factor_result=factor)


@app.get("/api/strategy/learning")
async def get_strategy_learning(user=Depends(get_user)):
    """Strategy learning loop: candidates, shadow/active statuses, active policy."""
    from shared.strategy_learning import fetch_learning_summary

    try:
        data = fetch_learning_summary()
        if not data.get("candidates"):
            _seed_learning_candidates_from_latest()
            data = fetch_learning_summary()
        return data
    except Exception as e:
        return {"error": str(e), "candidates": [], "status_counts": {}}


@app.post("/api/strategy/learning/{candidate_id}/status")
async def update_strategy_learning_status(candidate_id: int, body: dict, user=Depends(get_user)):
    from shared.strategy_learning import update_candidate_status

    try:
        result = update_candidate_status(
            candidate_id,
            str(body.get("status") or ""),
            detail=body.get("detail") or {"source": "ui"},
        )
        _response_cache.clear()
        _api_cache.clear()
        return result
    except Exception as e:
        return {"error": str(e)}


_trading_status_cache = {"data": None, "time": 0}
_CACHE_TTL = 10  # 10绉掔紦瀛?


def _clear_trading_caches():
    _trading_status_cache["data"] = None
    _trading_status_cache["time"] = 0
    _response_cache.clear()
    _api_cache.clear()


def _position_strategy_source(conn, symbol):
    row = conn.execute(
        "SELECT strategy_source FROM position_history WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if row and row["strategy_source"]:
        return row["strategy_source"]
    return "normal"


def _flatten_positions_by_source(strategy_source, reason):
    from datetime import datetime, timezone
    from trader.exchange import BinanceFutures
    from trader.execution import ExecutionEngine
    from shared.db import get_conn

    ex = BinanceFutures()
    conn = get_conn()
    try:
        engine = ExecutionEngine(ex)
        positions = ex.get_positions()
        actions = []
        for pos in positions:
            source = _position_strategy_source(conn, pos["symbol"])
            if source != strategy_source:
                continue
            close_side = "SELL" if pos.get("side") == "LONG" else "BUY"
            actions.append({
                "action": "close",
                "symbol": pos["symbol"],
                "side": close_side,
                "position_side": pos.get("side"),
                "close_price": pos.get("mark_price"),
                "reason": reason,
                "strategy_source": source,
                "run_id": f"manual-switch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            })
        if not actions:
            return {"closed": 0, "results": []}
        results = engine.execute(actions)
        return {
            "closed": sum(1 for r in results if r.get("status") == "ok"),
            "results": results,
        }
    finally:
        conn.close()
        ex.close()


@app.get("/api/trading/controls")
async def get_trading_controls(user=Depends(get_user)):
    return get_trading_runtime_controls()


@app.post("/api/trading/controls")
async def update_trading_controls(body: dict, user=Depends(get_user)):
    key_map = {
        "normal": "normal_trading_enabled",
        "normal_trading_enabled": "normal_trading_enabled",
        "alpha": "alpha_trading_enabled",
        "alpha_trading_enabled": "alpha_trading_enabled",
    }
    mode = str(body.get("mode") or body.get("key") or "").strip()
    key = key_map.get(mode)
    if not key:
        return {"error": "unsupported trading control"}
    enabled = bool(body.get("enabled"))
    controls = set_trading_runtime_control(key, enabled)
    close_result = {"closed": 0, "results": []}
    if not enabled:
        source = "alpha" if key == "alpha_trading_enabled" else "normal"
        label = "Alpha" if source == "alpha" else "普通"
        close_result = _flatten_positions_by_source(
            source,
            f"manual_{source}_trading_switch_off: 页面关闭{label}交易",
        )
    _clear_trading_caches()
    return {
        "ok": True,
        "controls": controls,
        "close_result": close_result,
    }


def _build_local_trading_status(error=None):
    from shared.db import fetch_position_trade_groups, get_conn
    from trader.config import TRADING_CONFIG

    controls = get_trading_runtime_controls()
    conn = get_conn()
    try:
        excluded = ("historical_import", "閸樺棗褰剁悰銉ョ秿(閹靛濮╅獮鍏呯波)")
        latest_snapshot = conn.execute("SELECT MAX(time) AS t FROM positions_history").fetchone()["t"]
        position_rows = []
        if latest_snapshot:
            position_rows = conn.execute(
                "SELECT * FROM positions_history WHERE time = ? ORDER BY symbol",
                (latest_snapshot,),
            ).fetchall()
        recent_trades = fetch_position_trade_groups(100)
        closed = conn.execute(
            """SELECT pnl, pnl_pct, exit_reason, entry_time, exit_time
               FROM trades
               WHERE exit_time != 'N/A'
                 AND exit_time IS NOT NULL
                 AND exit_reason NOT IN (?,?)
                 AND source='system'""",
            excluded,
        ).fetchall()
        total_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE exit_reason NOT IN (?,?) AND source='system'",
            excluded,
        ).fetchone()[0]
        total_pnl = sum(r["pnl"] or 0 for r in closed)
        wins = [r for r in closed if (r["pnl"] or 0) > 0]
        losses = [r for r in closed if (r["pnl"] or 0) <= 0]
        total_closed = len(closed)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = round(win_count / total_closed * 100, 2) if total_closed else 0
        total_win_val = sum(r["pnl"] or 0 for r in wins)
        total_loss_val = abs(sum(r["pnl"] or 0 for r in losses))
        profit_ratio = round(abs(total_win_val / total_loss_val), 2) if total_loss_val else 0
        reason_stats = {}
        for r in closed:
            reason = r["exit_reason"] or "unknown"
            reason_stats.setdefault(reason, {"count": 0, "total_pnl": 0, "wins": 0})
            reason_stats[reason]["count"] += 1
            reason_stats[reason]["total_pnl"] += r["pnl"] or 0
            if (r["pnl"] or 0) > 0:
                reason_stats[reason]["wins"] += 1
        positions = []
        for p in position_rows:
            entry_price = p["entry_price"] or 0
            qty = p["quantity"] or 0
            leverage = p["leverage"] or 1
            margin = entry_price * qty / max(leverage, 1) if entry_price and qty else 0
            unrealized = p["unrealized_pnl"] or 0
            positions.append({
                "symbol": p["symbol"],
                "side": p["position_side"] or p["side"],
                "position_side": p["position_side"],
                "quantity": qty,
                "entry_price": entry_price,
                "mark_price": p["mark_price"],
                "unrealized_pnl": unrealized,
                "leverage": leverage,
                "margin": round(margin, 2),
                "margin_ratio": None,
                "pnl_pct": round(unrealized / margin * 100, 2) if margin else 0,
                "invested": round(entry_price * qty, 2),
                "entry_reason": None,
                "snapshot_time": p["time"],
            })
        realized = round(total_pnl, 2)
        unrealized_total = round(sum(p["unrealized_pnl"] or 0 for p in positions), 2)
        initial_capital = TRADING_CONFIG.get("total_capital", 5000)
        return {
            "data_source": "local_fallback",
            "warning": str(error) if error else None,
            "balance": round(initial_capital + realized + unrealized_total, 2),
            "total_trades": total_trades,
            "positions": positions,
            "recent_trades": recent_trades,
            "total_pnl": round(realized + unrealized_total, 2),
            "realized_pnl": realized,
            "trades_pnl": realized,
            "income_pnl": round(conn.execute("SELECT SUM(realized_pnl) FROM fills WHERE side='REALIZED_PNL'").fetchone()[0] or 0, 2),
            "current_positions": len(positions),
            "total_opens": total_trades,
            "total_closed": total_closed,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "profit_ratio": profit_ratio,
            "reason_stats": {
                k: {
                    "count": v["count"],
                    "total_pnl": round(v["total_pnl"], 2),
                    "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
                }
                for k, v in sorted(reason_stats.items(), key=lambda x: -x[1]["count"])
            },
            "latest_position_snapshot": latest_snapshot,
            "trading_controls": controls,
        }
    finally:
        conn.close()


@app.get("/api/trading/statu")
@app.get("/api/trading/status")
async def get_trading_status(user=Depends(get_user)):
    """Merge live status and stats with cache."""
    import time
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from shared.db import fetch_position_trade_groups, get_conn

    # Check cache.
    if _trading_status_cache["data"] and time.time() - _trading_status_cache["time"] < _CACHE_TTL:
        return _trading_status_cache["data"]

    # Return an explicit unavailable state when Binance keys are missing.
    import os
    from trader.config import EXCHANGE_CONFIG
    missing_binance_vars = []
    if not EXCHANGE_CONFIG.get("api_key"):
        missing_binance_vars.append("TESTNET_API_KEY" if EXCHANGE_CONFIG.get("testnet") else "BINANCE_API_KEY")
    if not EXCHANGE_CONFIG.get("api_secret"):
        missing_binance_vars.append("TESTNET_API_SECRET" if EXCHANGE_CONFIG.get("testnet") else "BINANCE_API_SECRET")
    if missing_binance_vars:
        return {
            "error": "missing " + ", ".join(missing_binance_vars),
            "data_source": "binance_live",
            "balance": 0,
            "positions": [],
            "recent_trades": [],
            "total_pnl": 0,
            "trading_controls": get_trading_runtime_controls(),
        }

    EXCLUDED_REASONS = ("historical_import", "鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)")

    from trader.exchange import BinanceFutures
    from trader.config import TRADING_CONFIG
    INITIAL_CAPITAL = TRADING_CONFIG.get("total_capital", 5000)
    controls = get_trading_runtime_controls()
    ex = BinanceFutures()
    try:
        margin_data = ex.get_margin_balance()
        balance = margin_data["totalWalletBalance"]
        margin_balance = margin_data.get("totalMarginBalance") or balance
        positions = ex.get_positions()
        conn = get_conn()
        recent_trades = fetch_position_trade_groups(100)
        total_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE exit_reason NOT IN ('historical_import','鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)') AND source='system'").fetchone()[0]
        
        # ---- stats 鏁版嵁 ----
        closed = conn.execute(
            "SELECT pnl, pnl_pct, exit_reason, entry_time, exit_time FROM trades WHERE exit_time != 'N/A' AND exit_time IS NOT NULL AND exit_reason NOT IN ('historical_import','鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)') AND source='system'"
        ).fetchall()
        total_closed = len(closed)
        total_pnl = sum(r["pnl"] or 0 for r in closed)
        wins = [r for r in closed if (r["pnl"] or 0) > 0]
        losses = [r for r in closed if (r["pnl"] or 0) <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = round(win_count / total_closed * 100, 2) if total_closed else 0
        total_win_val = sum(r["pnl"] or 0 for r in wins)
        total_loss_val = abs(sum(r["pnl"] or 0 for r in losses))
        profit_ratio = round(abs(total_win_val / total_loss_val), 2) if total_loss_val else 0
        
        reason_stats = {}
        for r in closed:
            reason = r["exit_reason"] or "鏈煡"
            if reason not in reason_stats:
                reason_stats[reason] = {"count": 0, "total_pnl": 0, "wins": 0}
            reason_stats[reason]["count"] += 1
            reason_stats[reason]["total_pnl"] += r["pnl"] or 0
            if (r["pnl"] or 0) > 0:
                reason_stats[reason]["wins"] += 1

        def position_management_fields(symbol):
            r = conn.execute("SELECT * FROM position_history WHERE symbol=?", (symbol,)).fetchone()
            if not r:
                return {
                    "entry_reason": None,
                    "entry_score": 0,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "highest_price": None,
                    "last_exit_reason": None,
                    "last_exit_plain": None,
                    "strategy_source": "normal",
                    "signal_source": None,
                    "alpha_symbol": None,
                    "alpha_profile": None,
                    "alpha_entry_level": None,
                    "alpha_score": None,
                    "alpha_suggested_position_pct": None,
                    "roll_layer": 0,
                    "last_roll_time": None,
                    "protected_profit": 0,
                    "max_floating_pnl": 0,
                    "roll_enabled": False,
                    "roll_block_reason": None,
                }
            return {
                "entry_reason": r["entry_reason"],
                "entry_score": float(r["entry_score"] or 0),
                "tp1_hit": bool(r["tp1_hit"]) if "tp1_hit" in r.keys() else False,
                "tp2_hit": bool(r["tp2_hit"]) if "tp2_hit" in r.keys() else False,
                "highest_price": float(r["highest_price"] or 0) if "highest_price" in r.keys() else None,
                "last_exit_reason": r["last_exit_reason"] if "last_exit_reason" in r.keys() else None,
                "last_exit_plain": plain_reason(r["last_exit_reason"]) if "last_exit_reason" in r.keys() else None,
                "strategy_source": r["strategy_source"] if "strategy_source" in r.keys() else "normal",
                "signal_source": r["signal_source"] if "signal_source" in r.keys() else None,
                "alpha_symbol": r["alpha_symbol"] if "alpha_symbol" in r.keys() else None,
                "alpha_profile": r["alpha_profile"] if "alpha_profile" in r.keys() else None,
                "alpha_entry_level": r["alpha_entry_level"] if "alpha_entry_level" in r.keys() else None,
                "alpha_score": float(r["alpha_score"] or 0) if "alpha_score" in r.keys() else None,
                "alpha_suggested_position_pct": float(r["alpha_suggested_position_pct"] or 0) if "alpha_suggested_position_pct" in r.keys() else None,
                "roll_layer": int(r["roll_layer"] or 0) if "roll_layer" in r.keys() else 0,
                "last_roll_time": r["last_roll_time"] if "last_roll_time" in r.keys() else None,
                "protected_profit": float(r["protected_profit"] or 0) if "protected_profit" in r.keys() else 0,
                "max_floating_pnl": float(r["max_floating_pnl"] or 0) if "max_floating_pnl" in r.keys() else 0,
                "roll_enabled": bool(r["roll_enabled"]) if "roll_enabled" in r.keys() else False,
                "roll_block_reason": r["roll_block_reason"] if "roll_block_reason" in r.keys() else None,
            }

        decision_panel = {
            "latest_run_id": None,
            "latest_time": None,
            "top_reasons": [],
            "recent": [],
            "entry_gate_mode": "per_symbol_entry_profile",
            "entry_gate_plain": "开仓线已改为按币种模板判断；全局60分不再提前拦截试探仓。",
            "legacy_global_score_gate": "disabled_for_live_entry",
            "regime_effect_plain": "行情状态只调整开仓名额和仓位，不再直接抬高综合分门槛。",
        }
        try:
            from shared.strategy_learning import load_entry_policy
            active_policy = load_entry_policy()
            decision_panel["active_entry_policy_count"] = len(active_policy.get("rules") or [])
            decision_panel["active_entry_policy_version"] = active_policy.get("version")
        except Exception:
            decision_panel["active_entry_policy_count"] = 0
            decision_panel["active_entry_policy_version"] = None
        latest_decision = conn.execute(
            """SELECT run_id, time
               FROM strategy_decisions
               ORDER BY time DESC, id DESC
               LIMIT 1"""
        ).fetchone()
        if latest_decision:
            run_id = latest_decision["run_id"]
            decision_panel["latest_run_id"] = run_id
            decision_panel["latest_time"] = latest_decision["time"]
            decision_panel["last_execution_time"] = latest_decision["time"]
            decision_panel["top_reasons"] = [
                {"reason": r["reason"], "plain": plain_reason(r["reason"]), "count": r["count"]}
                for r in conn.execute(
                    """SELECT filter_reason AS reason, COUNT(*) AS count
                       FROM strategy_decisions
                       WHERE run_id = ?
                         AND filter_reason IS NOT NULL
                         AND filter_reason != ''
                       GROUP BY filter_reason
                       ORDER BY count DESC
                       LIMIT 6""",
                    (run_id,),
                ).fetchall()
            ]
            decision_panel["recent"] = [
                {
                    "time": r["time"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "stage": r["decision_stage"],
                    "result": r["decision_result"],
                    "score": float(r["composite_score"] or 0),
                    "reason": r["filter_reason"],
                    "plain": plain_reason(r["filter_reason"]),
                }
                for r in conn.execute(
                    """SELECT time, symbol, side, decision_stage, decision_result,
                              filter_reason, composite_score
                       FROM strategy_decisions
                       WHERE run_id = ?
                       ORDER BY id DESC
                       LIMIT 10""",
                    (run_id,),
                ).fetchall()
            ]
        result = {
            "data_source": "binance_live",
            "warning": None,
            "balance": balance,
            "total_trades": total_trades,
            "positions": [
                {
                    "symbol": p["symbol"], "side": p["side"],
                    "quantity": p["quantity"], "entry_price": p["entry_price"],
                    "mark_price": p["mark_price"],
                    "unrealized_pnl": p["unrealized_pnl"],
                    "leverage": p["leverage"],
                    "margin": round(p.get("margin") or 0, 2),
                    "margin_ratio": round((p.get("maint_margin") or 0) / margin_balance * 100, 4) if margin_balance else 0,
                    "pnl_pct": round(p["unrealized_pnl"] / p["margin"] * 100, 2) if p.get("margin") else 0,
                    "invested": round(p.get("notional") or (p["entry_price"] * p["quantity"]), 2),
                    "initial_margin": round(p.get("initial_margin") or 0, 2),
                    "maint_margin": round(p.get("maint_margin") or 0, 2),
                    "position_initial_margin": round(p.get("position_initial_margin") or 0, 2),
                    "open_order_initial_margin": round(p.get("open_order_initial_margin") or 0, 2),
                    "isolated_margin": round(p.get("isolated_margin") or 0, 2),
                    "notional": round(p.get("notional") or 0, 2),
                    "margin_asset": p.get("margin_asset"),
                    "margin_type": p.get("margin_type"),
                    "liquidation_price": p.get("liquidation_price"),
                    "break_even_price": p.get("break_even_price"),
                    "risk_api_version": p.get("risk_api_version"),
                    **position_management_fields(p["symbol"]),
                }
                for p in positions
            ],
            "recent_trades": recent_trades,
            "account_margin_balance": round(margin_balance, 2),
            # stats 瀛楁
            "total_pnl": round(balance - INITIAL_CAPITAL, 2) if isinstance(balance, (int, float)) else 0,
            "trades_pnl": round(total_pnl, 2),
            "income_pnl": round(conn.execute("SELECT SUM(pnl) FROM trades WHERE source='income_auto'").fetchone()[0] or 0, 2),
            "current_positions": len(positions),
            "total_opens": total_trades,
            "total_closed": total_closed,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "profit_ratio": profit_ratio,
            "reason_stats": {
                k: {
                    "count": v["count"],
                    "total_pnl": round(v["total_pnl"], 2),
                    "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
                    "plain": plain_reason(k),
                }
                for k, v in sorted(reason_stats.items(), key=lambda x: -x[1]["count"])
            },
            "decision_panel": decision_panel,
            "trading_controls": controls,
        }
        # 淇濆瓨缂撳瓨
        _trading_status_cache["data"] = result
        _trading_status_cache["time"] = time.time()
        return result
    except Exception as e:
        return {
            "error": str(e),
            "data_source": "binance_live",
            "balance": 0,
            "positions": [],
            "recent_trades": [],
            "total_pnl": 0,
            "trading_controls": get_trading_runtime_controls(),
        }
    finally:
        ex.close()


@app.get("/api/trading/stats")
async def get_trading_stats(user=Depends(get_user)):
    from shared.db import get_conn
    conn = get_conn()
    try:
        EXCL = ("historical_import", "鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)")
        # 鎵鏈夊凡骞充粨浜ゆ槗锛堟帓闄ゅ巻鍙插鍏ワ級
        closed = conn.execute(
            "SELECT pnl, pnl_pct, exit_reason, entry_time, exit_time FROM trades WHERE exit_time != 'N/A' AND exit_time IS NOT NULL AND exit_reason NOT IN (?,?)",
            EXCL
        ).fetchall()
        
        total_closed = len(closed)
        total_pnl = sum(r["pnl"] or 0 for r in closed)
        wins = [r for r in closed if (r["pnl"] or 0) > 0]
        losses = [r for r in closed if (r["pnl"] or 0) <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = round(win_count / total_closed * 100, 2) if total_closed else 0
        
        # 鐩堜簭姣?        total_win = sum(r["pnl"] or 0 for r in wins)
        total_loss = abs(sum(r["pnl"] or 0 for r in losses))
        profit_ratio = round(abs(total_win / total_loss), 2) if total_loss else 0
        
        # 褰撳墠鎸佷粨鏁?        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from trader.exchange import BinanceFutures
        ex = BinanceFutures()
        pos = ex.get_positions()
        current_pos = len(pos)
        ex.close()
        
        # 鎸夊钩浠撳師鍥犵粺璁?        reason_stats = {}
        for r in closed:
            reason = r["exit_reason"] or "鏈煡"
            if reason not in reason_stats:
                reason_stats[reason] = {"count": 0, "total_pnl": 0, "wins": 0}
            reason_stats[reason]["count"] += 1
            reason_stats[reason]["total_pnl"] += r["pnl"] or 0
            if (r["pnl"] or 0) > 0:
                reason_stats[reason]["wins"] += 1
        
        # 鎬诲紑浠撴鏁帮紙鎺掗櫎鍘嗗彶瀵煎叆锛?        total_opens = conn.execute("SELECT count(*) FROM trades WHERE exit_reason NOT IN (?,?)", EXCL).fetchone()[0]
        
        return {
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": round(total_pnl, 2),
            "current_positions": current_pos,
            "total_opens": total_opens,
            "total_closed": total_closed,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "profit_ratio": profit_ratio,
            "reason_stats": {
                k: {
                    "count": v["count"],
                    "total_pnl": round(v["total_pnl"], 2),
                    "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
                }
                for k, v in sorted(reason_stats.items(), key=lambda x: -x[1]["count"])
            },
        }
    except Exception as e:
        return {"error": str(e), "total_pnl": 0, "current_positions": 0, "total_opens": 0}
    finally:
        pass


@app.get("/api/trading/positions_history")
async def get_positions_history(page: int = 1, limit: int = 20, user=Depends(get_user)):
    from shared.db import get_conn
    conn = get_conn()
    try:
        offset = (page - 1) * limit
        rows = conn.execute(
            """SELECT *
               FROM positions_history
               ORDER BY time DESC
               LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM positions_history"
        ).fetchone()[0]
        return {
            "total": total,
            "page": page,
            "positions": [
                {
                    "time": p["time"],
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "position_side": p["position_side"],
                    "quantity": p["quantity"],
                    "entry_price": p["entry_price"],
                    "mark_price": p["mark_price"],
                    "unrealized_pnl": p["unrealized_pnl"],
                    "leverage": p["leverage"],
                    "stop_loss": p["stop_loss"],
                    "take_profit": p["take_profit"],
                }
                for p in rows
            ],
        }
    except Exception as e:
        return {"error": str(e), "positions": []}
    finally:
        pass


@app.get("/api/strategy/decisions")
async def get_strategy_decisions(
    page: int = 1,
    limit: int = 100,
    symbol: str | None = None,
    stage: str | None = None,
    result: str | None = None,
    user=Depends(get_user),
):
    from shared.db import get_conn

    conn = get_conn()
    try:
        page = max(page, 1)
        limit = max(1, min(limit, 500))
        offset = (page - 1) * limit
        where = []
        params = []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol.upper())
        if stage:
            where.append("decision_stage = ?")
            params.append(stage)
        if result:
            where.append("decision_result = ?")
            params.append(result)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT *
                FROM strategy_decisions
                {where_sql}
                ORDER BY time DESC, id DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM strategy_decisions {where_sql}",
            params,
        ).fetchone()[0]
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "decisions": [dict(r) for r in rows],
        }
    except Exception as e:
        return {"error": str(e), "decisions": []}
    finally:
        conn.close()


