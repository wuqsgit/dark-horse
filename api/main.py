"""AlphaDog API Server 鈥?FastAPI (SQLite)"""
import asyncio
import os, sys, json, time
from fastapi import FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    fetch_alpha_score_history,
    fetch_alpha_symbol_detail,
    fetch_latest_alpha_scan,
    fetch_latest_alpha_trade_candidate,
    fetch_latest_alpha_trade_candidates,
    fetch_active_alpha_cooldowns,
    fetch_latest_scan,
    fetch_latest_scan_meta,
    fetch_symbol_detail,
    fetch_score_history,
    fetch_signal_outcome_summary,
    get_trading_runtime_controls,
    set_trading_runtime_control,
    fetch_market_data_health,
    init_db,
)
from shared.policy_loop import (
    fetch_policy_loop_summary,
    fetch_exit_reviews,
    fetch_exit_review_summaries,
    fetch_position_action_evidence,
    generate_and_activate_policies,
    label_decision_outcomes,
    review_position_trade_exits,
    review_position_trade_entries,
    summarize_entry_reviews,
    summarize_exit_reviews,
    clear_legacy_backtest_data,
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
_versioned_cache = {}
_scan_payload_cache = {"scan_id": None, "payload": None, "body": None, "time": 0}
_scan_refresh_task = None


_SCAN_CACHE_TTL = 5
_BACKTEST_CACHE_TTL = 300
_TRADING_CACHE_TTL = 10
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_FAST_CACHE_PATHS = {
    "/api/scan/latest",
    "/api/alpha/scan/latest",
    "/api/scan/details",
    "/api/backtest/summary",
    "/api/backtest/recent",
    "/api/backtest/signals",
    "/api/backtest/factor_analysis",
    "/api/backtest/review",
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


def versioned_cache_get(key, version, ttl=None):
    item = _versioned_cache.get(key)
    if not item or item.get("version") != version:
        return None
    if ttl is not None and time.time() - item.get("time", 0) >= ttl:
        return None
    return item.get("data")


def versioned_cache_set(key, version, data):
    _versioned_cache[key] = {"version": version, "time": time.time(), "data": data}
    return data


def json_response(data):
    return Response(
        content=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
        media_type="application/json",
        headers=_NO_STORE_HEADERS,
    )


def versioned_response_get(key, version, ttl=None):
    body = versioned_cache_get(key, version, ttl)
    if body is None:
        return None
    return Response(content=body, media_type="application/json", headers=_NO_STORE_HEADERS)


def versioned_response_set(key, version, data):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    versioned_cache_set(key, version, body)
    return Response(content=body, media_type="application/json", headers=_NO_STORE_HEADERS)


def seed_response_cache(path, data):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    for host in ("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:3000", "http://localhost:3000"):
        _response_cache[f"{host}{path}"] = {
            "time": time.time(),
            "body": body,
            "status_code": 200,
            "media_type": "application/json",
            "headers": dict(_NO_STORE_HEADERS),
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
            headers={**_NO_STORE_HEADERS, **item.get("headers", {}), "X-Cache": "HIT"},
        )
    response = await call_next(request)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    if response.status_code == 200:
        cache_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
        }
        cache_headers.update(_NO_STORE_HEADERS)
        payload = {
            "time": time.time(),
            "body": body,
            "status_code": response.status_code,
            "media_type": response.media_type or response.headers.get("content-type", "application/json"),
            "headers": cache_headers,
        }
        _response_cache[key] = payload
        if request.url.path == "/api/trading/status":
            _response_cache[key.replace("/api/trading/status", "/api/trading/statu")] = payload
        elif request.url.path == "/api/trading/statu":
            _response_cache[key.replace("/api/trading/statu", "/api/trading/status")] = payload
    response_headers = {
        k: v for k, v in response.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
    }
    response_headers.update(_NO_STORE_HEADERS)
    return Response(
        content=body,
        status_code=response.status_code,
        media_type=response.media_type,
        headers=response_headers,
    )


@app.on_event("startup")
async def startup():
    global _scan_refresh_task
    init_db()
    try:
        await asyncio.to_thread(_refresh_scan_payload_sync)
        if _scan_refresh_task is None:
            _scan_refresh_task = asyncio.create_task(_scan_cache_refresher())
        summary = await get_backtest_summary(user="admin")
        seed_response_cache("/api/backtest/summary", summary)
        seed_response_cache("/api/backtest/recent?grade=S1&limit=50", (summary.get("actions") or [])[:50])
        weights_path = os.path.join(os.path.dirname(__file__), "..", "engine", "factor_weights.json")
        with open(weights_path, encoding="utf-8") as f:
            seed_response_cache("/api/backtest/factor_weights", json.load(f))
        await get_latest_alpha_scan(user="admin")
        await get_alpha_trade_candidates(user="admin")
        trading_status = await get_trading_status(user="admin")
        seed_response_cache("/api/trading/status", trading_status)
        seed_response_cache("/api/trading/statu", trading_status)
    except Exception:
        pass


async def get_user():
    return "admin"


@app.get("/api/market-data/health")
async def get_market_data_health(user=Depends(get_user)):
    return json_response(await asyncio.to_thread(fetch_market_data_health))


def _build_scan_payload(scan, rows):
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
        market_phase = features.get("market_phase") or {}
        v3_signals = compute_v3_signals(r["symbol"], r, tech)
        try:
            from trader.entry_profiles import evaluate_profile_entry
            entry_profile_full = evaluate_profile_entry(r, v3_signals, plain.get("side"))
        except Exception as e:
            entry_profile_full = {"status": "error", "reason": str(e), "template": "unknown", "template_name": "鏈煡"}
        plain = apply_entry_profile_plain_signal(plain, entry_profile_full)
        entry_profile = {
            "status": entry_profile_full.get("status"),
            "reason": entry_profile_full.get("reason"),
            "template": entry_profile_full.get("template"),
            "template_name": entry_profile_full.get("template_name"),
        }
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
            "entry_profile": entry_profile,
            "market_phase": market_phase,
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
    return {"scan_time": scan["time"], "count": len(symbols), "symbols": symbols}


def _refresh_scan_payload_sync():
    latest = fetch_latest_scan_meta()
    if latest and _scan_payload_cache["scan_id"] == latest["scan_id"] and _scan_payload_cache["payload"] is not None:
        return _scan_payload_cache["payload"]
    scan, rows = fetch_latest_scan()
    if not scan:
        payload = {"scan_time": None, "symbols": []}
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        _scan_payload_cache.update({"scan_id": None, "payload": payload, "body": body, "time": time.time()})
        return payload
    if _scan_payload_cache["scan_id"] == scan["scan_id"] and _scan_payload_cache["payload"] is not None:
        return _scan_payload_cache["payload"]
    payload = _build_scan_payload(scan, rows)
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    _scan_payload_cache.update({"scan_id": scan["scan_id"], "payload": payload, "body": body, "time": time.time()})
    return payload


def _scan_payload_response():
    body = _scan_payload_cache.get("body")
    if body is not None:
        return Response(content=body, media_type="application/json")
    return json_response(_scan_payload_cache.get("payload") or {"scan_time": None, "symbols": []})


async def _scan_cache_refresher():
    while True:
        try:
            await asyncio.to_thread(_refresh_scan_payload_sync)
        except Exception:
            pass
        await asyncio.sleep(_SCAN_CACHE_TTL)


@app.get("/api/scan/latest")
async def get_latest_scan(user=Depends(get_user)):
    scan = await asyncio.to_thread(fetch_latest_scan_meta)
    if not scan:
        return json_response({"scan_time": None, "symbols": []})
    if _scan_payload_cache["scan_id"] == scan["scan_id"] and _scan_payload_cache["payload"] is not None:
        return _scan_payload_response()
    await asyncio.to_thread(_refresh_scan_payload_sync)
    return _scan_payload_response()


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


def _alpha_dashboard_version():
    from shared.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT
                   (SELECT scan_id FROM alpha_scan_scores ORDER BY time DESC LIMIT 1) AS scan_id,
                   (SELECT MAX(time) FROM alpha_scan_scores) AS scan_time,
                   (SELECT MAX(id) FROM alpha_trade_candidates) AS candidate_id,
                   (SELECT MAX(updated_at) FROM alpha_trade_candidates) AS candidate_updated,
                   (SELECT MAX(time) FROM alpha_trade_candidates) AS candidate_time,
                   (SELECT COUNT(*) FROM alpha_trade_candidates) AS candidate_count,
                   (SELECT MAX(cooldown_until) FROM alpha_cooldowns WHERE source = 'alpha') AS cooldown_until,
                   (SELECT COUNT(*) FROM alpha_cooldowns WHERE source = 'alpha') AS cooldown_count"""
        ).fetchone()
        return tuple(row) if row else None
    finally:
        conn.close()


def _slim_alpha_candidate(row):
    keys = (
        "id", "time", "alpha_symbol", "base_asset", "futures_symbol",
        "alpha_discovery_score", "alpha_profile", "normal_score", "normal_grade",
        "normal_side", "entry_status", "block_reason", "adapter_quality",
        "volume_price_state", "volume_price_action", "volume_price_reasons_json",
        "volume_price_max_position_factor", "updated_at",
    )
    return {k: row.get(k) for k in keys if k in row}


@app.get("/api/alpha/scan/latest")
async def get_latest_alpha_scan(user=Depends(get_user)):
    version = await asyncio.to_thread(_alpha_dashboard_version)
    cached = versioned_response_get("alpha_scan_latest", version)
    if cached is not None:
        return cached
    scan, rows = fetch_latest_alpha_scan()
    if not scan:
        return versioned_response_set("alpha_scan_latest", version, {"scan_time": None, "count": 0, "symbols": []})
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
        market_phase = raw.get("market_phase") or {}
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
            "alpha_volume_growth_6h": (raw.get("volume") or {}).get("alpha_volume_growth_6h"),
            "futures_volume_growth_6h": (raw.get("futures_sync") or {}).get("futures_volume_growth_6h"),
            "futures_oi_change_4h": (raw.get("futures_sync") or {}).get("oi_change_4h"),
            "futures_oi_change_24h": (raw.get("futures_sync") or {}).get("oi_change_24h"),
            "alpha_trend": raw.get("alpha_trend") or {},
            "volume_price": volume_price,
            "market_phase": market_phase,
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
    return versioned_response_set(
        "alpha_scan_latest",
        version,
        {"scan_time": scan["time"], "count": len(symbols), "symbols": symbols, "cooldowns": cooldowns},
    )


@app.get("/api/alpha/scan/by_symbol/{alpha_symbol}")
async def get_alpha_symbol_detail(alpha_symbol: str, user=Depends(get_user)):
    row = fetch_alpha_symbol_detail(alpha_symbol.upper())
    if not row:
        return {"error": "Not found", "symbol": alpha_symbol}
    raw = _parse_json(row["raw_features"])
    symbol_raw = _parse_json(row["symbol_raw_json"])
    candidate = fetch_latest_alpha_trade_candidate(row["alpha_symbol"])
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
        "market_phase": raw.get("market_phase") or {},
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


async def get_alpha_trade_candidates(user=Depends(get_user)):
    version = await asyncio.to_thread(_alpha_dashboard_version)
    cached = versioned_response_get("alpha_trade_candidates", version)
    if cached is not None:
        return cached
    return versioned_response_set("alpha_trade_candidates", version, {
        "candidates": [_slim_alpha_candidate(c) for c in fetch_latest_alpha_trade_candidates(200)],
        "cooldowns": fetch_active_alpha_cooldowns(100),
    })


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
    market_phase = features.get("market_phase") or {}

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
        "market_phase": market_phase,
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
    cached = cache_get("policy_loop_summary", 30)
    if cached is not None:
        return cached
    try:
        data = fetch_policy_loop_summary()
        overview = data.get("overview") or {}
        return cache_set("policy_loop_summary", {
            "mode": "policy_loop",
            "latest_run": data.get("latest_review_time"),
            "generated_at": data.get("generated_at"),
            "overview": overview,
            "candidates": data.get("candidates", []),
            "versions": data.get("versions", []),
            "auto_policy_status": data.get("auto_policy_status", {}),
            "entry_reviews": data.get("entry_reviews", [])[:100],
            "entry_summaries": data.get("entry_summaries", [])[:100],
            "entry_review_status": data.get("entry_review_status", {}),
            "exit_reviews": data.get("exit_reviews", [])[:100],
            "exit_summaries": data.get("exit_summaries", [])[:100],
            "trade_reviews": data.get("trade_reviews", [])[:100],
            "trade_review_summaries": data.get("trade_review_summaries", [])[:100],
            "entry_policy": data.get("entry_policy", {}),
            "exit_policy": data.get("exit_policy", {}),
            "grades": [],
            "decision_summary": {
                "total": int(overview.get("samples") or 0),
                "latest_run_id": data.get("latest_review_time"),
                "latest_time": data.get("generated_at"),
                "stage_counts": [],
                "result_counts": [],
                "top_filter_reasons": [],
                "recent": [],
            },
            "outcome_summary": overview,
            "backtest_status": {
                "plain": "策略闭环按完整仓位统一复盘开仓条件、平仓触发和后续走势；动作流水作为按仓位查询的后台证据。",
                "backtest_rows": 0,
                "review_rows": 0,
                "factor_rows": 0,
            },
        })
    except Exception as e:
        return {"mode": "policy_loop", "error": str(e), "overview": {}, "actions": []}


@app.get("/api/factor/performance")
async def get_factor_performance(factor: str = None, user=Depends(get_user)):
    """Policy-loop diagnostics kept on the old path for compatibility."""
    try:
        payload = fetch_policy_loop_summary(limit=200, include_diagnostics=True).get("reviews", [])
        if factor:
            payload = [r for r in payload if r.get("target_name") == factor or r.get("target_type") == factor]
        return {"rows": payload, "count": len(payload)}
    except Exception as e:
        return {"error": str(e), "rows": []}


@app.get("/api/backtest/recent")
async def get_recent_signals(grade: str = "S1", limit: int = 50, user=Depends(get_user)):
    data = fetch_policy_loop_summary(limit=limit)
    return data.get("actions", [])[:limit]


@app.get("/api/backtest/signals")
async def get_backtest_signals(grade: str = "all", limit: int = 200, user=Depends(get_user)):
    data = fetch_policy_loop_summary(limit=limit)
    return data.get("actions", [])[:limit]
# ---- 瀹炵洏浜ゆ槗 API ----


@app.get("/api/backtest/factor_analysis")
async def get_factor_analysis(user=Depends(get_user)):
    """Return policy-loop factor/category diagnostics."""
    try:
        data = fetch_policy_loop_summary(include_diagnostics=True)
        return {
            "mode": "policy_loop",
            "run_time": data.get("latest_review_time"),
            "total_signals": (data.get("overview") or {}).get("samples", 0),
            "category_stats": data.get("categories", []),
            "current_factors": data.get("reviews", []),
            "candidate_recommendations": data.get("candidates", []),
            "overall_discrimination": 0,
        }
    except Exception as e:
        return {"error": str(e), "recommendations": [], "candidate_recommendations": []}


@app.get("/api/backtest/review")
async def get_backtest_review(user=Depends(get_user)):
    """Return latest policy-loop review."""
    try:
        data = fetch_policy_loop_summary(include_diagnostics=True)
        return {
            "mode": "policy_loop",
            "_run_time": data.get("latest_review_time"),
            "summary": {"overview": data.get("overview", {})},
            "reviews": data.get("reviews", []),
            "entry_issues": [r for r in data.get("reviews", []) if r.get("target_type") == "entry_filter" and (r.get("bad_block_count") or 0) > 0],
            "exit_issues": [r for r in data.get("reviews", []) if r.get("target_type") == "exit" and (r.get("early_exit_count") or 0) > 0],
            "good_exits": [r for r in data.get("reviews", []) if r.get("target_type") == "exit" and (r.get("early_exit_count") or 0) == 0],
            "rules": [
                {"section": "闭环目标", "text": "优先提高收益率、减少错过大波段、减少频繁小盈利平仓；胜率只作为辅助指标。"},
                {"section": "自动生效", "text": "满足样本和收益改善条件的策略会自动 active，并写入运行时策略文件。"},
                {"section": "回滚保护", "text": "新策略如果导致收益转差、过早平仓或误拦截升高，会被 policy guard 自动回滚。"},
            ],
            "candidates": data.get("candidates", []),
            "versions": data.get("versions", []),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/policy/review/run")
async def run_policy_review_now(user=Depends(get_user)):
    try:
        result = generate_and_activate_policies()
        entry_review = review_position_trade_entries(limit=1000)
        entry_summary = summarize_entry_reviews(recent_limit=30)
        _api_cache.pop("policy_loop_summary", None)
        return {"status": "ok", **result, "entry_review": entry_review, "entry_summary": entry_summary}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/policy/exit-review/run")
async def run_exit_review_now(user=Depends(get_user)):
    try:
        reviewed = review_position_trade_exits(limit=1000)
        summary = summarize_exit_reviews(window_days=7, recent_limit=30)
        _api_cache.pop("policy_loop_summary", None)
        return {"status": "ok", "reviewed": reviewed, "summary": summary}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/backtest/exit-reviews")
async def get_exit_reviews(limit: int = 100, user=Depends(get_user)):
    try:
        return {"reviews": fetch_exit_reviews(limit=limit)}
    except Exception as e:
        return {"error": str(e), "reviews": []}


@app.get("/api/backtest/exit-review-summary")
async def get_exit_review_summary(limit: int = 100, user=Depends(get_user)):
    try:
        return {"summaries": fetch_exit_review_summaries(limit=limit)}
    except Exception as e:
        return {"error": str(e), "summaries": []}


@app.post("/api/policy/outcomes/label")
async def label_policy_outcomes_now(user=Depends(get_user)):
    try:
        count = label_decision_outcomes(limit=5000)
        return {"status": "ok", "updated": count}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/policy/actions")
async def get_policy_actions(limit: int = 200, user=Depends(get_user)):
    try:
        return {"actions": fetch_policy_loop_summary(limit=limit).get("actions", [])}
    except Exception as e:
        return {"error": str(e), "actions": []}


@app.get("/api/policy-loop/positions/{position_trade_id}/actions")
async def get_position_actions(position_trade_id: str, limit: int = 100, user=Depends(get_user)):
    try:
        return {"position_trade_id": position_trade_id, "actions": fetch_position_action_evidence(position_trade_id, limit=min(limit, 200))}
    except Exception as e:
        return {"error": str(e), "position_trade_id": position_trade_id, "actions": []}


@app.get("/api/policy/versions")
async def get_policy_versions(user=Depends(get_user)):
    try:
        data = fetch_policy_loop_summary()
        return {"versions": data.get("versions", []), "entry_policy": data.get("entry_policy", {}), "exit_policy": data.get("exit_policy", {})}
    except Exception as e:
        return {"error": str(e), "versions": []}


@app.post("/api/policy/legacy/clear")
async def clear_legacy_backtest(vacuum: bool = False, user=Depends(get_user)):
    try:
        return {"status": "ok", **clear_legacy_backtest_data(vacuum=vacuum)}
    except Exception as e:
        return {"error": str(e)}


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
    return generate_and_activate_policies()


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


def _safe_trading_runtime_controls():
    try:
        return get_trading_runtime_controls()
    except Exception as e:
        return {
            "normal_trading_enabled": True,
            "alpha_trading_enabled": False,
            "updated_at": {},
            "warning": str(e),
        }


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
    from shared.db import fetch_position_trade_groups, get_conn, rebuild_position_trades_from_income
    from trader.config import TRADING_CONFIG

    controls = _safe_trading_runtime_controls()
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
        recent_trades = [
            r for r in fetch_position_trade_groups(150)
            if r.get("symbol") != "ACCOUNT"
        ][:100]
        grouped_stats = _grouped_trade_stats(fetch_position_trade_groups(10000))
        total_trades = grouped_stats["total_trades"]
        total_pnl = grouped_stats["total_pnl"]
        total_closed = grouped_stats["total_closed"]
        win_count = grouped_stats["win_count"]
        loss_count = grouped_stats["loss_count"]
        win_rate = grouped_stats["win_rate"]
        profit_ratio = grouped_stats["profit_ratio"]
        reason_stats = grouped_stats["reason_stats"]
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
            "reason_stats": reason_stats,
            "latest_position_snapshot": latest_snapshot,
            "trading_controls": controls,
        }
    finally:
        conn.close()


def _grouped_trade_stats(grouped_trades):
    rows = [r for r in (grouped_trades or []) if r.get("exit_time")]
    position_rows = [
        r for r in rows
        if not r.get("is_adjustment") and r.get("symbol") != "ACCOUNT"
    ]
    total_pnl = sum(float(r.get("pnl") or 0) for r in rows)
    adjustment_pnl = sum(float(r.get("pnl") or 0) for r in rows if r not in position_rows)
    position_pnl = sum(float(r.get("pnl") or 0) for r in position_rows)
    wins = [r for r in position_rows if float(r.get("pnl") or 0) > 0]
    losses = [r for r in position_rows if float(r.get("pnl") or 0) <= 0]
    total_win = sum(float(r.get("pnl") or 0) for r in wins)
    total_loss = abs(sum(float(r.get("pnl") or 0) for r in losses))
    reason_stats = {}
    for r in position_rows:
        reason = r.get("exit_reason") or "unknown"
        reason_stats.setdefault(reason, {"count": 0, "total_pnl": 0.0, "wins": 0})
        reason_stats[reason]["count"] += 1
        reason_stats[reason]["total_pnl"] += float(r.get("pnl") or 0)
        if float(r.get("pnl") or 0) > 0:
            reason_stats[reason]["wins"] += 1
    return {
        "total_trades": len(position_rows),
        "total_closed": len(position_rows),
        "total_pnl": total_pnl,
        "position_pnl": position_pnl,
        "adjustment_pnl": adjustment_pnl,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(position_rows) * 100, 2) if position_rows else 0,
        "profit_ratio": round(total_win / total_loss, 2) if total_loss else 0,
        "reason_stats": {
            k: {
                "count": v["count"],
                "total_pnl": round(v["total_pnl"], 2),
                "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0,
            }
            for k, v in sorted(reason_stats.items(), key=lambda x: -x[1]["count"])
        },
    }


@app.get("/api/trading/statu")
@app.get("/api/trading/status")
async def get_trading_status(user=Depends(get_user)):
    """Merge live status and stats with cache."""
    import time
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from shared.db import fetch_position_trade_groups, get_conn, rebuild_position_trades_from_income

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
            "trading_controls": _safe_trading_runtime_controls(),
        }

    EXCLUDED_REASONS = ("historical_import", "鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)")

    from trader.exchange import BinanceFutures
    from trader.config import TRADING_CONFIG
    INITIAL_CAPITAL = TRADING_CONFIG.get("total_capital", 5000)
    controls = _safe_trading_runtime_controls()
    ex = BinanceFutures()
    try:
        margin_data = ex.get_margin_balance()
        balance = margin_data["totalWalletBalance"]
        margin_balance = margin_data.get("totalMarginBalance") or balance
        total_maint_margin = float(margin_data.get("totalMaintMargin") or 0)
        cross_margin_ratio = (total_maint_margin / margin_balance * 100) if margin_balance else None
        positions = ex.get_positions()
        unrealized_total = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
        rebuild_position_trades_from_income(
            account_pnl=(float(balance) - float(INITIAL_CAPITAL)) if isinstance(balance, (int, float)) else None,
            unrealized_pnl=unrealized_total,
        )
        conn = get_conn()
        recent_trades = [
            r for r in fetch_position_trade_groups(150)
            if r.get("symbol") != "ACCOUNT"
        ][:100]
        grouped_stats = _grouped_trade_stats(fetch_position_trade_groups(10000))
        total_trades = grouped_stats["total_trades"]
        total_closed = grouped_stats["total_closed"]
        total_pnl = grouped_stats["total_pnl"]
        position_pnl = grouped_stats.get("position_pnl", total_pnl)
        adjustment_pnl = grouped_stats.get("adjustment_pnl", 0)
        win_count = grouped_stats["win_count"]
        loss_count = grouped_stats["loss_count"]
        win_rate = grouped_stats["win_rate"]
        profit_ratio = grouped_stats["profit_ratio"]
        reason_stats = grouped_stats["reason_stats"]

        def parse_position_time(value):
            if not value:
                return None
            try:
                from datetime import datetime, timezone

                text = str(value).strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None

        def format_holding_time(seconds):
            if seconds is None:
                return "-"
            seconds = max(0, int(seconds))
            days, rem = divmod(seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, secs = divmod(rem, 60)
            if days:
                return f"{days}天{hours}小时"
            if hours:
                return f"{hours}小时{minutes}分钟"
            if minutes:
                return f"{minutes}分钟"
            return f"{secs}秒"

        def holding_fields(entry_time):
            dt = parse_position_time(entry_time)
            if not dt:
                return {
                    "entry_time": entry_time,
                    "holding_seconds": None,
                    "holding_time": "-",
                }
            from datetime import datetime, timezone

            seconds = (datetime.now(timezone.utc) - dt).total_seconds()
            return {
                "entry_time": entry_time,
                "holding_seconds": max(0, int(seconds)),
                "holding_time": format_holding_time(seconds),
            }

        def entry_time_from_position_id(position_id):
            try:
                import re

                match = re.search(r"(\d{8}T\d{6}Z)", str(position_id or ""))
                if not match:
                    return None
                raw = match.group(1)
                return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"
            except Exception:
                return None

        def fallback_entry_time(symbol, side):
            if side not in ("LONG", "SHORT"):
                return None
            open_side = "BUY" if side == "LONG" else "SELL"
            row = conn.execute(
                """SELECT created_at FROM orders
                   WHERE symbol=? AND side=? AND order_type='MARKET'
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (symbol, open_side),
            ).fetchone()
            return row["created_at"] if row else None

        def position_management_fields(symbol, side=None):
            r = conn.execute("SELECT * FROM position_history WHERE symbol=?", (symbol,)).fetchone()
            if not r:
                return {
                    **holding_fields(fallback_entry_time(symbol, side)),
                    "entry_reason": None,
                    "entry_score": 0,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "highest_price": None,
                    "lowest_price": None,
                    "stop_model": None,
                    "initial_stop_loss": None,
                    "stop_pct": None,
                    "current_stop_loss": None,
                    "trailing_stop_price": None,
                    "trailing_enabled": False,
                    "trailing_atr_multiplier": None,
                    "r_multiple": 0,
                    "initial_quantity": None,
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
                    "roll_status": "state_incomplete",
                    "roll_price": None,
                    "protected_stop": None,
                    "last_roll_time": None,
                    "protected_profit": 0,
                    "max_floating_pnl": 0,
                    "roll_enabled": False,
                    "roll_block_reason": None,
                    "alpha_current_score": None,
                    "alpha_volume_price_state": None,
                    "alpha_volume_price_action": None,
                    "alpha_volume_price_reason": None,
                }
            alpha_context = {}
            if "strategy_source" in r.keys() and r["strategy_source"] == "alpha":
                try:
                    from shared.db import fetch_latest_alpha_position_context

                    alpha_context = fetch_latest_alpha_position_context(
                        symbol=symbol,
                        alpha_symbol=r["alpha_symbol"] if "alpha_symbol" in r.keys() else None,
                    ) or {}
                except Exception:
                    alpha_context = {}
            alpha_reasons = _parse_json(alpha_context.get("volume_price_reasons_json"), [])
            entry_time = r["entry_time"] if "entry_time" in r.keys() else None
            if not entry_time and "position_id" in r.keys():
                entry_time = entry_time_from_position_id(r["position_id"])
            if not entry_time:
                entry_time = fallback_entry_time(symbol, side)
            initial_quantity = float(r["initial_quantity"] or 0) if "initial_quantity" in r.keys() else 0
            initial_stop = float(r["initial_stop_loss"] or 0) if "initial_stop_loss" in r.keys() else 0
            atr_value = float(r["atr_value"] or 0) if "atr_value" in r.keys() else 0
            roll_layer = int(r["roll_layer"] or 0) if "roll_layer" in r.keys() else 0
            protected_stop = float(r["protected_stop"] or 0) if "protected_stop" in r.keys() else 0
            roll_block_reason = r["roll_block_reason"] if "roll_block_reason" in r.keys() else None
            if not initial_quantity or not initial_stop or not atr_value:
                roll_status = "state_incomplete"
            elif roll_layer >= 1:
                roll_status = "rolled_protected" if protected_stop else "protection_missing"
            elif roll_block_reason:
                roll_status = roll_block_reason
            elif not bool(r["tp1_hit"] if "tp1_hit" in r.keys() else 0):
                roll_status = "waiting_tp1"
            else:
                roll_status = "waiting_1_5r"
            return {
                **holding_fields(entry_time),
                "entry_reason": r["entry_reason"],
                "entry_score": float(r["entry_score"] or 0),
                "tp1_hit": bool(r["tp1_hit"]) if "tp1_hit" in r.keys() else False,
                "tp2_hit": bool(r["tp2_hit"]) if "tp2_hit" in r.keys() else False,
                "highest_price": float(r["highest_price"] or 0) if "highest_price" in r.keys() else None,
                "lowest_price": float(r["lowest_price"] or 0) if "lowest_price" in r.keys() else None,
                "stop_model": r["stop_model"] if "stop_model" in r.keys() else None,
                "initial_stop_loss": float(r["initial_stop_loss"] or 0) if "initial_stop_loss" in r.keys() else None,
                "stop_pct": float(r["stop_pct"] or 0) if "stop_pct" in r.keys() else None,
                "current_stop_loss": float(r["current_stop_loss"] or 0) if "current_stop_loss" in r.keys() else None,
                "trailing_stop_price": float(r["trailing_stop_price"] or 0) if "trailing_stop_price" in r.keys() else None,
                "trailing_enabled": bool(r["trailing_enabled"]) if "trailing_enabled" in r.keys() else False,
                "trailing_atr_multiplier": float(r["trailing_atr_multiplier"] or 0) if "trailing_atr_multiplier" in r.keys() else None,
                "r_multiple": float(r["r_multiple"] or 0) if "r_multiple" in r.keys() else 0,
                "initial_quantity": initial_quantity or None,
                "last_exit_reason": r["last_exit_reason"] if "last_exit_reason" in r.keys() else None,
                "last_exit_plain": plain_reason(r["last_exit_reason"]) if "last_exit_reason" in r.keys() else None,
                "strategy_source": r["strategy_source"] if "strategy_source" in r.keys() else "normal",
                "signal_source": r["signal_source"] if "signal_source" in r.keys() else None,
                "alpha_symbol": r["alpha_symbol"] if "alpha_symbol" in r.keys() else None,
                "alpha_profile": r["alpha_profile"] if "alpha_profile" in r.keys() else None,
                "alpha_entry_level": r["alpha_entry_level"] if "alpha_entry_level" in r.keys() else None,
                "alpha_score": float(r["alpha_score"] or 0) if "alpha_score" in r.keys() else None,
                "alpha_suggested_position_pct": float(r["alpha_suggested_position_pct"] or 0) if "alpha_suggested_position_pct" in r.keys() else None,
                "roll_layer": roll_layer,
                "roll_status": roll_status,
                "roll_price": float(r["roll_price"] or 0) if "roll_price" in r.keys() else None,
                "protected_stop": protected_stop or None,
                "last_roll_time": r["last_roll_time"] if "last_roll_time" in r.keys() else None,
                "protected_profit": float(r["protected_profit"] or 0) if "protected_profit" in r.keys() else 0,
                "max_floating_pnl": float(r["max_floating_pnl"] or 0) if "max_floating_pnl" in r.keys() else 0,
                "roll_enabled": bool(r["roll_enabled"]) if "roll_enabled" in r.keys() else False,
                "roll_block_reason": roll_block_reason,
                "alpha_current_score": float(alpha_context.get("alpha_score") or 0) if alpha_context else None,
                "alpha_volume_price_state": alpha_context.get("volume_price_state") if alpha_context else None,
                "alpha_volume_price_action": alpha_context.get("volume_price_action") if alpha_context else None,
                "alpha_volume_price_reason": alpha_reasons[0] if alpha_reasons else None,
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
                    "margin_ratio": (
                        round(cross_margin_ratio, 4)
                        if str(p.get("margin_type") or "").lower() in {"cross", "crossed"} and cross_margin_ratio is not None
                        else round(
                            (p.get("maint_margin") or 0)
                            / ((p.get("isolated_margin") or 0) + (p.get("unrealized_pnl") or 0))
                            * 100,
                            4,
                        )
                        if ((p.get("isolated_margin") or 0) + (p.get("unrealized_pnl") or 0)) > 0
                        else None
                    ),
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
                    **position_management_fields(p["symbol"], p.get("side")),
                }
                for p in positions
            ],
            "recent_trades": recent_trades,
            "account_margin_balance": round(margin_balance, 2),
            # stats 瀛楁
            "total_pnl": round(balance - INITIAL_CAPITAL, 2) if isinstance(balance, (int, float)) else 0,
            "trades_pnl": round(total_pnl, 2),
            "realized_pnl": round(total_pnl, 2),
            "position_pnl": round(position_pnl, 2),
            "adjustment_pnl": round(adjustment_pnl, 2),
            "income_pnl": round(conn.execute("SELECT COALESCE(SUM(income),0) FROM exchange_income_ledger").fetchone()[0] or 0, 2),
            "reconcile_diff": round((balance - INITIAL_CAPITAL) - total_pnl - unrealized_total, 2) if isinstance(balance, (int, float)) else 0,
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
                    "win_rate": v.get("win_rate", 0),
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
        stale = _trading_status_cache.get("data")
        if stale:
            fallback = dict(stale)
            fallback["stale"] = True
            fallback["binance_warning"] = f"Binance 查询暂时超时，当前展示最近成功数据: {e}"
            fallback["stale_age_seconds"] = round(max(0, time.time() - _trading_status_cache.get("time", 0)), 1)
            return fallback
        return {
            "error": str(e),
            "data_source": "binance_live",
            "balance": 0,
            "positions": [],
            "recent_trades": [],
            "total_pnl": 0,
            "trading_controls": _safe_trading_runtime_controls(),
        }
    finally:
        ex.close()


def _live_holding_fields(entry_time):
    from datetime import datetime, timezone

    if not entry_time:
        return {"entry_time": None, "holding_seconds": None, "holding_time": "-"}
    try:
        text = str(entry_time).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        entered = datetime.fromisoformat(text)
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - entered.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return {"entry_time": entry_time, "holding_seconds": None, "holding_time": "-"}

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        label = f"{days}天{hours}小时"
    elif hours:
        label = f"{hours}小时{minutes}分钟"
    elif minutes:
        label = f"{minutes}分钟"
    else:
        label = f"{secs}秒"
    return {"entry_time": entry_time, "holding_seconds": seconds, "holding_time": label}


def _live_position_management_fields(state: dict | None) -> dict:
    if not state:
        return {
            "entry_reason": None, "entry_score": None,
            "tp1_hit": False, "tp2_hit": False,
            "highest_price": None, "lowest_price": None,
            "stop_model": None, "initial_stop_loss": None, "stop_pct": None,
            "current_stop_loss": None, "trailing_stop_price": None,
            "trailing_enabled": False, "trailing_atr_multiplier": None,
            "r_multiple": None, "initial_quantity": None,
            "last_exit_reason": None, "last_exit_plain": None,
            "strategy_source": "normal", "signal_source": None,
            "alpha_symbol": None, "alpha_profile": None,
            "alpha_entry_level": None, "alpha_score": None,
            "alpha_suggested_position_pct": None,
            "roll_layer": 0, "roll_status": "state_incomplete",
            "roll_price": None, "protected_stop": None,
            "last_roll_time": None, "protected_profit": 0,
            "max_floating_pnl": 0, "roll_enabled": False,
            "roll_block_reason": "state_incomplete",
            "alpha_current_score": None,
            "alpha_volume_price_state": None,
            "alpha_volume_price_action": None,
            "alpha_volume_price_reason": None,
        }

    def number(name, default=None):
        value = state.get(name)
        return float(value) if value is not None else default

    roll_block_reason = state.get("roll_block_reason")
    roll_layer = int(state.get("roll_layer") or 0)
    if roll_block_reason:
        roll_status = roll_block_reason
    elif roll_layer >= 1:
        roll_status = "rolled_protected" if number("protected_stop") else "protection_missing"
    elif not number("initial_quantity") or not number("initial_stop_loss") or not number("atr_value"):
        roll_status = "state_incomplete"
    elif not bool(state.get("tp1_hit")):
        roll_status = "waiting_tp1"
    else:
        roll_status = "waiting_1_5r"

    return {
        "entry_reason": state.get("entry_reason"),
        "entry_score": number("entry_score"),
        "tp1_hit": bool(state.get("tp1_hit")),
        "tp2_hit": bool(state.get("tp2_hit")),
        "highest_price": number("highest_price"),
        "lowest_price": number("lowest_price"),
        "stop_model": state.get("stop_model"),
        "initial_stop_loss": number("initial_stop_loss"),
        "stop_pct": number("stop_pct"),
        "current_stop_loss": number("current_stop_loss"),
        "trailing_stop_price": number("trailing_stop_price"),
        "trailing_enabled": bool(state.get("trailing_enabled")),
        "trailing_atr_multiplier": number("trailing_atr_multiplier"),
        "r_multiple": number("r_multiple"),
        "initial_quantity": number("initial_quantity"),
        "last_exit_reason": state.get("last_exit_reason"),
        "last_exit_plain": plain_reason(state.get("last_exit_reason")) if state.get("last_exit_reason") else None,
        "strategy_source": state.get("strategy_source") or "normal",
        "signal_source": state.get("signal_source"),
        "alpha_symbol": state.get("alpha_symbol"),
        "alpha_profile": state.get("alpha_profile"),
        "alpha_entry_level": state.get("alpha_entry_level"),
        "alpha_score": number("alpha_score"),
        "alpha_suggested_position_pct": number("alpha_suggested_position_pct"),
        "roll_layer": roll_layer,
        "roll_status": roll_status,
        "roll_price": number("roll_price"),
        "protected_stop": number("protected_stop"),
        "last_roll_time": state.get("last_roll_time"),
        "protected_profit": number("protected_profit", 0),
        "max_floating_pnl": number("max_floating_pnl", 0),
        "roll_enabled": bool(state.get("roll_enabled")),
        "roll_block_reason": roll_block_reason,
        "alpha_current_score": None,
        "alpha_volume_price_state": None,
        "alpha_volume_price_action": None,
        "alpha_volume_price_reason": None,
    }


def _account_decision_panel(conn, account_id: int) -> dict:
    panel = {
        "latest_run_id": None, "latest_time": None,
        "last_execution_time": None, "top_reasons": [], "recent": [],
        "entry_gate_mode": "per_symbol_entry_profile",
        "entry_gate_plain": "开仓线按币种模板判断；全局60分不提前拦截试探仓。",
        "regime_effect_plain": "行情状态只调整开仓名额和仓位，不直接抬高综合分门槛。",
    }
    try:
        from shared.strategy_learning import load_entry_policy
        policy = load_entry_policy()
        panel["active_entry_policy_count"] = len(policy.get("rules") or [])
        panel["active_entry_policy_version"] = policy.get("version")
    except Exception:
        panel["active_entry_policy_count"] = 0
        panel["active_entry_policy_version"] = None

    latest = conn.execute(
        """SELECT run_id, time FROM strategy_decisions
           WHERE account_id=? ORDER BY datetime(time) DESC, id DESC LIMIT 1""",
        (account_id,),
    ).fetchone()
    if not latest:
        return panel
    panel["latest_run_id"] = latest["run_id"]
    panel["latest_time"] = latest["time"]
    panel["last_execution_time"] = latest["time"]
    panel["top_reasons"] = [
        {"reason": row["reason"], "plain": plain_reason(row["reason"]), "count": row["count"]}
        for row in conn.execute(
            """SELECT filter_reason AS reason, COUNT(*) AS count
               FROM strategy_decisions
               WHERE account_id=? AND run_id=?
                 AND filter_reason IS NOT NULL AND filter_reason!=''
               GROUP BY filter_reason ORDER BY count DESC LIMIT 6""",
            (account_id, latest["run_id"]),
        ).fetchall()
    ]
    panel["recent"] = [
        {
            "time": row["time"], "symbol": row["symbol"], "side": row["side"],
            "stage": row["decision_stage"], "result": row["decision_result"],
            "score": float(row["composite_score"] or 0),
            "reason": row["filter_reason"], "plain": plain_reason(row["filter_reason"]),
        }
        for row in conn.execute(
            """SELECT time, symbol, side, decision_stage, decision_result,
                      filter_reason, composite_score
               FROM strategy_decisions
               WHERE account_id=? AND run_id=? ORDER BY id DESC LIMIT 10""",
            (account_id, latest["run_id"]),
        ).fetchall()
    ]
    return panel


def _account_status_payload(account: dict) -> dict:
    from shared.accounts import account_exchange_config
    from shared.db import fetch_position_trade_groups, get_conn
    from trader.exchange import BinanceFutures

    ex = BinanceFutures(
        config=account_exchange_config(account),
        account_id=account["id"],
        account_name=account["name"],
    )
    try:
        margin = ex.get_margin_balance()
        positions = ex.get_positions()
        wallet = float(margin.get("totalWalletBalance") or 0)
        equity = float(margin.get("totalMarginBalance") or wallet)
        total_maint_margin = float(margin.get("totalMaintMargin") or 0)
        cross_margin_ratio = (total_maint_margin / equity * 100) if equity > 0 else None
        conn = get_conn()
        try:
            adjustments = float(conn.execute(
                """SELECT COALESCE(SUM(CASE
                       WHEN adjustment_type IN ('deposit','transfer_in') THEN amount
                       WHEN adjustment_type IN ('withdraw','transfer_out') THEN -amount
                       ELSE amount END), 0)
                   FROM account_capital_adjustments WHERE account_id=?""",
                (account["id"],),
            ).fetchone()[0] or 0)
            history = fetch_position_trade_groups(100, account_id=account["id"])
            position_states = {
                row["symbol"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM account_position_history WHERE account_id=?",
                    (account["id"],),
                ).fetchall()
            }
            latest_open_orders = {
                row["symbol"]: row["created_at"]
                for row in conn.execute(
                    """SELECT symbol, MAX(created_at) AS created_at
                       FROM orders
                       WHERE account_id=? AND order_type='MARKET'
                       GROUP BY symbol""",
                    (account["id"],),
                ).fetchall()
            }
            decision_panel = _account_decision_panel(conn, account["id"])
            latest_position_actions = {
                row["symbol"]: row["filter_reason"] or row["decision_result"]
                for row in conn.execute(
                    """SELECT d.symbol, d.filter_reason, d.decision_result
                       FROM strategy_decisions d
                       JOIN (
                         SELECT symbol, MAX(id) AS latest_id
                         FROM strategy_decisions
                         WHERE account_id=?
                           AND decision_stage IN ('position_management','roll_position','execution')
                         GROUP BY symbol
                       ) latest ON latest.latest_id=d.id""",
                    (account["id"],),
                ).fetchall()
            }
            latest_market_phase = {}
            for row in conn.execute(
                """SELECT s.symbol, s.raw_features
                   FROM alpha_scores s
                   JOIN (
                     SELECT symbol, MAX(time) AS max_time
                     FROM alpha_scores GROUP BY symbol
                   ) latest ON latest.symbol=s.symbol AND latest.max_time=s.time"""
            ).fetchall():
                raw = _parse_json(row["raw_features"], {})
                latest_market_phase[row["symbol"]] = raw.get("market_phase") or {}
            for row in conn.execute(
                """SELECT s.alpha_symbol, s.futures_symbol, s.raw_features
                   FROM alpha_scan_scores s
                   JOIN (
                     SELECT alpha_symbol, MAX(time) AS max_time
                     FROM alpha_scan_scores GROUP BY alpha_symbol
                   ) latest ON latest.alpha_symbol=s.alpha_symbol AND latest.max_time=s.time"""
            ).fetchall():
                raw = _parse_json(row["raw_features"], {})
                phase = raw.get("market_phase") or {}
                if row["futures_symbol"]:
                    latest_market_phase[row["futures_symbol"]] = phase
                latest_market_phase[row["alpha_symbol"]] = phase
        finally:
            conn.close()
        initial = float(account.get("initial_capital") or 0)
        total_pnl = equity - initial - adjustments
        base = initial + adjustments
        trade_rows = [row for row in history if row.get("symbol") != "ACCOUNT"]
        win_count = sum(1 for row in trade_rows if float(row.get("pnl") or row.get("net_pnl") or 0) > 0)
        loss_count = sum(1 for row in trade_rows if float(row.get("pnl") or row.get("net_pnl") or 0) < 0)
        for position in positions:
            position["account_id"] = account["id"]
            position["account_name"] = account["name"]
            state = position_states.get(position.get("symbol")) or {}
            entry_time = state.get("entry_time") or latest_open_orders.get(position.get("symbol"))
            position.update(_live_holding_fields(entry_time))
            position.update(_live_position_management_fields(state))
            position["market_phase"] = (
                latest_market_phase.get(position.get("symbol"))
                or latest_market_phase.get(state.get("alpha_symbol"))
                or {}
            )
            position["last_system_action"] = latest_position_actions.get(position.get("symbol"))
            position["invested"] = round(
                float(position.get("notional") or 0)
                or abs(float(position.get("entry_price") or 0) * float(position.get("quantity") or 0)),
                2,
            )
            if state.get("strategy_source") == "alpha":
                from shared.db import fetch_latest_alpha_position_context
                alpha_context = fetch_latest_alpha_position_context(
                    symbol=position.get("symbol"),
                    alpha_symbol=state.get("alpha_symbol"),
                ) or {}
                reasons = _parse_json(alpha_context.get("volume_price_reasons_json"), [])
                position.update({
                    "alpha_current_score": float(alpha_context.get("alpha_score") or 0) if alpha_context else None,
                    "alpha_volume_price_state": alpha_context.get("volume_price_state"),
                    "alpha_volume_price_action": alpha_context.get("volume_price_action"),
                    "alpha_volume_price_reason": reasons[0] if reasons else None,
                })
            entry_price = float(position.get("entry_price") or 0)
            quantity = float(position.get("quantity") or 0)
            tracked_price = (
                float(position.get("highest_price") or entry_price)
                if position.get("side") == "LONG"
                else float(position.get("lowest_price") or entry_price)
            )
            tracked_pnl = (
                (tracked_price - entry_price) * quantity
                if position.get("side") == "LONG"
                else (entry_price - tracked_price) * quantity
            )
            position["max_floating_pnl"] = round(
                max(float(position.get("max_floating_pnl") or 0), tracked_pnl, 0),
                2,
            )
            position_margin = float(position.get("margin") or position.get("position_initial_margin") or 0)
            position["pnl_pct"] = round(float(position.get("unrealized_pnl") or 0) / position_margin * 100, 2) if position_margin > 0 else None
            if str(position.get("margin_type") or "").lower() in {"cross", "crossed"}:
                position["margin_ratio"] = round(cross_margin_ratio, 4) if cross_margin_ratio is not None else None
            else:
                isolated_balance = float(position.get("isolated_margin") or 0) + float(position.get("unrealized_pnl") or 0)
                position["margin_ratio"] = round(float(position.get("maint_margin") or 0) / isolated_balance * 100, 4) if isolated_balance > 0 else None
        return {
            "account_id": account["id"], "account_name": account["name"],
            "environment": account["environment"], "status": "ok", "stale": False,
            "initial_capital": initial, "net_capital_adjustments": adjustments,
            "wallet_balance": wallet, "equity": equity,
            "available_balance": float(margin.get("availableBalance") or 0),
            "unrealized_pnl": float(margin.get("totalUnrealizedProfit") or 0),
            "total_pnl": total_pnl, "return_pct": (total_pnl / base * 100) if base else 0,
            "max_positions": int(account.get("max_positions") or 5),
            "position_count": len(positions), "positions": positions, "recent_trades": history,
            "decision_panel": decision_panel,
            "stats": {
                "total_opens": len(trade_rows) + len(positions),
                "total_closed": len(trade_rows),
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": (win_count / len(trade_rows) * 100) if trade_rows else 0,
            },
            "normal_trading_enabled": bool(account.get("normal_trading_enabled")),
            "alpha_trading_enabled": bool(account.get("alpha_trading_enabled")),
            "auto_trading_enabled": bool(account.get("auto_trading_enabled")),
        }
    except Exception as exc:
        return {
            "account_id": account["id"], "account_name": account["name"],
            "environment": account["environment"], "status": "degraded",
            "error": str(exc), "positions": [], "recent_trades": [],
            "initial_capital": float(account.get("initial_capital") or 0),
            "max_positions": int(account.get("max_positions") or 5),
        }
    finally:
        ex.close()


@app.get("/api/trading/accounts")
async def get_trading_accounts(user=Depends(get_user)):
    from shared.accounts import list_accounts
    return {"accounts": list_accounts()}


@app.post("/api/trading/accounts")
async def create_trading_account(body: dict, user=Depends(get_user)):
    from shared.accounts import save_account
    try:
        account = save_account(body)
        _clear_trading_caches()
        return {"status": "ok", "account": account}
    except Exception as exc:
        return {"error": str(exc)}


@app.patch("/api/trading/accounts/{account_id}")
async def update_trading_account(account_id: int, body: dict, user=Depends(get_user)):
    from shared.accounts import save_account
    try:
        account = save_account(body, account_id=account_id)
        _clear_trading_caches()
        return {"status": "ok", "account": account}
    except Exception as exc:
        return {"error": str(exc)}


@app.delete("/api/trading/accounts/{account_id}")
async def delete_trading_account(account_id: int, user=Depends(get_user)):
    from shared.accounts import delete_account
    try:
        delete_account(account_id)
        _clear_trading_caches()
        return {"status": "ok"}
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/trading/accounts/{account_id}/test")
async def test_trading_account(account_id: int, user=Depends(get_user)):
    from shared.accounts import get_account
    account = get_account(account_id, include_secrets=True)
    if not account:
        return {"error": "账户不存在"}
    result = await asyncio.to_thread(_account_status_payload, account)
    return result


@app.post("/api/trading/accounts/{account_id}/capital-adjustments")
async def add_account_capital_adjustment(account_id: int, body: dict, user=Depends(get_user)):
    from shared.db import get_conn
    adjustment_type = str(body.get("adjustment_type") or "correction")
    if adjustment_type not in {"deposit", "withdraw", "transfer_in", "transfer_out", "correction"}:
        return {"error": "无效的资金调整类型"}
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO account_capital_adjustments
               (account_id, adjustment_type, amount, effective_time, note)
               VALUES (?, ?, ?, COALESCE(?, datetime('now')), ?)""",
            (account_id, adjustment_type, float(body.get("amount") or 0), body.get("effective_time"), body.get("note")),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/trading/accounts/status")
async def get_all_trading_account_status(user=Depends(get_user)):
    from shared.accounts import list_accounts
    accounts = list_accounts(include_secrets=True, enabled_only=True)
    results = await asyncio.gather(*[asyncio.to_thread(_account_status_payload, account) for account in accounts])
    healthy = [row for row in results if row.get("status") == "ok"]
    environments = {row.get("environment") for row in healthy}
    if len(environments) > 1:
        environment_status = "MIXED"
    elif environments == {"prod"}:
        environment_status = "PROD LIVE"
    elif environments == {"testnet"}:
        environment_status = "TESTNET LIVE"
    else:
        environment_status = "LIVE DEGRADED"
    return {
        "accounts": results,
        "environment_status": environment_status,
        "summary": {
            "initial_capital": sum(float(r.get("initial_capital") or 0) for r in healthy),
            "equity": sum(float(r.get("equity") or 0) for r in healthy),
            "total_pnl": sum(float(r.get("total_pnl") or 0) for r in healthy),
            "unrealized_pnl": sum(float(r.get("unrealized_pnl") or 0) for r in healthy),
            "position_count": sum(len(r.get("positions") or []) for r in healthy),
        },
    }


@app.get("/api/trading/stats")
async def get_trading_stats(user=Depends(get_user)):
    from shared.db import fetch_position_trade_groups, get_conn, rebuild_position_trades_from_income
    conn = get_conn()
    try:
        grouped_stats = _grouped_trade_stats(fetch_position_trade_groups(10000))
        total_closed = grouped_stats["total_closed"]
        total_pnl = grouped_stats["total_pnl"]
        position_pnl = grouped_stats.get("position_pnl", total_pnl)
        adjustment_pnl = grouped_stats.get("adjustment_pnl", 0)
        win_count = grouped_stats["win_count"]
        loss_count = grouped_stats["loss_count"]
        win_rate = grouped_stats["win_rate"]
        profit_ratio = grouped_stats["profit_ratio"]
        
        # 褰撳墠鎸佷粨鏁?        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from trader.exchange import BinanceFutures
        ex = BinanceFutures()
        pos = ex.get_positions()
        current_pos = len(pos)
        ex.close()
        
        reason_stats = grouped_stats["reason_stats"]
        total_opens = grouped_stats["total_trades"]
        
        return {
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": round(total_pnl, 2),
            "position_pnl": round(position_pnl, 2),
            "adjustment_pnl": round(adjustment_pnl, 2),
            "current_positions": current_pos,
            "total_opens": total_opens,
            "total_closed": total_closed,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "profit_ratio": profit_ratio,
            "reason_stats": reason_stats,
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
        version_row = conn.execute(
            f"""SELECT MAX(id) AS latest_id, MAX(time) AS latest_time, COUNT(*) AS total
                FROM strategy_decisions
                {where_sql}""",
            params,
        ).fetchone()
        total = int(version_row["total"] or 0) if version_row else 0
        version = (
            page,
            limit,
            symbol.upper() if symbol else None,
            stage,
            result,
            version_row["latest_id"] if version_row else None,
            version_row["latest_time"] if version_row else None,
            total,
        )
        cached = versioned_response_get("strategy_decisions", version)
        if cached is not None:
            return cached
        rows = conn.execute(
            f"""SELECT id, time, run_id, symbol, scan_id, decision_stage, decision_result,
                       filter_reason, composite_score, side, price, created_at
                FROM strategy_decisions
                {where_sql}
                ORDER BY time DESC, id DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return versioned_response_set("strategy_decisions", version, {
            "total": total,
            "page": page,
            "limit": limit,
            "decisions": [dict(r) for r in rows],
        })
    except Exception as e:
        return {"error": str(e), "decisions": []}
    finally:
        conn.close()


