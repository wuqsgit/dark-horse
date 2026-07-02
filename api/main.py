"""AlphaDog API Server 鈥?FastAPI (SQLite)"""
import os, sys, json, time
from fastapi import FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import fetch_latest_scan, fetch_symbol_detail, fetch_score_history, fetch_backtest_summary, fetch_recent_signals, fetch_factor_performance, fetch_signal_outcome_summary, init_db

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
    "/api/scan/details",
    "/api/backtest/review",
    "/api/backtest/summary",
    "/api/backtest/factor_analysis",
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
        from shared.db import get_conn
        conn = get_conn()
        review = conn.execute("SELECT run_time, review_json FROM backtest_review ORDER BY run_time DESC LIMIT 1").fetchone()
        if review and review["review_json"]:
            data = json.loads(review["review_json"])
            data["_run_time"] = review["run_time"]
        else:
            data = {"error": "鏆傛棤澶嶇洏鏁版嵁锛岃杩愯鍥炴祴"}
        seed_response_cache("/api/backtest/review", data)
        factor = conn.execute("SELECT result FROM factor_analysis ORDER BY rowid DESC LIMIT 1").fetchone()
        seed_response_cache(
            "/api/backtest/factor_analysis",
            json.loads(factor["result"]) if factor and factor["result"] else {"error": "no analysis data", "recommendations": [], "candidate_recommendations": []},
        )
        conn.close()
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
    return cache_set("backtest_summary", {
        "latest_run": latest,
        "grades": results,
        "decision_summary": decision_summary,
        "outcome_summary": outcome_summary,
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
        return {"error": "no analysis data", "recommendations": [], "candidate_recommendations": []}
    except Exception as e:
        return {"error": str(e), "recommendations": [], "candidate_recommendations": []}
    finally:
        pass


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
            return data
        return {"error": "鏆傛棤澶嶇洏鏁版嵁锛岃杩愯鍥炴祴"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        pass


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
def _build_local_trading_status(error=None):
    from shared.db import fetch_position_trade_groups, get_conn
    from trader.config import TRADING_CONFIG

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
                "margin_ratio": round(100 / leverage, 2) if leverage else 0,
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
        }

    EXCLUDED_REASONS = ("historical_import", "鍘嗗彶琛ュ綍(鎵嬪姩骞充粨)")

    from trader.exchange import BinanceFutures
    from trader.config import TRADING_CONFIG
    INITIAL_CAPITAL = TRADING_CONFIG.get("total_capital", 5000)
    ex = BinanceFutures()
    try:
        margin_data = ex.get_margin_balance()
        balance = margin_data["totalWalletBalance"]
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
                }
            return {
                "entry_reason": r["entry_reason"],
                "entry_score": float(r["entry_score"] or 0),
                "tp1_hit": bool(r["tp1_hit"]) if "tp1_hit" in r.keys() else False,
                "tp2_hit": bool(r["tp2_hit"]) if "tp2_hit" in r.keys() else False,
                "highest_price": float(r["highest_price"] or 0) if "highest_price" in r.keys() else None,
                "last_exit_reason": r["last_exit_reason"] if "last_exit_reason" in r.keys() else None,
                "last_exit_plain": plain_reason(r["last_exit_reason"]) if "last_exit_reason" in r.keys() else None,
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
                    "margin": round(p["entry_price"] * p["quantity"] / p["leverage"], 2) if p["entry_price"] and p["leverage"] else 0,
                    "margin_ratio": round(100 / p["leverage"], 2) if p["leverage"] else 0,
                    "pnl_pct": round(p["unrealized_pnl"] / (p["entry_price"] * p["quantity"] / p["leverage"]) * 100, 2) if p["entry_price"] and p["leverage"] else 0,
                    "invested": round(p["entry_price"] * p["quantity"], 2),
                    **position_management_fields(p["symbol"]),
                }
                for p in positions
            ],
            "recent_trades": recent_trades,
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


