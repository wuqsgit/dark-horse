"""AlphaDog API Server 鈥?FastAPI (SQLite)"""
import asyncio
import hmac
import logging
import os, sys, json, time
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from api.ai_proxy import AIServiceProxy
from ai_service.config import AI_DB_PATH, MAIN_DB_PATH
from shared.strategy_insights import generate_strategy_insights

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
    fetch_candle_sync_status,
    init_db,
)
from shared.policy_loop import (
    fetch_policy_loop_summary,
    fetch_exit_reviews,
    fetch_exit_review_summaries,
    fetch_position_action_evidence,
    generate_and_activate_policies,
    label_decision_outcomes,
    clear_legacy_backtest_data,
)
from shared.account_status_snapshot import (
    load_account_snapshot,
    sanitize_account_status_snapshot,
    save_account_snapshot,
)


def _roll_max_layers():
    try:
        from trader.config import TRADING_CONFIG

        return max(1, int((TRADING_CONFIG.get("roll_trading") or {}).get("max_layers") or 1))
    except Exception:
        return 1


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
        ("alpha_profit_lock_stage1", "达到第一档浮盈保护，减仓锁定利润"),
        ("alpha_profit_lock_stage2", "达到第二档浮盈保护，继续减仓锁定利润"),
        ("alpha_profit_lock_exit", "浮盈回撤到利润保护线，退出剩余仓位"),
        ("alpha_peak_giveback_exit", "主升段利润回吐达到上限，退出剩余仓位"),
        ("alpha_trade_profit_budget_exit", "剩余仓位回撤触及整笔交易利润预算"),
        ("alpha_trend_weak_profit_protect", "连续三根15分钟K线没有新高且量能转弱，减仓30%"),
        ("alpha_trend_weak_exit", "趋势转弱后跌到利润保护线，退出剩余仓位"),
        ("alpha_trend_structure_exit", "跌破近期结构低点且一小时趋势转弱，退出剩余仓位"),
        ("alpha_probe_no_progress_exit", "试仓一小时没有进展且趋势转弱，退出仓位"),
        ("alpha_spike_stall_profit_protect", "冲高后停滞，保护性减仓"),
        ("alpha_spike_stall_exit", "冲高停滞并跌到保护线，退出剩余仓位"),
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
        from trader.risk import determine_side, entry_alpha_for_side, meets_safety_filters

        row_dict = dict(row)
        side = determine_side(row_dict)
        ok, reason = meets_safety_filters(row_dict, side=side)
        score = float(row_dict.get("composite_score") or 0)
        entry_alpha = entry_alpha_for_side(row_dict, side)
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
_cors_origins = [
    item.strip()
    for item in os.getenv("DARK_HORSE_CORS_ORIGINS", "").split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Dark-Horse-Token"],
)
_ai_proxy = AIServiceProxy()
logger = logging.getLogger("api")
_admin_token = os.getenv("DARK_HORSE_API_TOKEN", "").strip()


@app.get("/api/ai/status")
async def ai_status():
    return await _ai_proxy.status()


@app.get("/api/ai/decisions")
async def ai_decisions(limit: int = 100):
    return await _ai_proxy.decisions(limit)


@app.get("/api/ai/strategy-insights")
async def ai_strategy_insights(limit: int = 8):
    return generate_strategy_insights(MAIN_DB_PATH, AI_DB_PATH, limit=limit)

_api_cache = {}
_response_cache = {}
_versioned_cache = {}
_scan_payload_cache = {"scan_id": None, "payload": None, "body": None, "time": 0}
_scan_refresh_task = None
_ACCOUNT_STATUS_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".runtime", "trading-account-status.json"
)
_account_status_snapshot = {"data": None, "time": 0.0, "last_error": None}
_account_status_refresh_task = None
_account_status_refresher_task = None
_runtime_status_snapshot = {"data": None, "time": 0.0, "last_error": None}
_runtime_status_refresh_task = None
_runtime_status_refresh_future = None
_runtime_status_refresh_lock = threading.Lock()
_runtime_status_refresh_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="runtime-status-refresh",
)


_SCAN_CACHE_TTL = 5
_BACKTEST_CACHE_TTL = 300
_TRADING_ACCOUNT_STATUS_CACHE_TTL = 10
_TRADING_ACCOUNT_STATUS_STALE_AFTER = 20
_TRADING_RUNTIME_STATUS_CACHE_TTL = 30
_RUNTIME_STATUS_REFRESH_ERROR_CODE = "runtime_snapshot_refresh_failed"
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
}


def _cache_ttl_for_path(path: str) -> int:
    if path == "/api/trading/accounts/status":
        return _TRADING_ACCOUNT_STATUS_CACHE_TTL
    if path.startswith("/api/scan/"):
        return _SCAN_CACHE_TTL
    if path in {"/api/backtest/review", "/api/backtest/factor_analysis"}:
        return 5
    return _BACKTEST_CACHE_TTL


def compute_v3_signals(symbol, row, tech, side=None):
    try:
        from engine.breakout_detector import (
            check_breakout_confirmation,
            compute_breakout_metrics,
            compute_rr_detail,
            compute_sr_levels,
        )
        from trader.cooldown_manager import is_in_cooldown

        price = float(row["market_price"] or 0)
        atr = tech.get("atr", 0)

        breakout_ok, breakout_reason = check_breakout_confirmation(symbol)
        metrics = compute_breakout_metrics(symbol)
        rr = compute_rr_detail(symbol, price, atr) if price > 0 and atr > 0 else {"rr_used": 0, "rr_atr": 0, "rr_structure": 0, "rr_method": "none"}
        if side == "SHORT" and price > 0 and atr > 0:
            sr = compute_sr_levels(symbol)
            support = float(sr.get("support") or 0)
            risk = atr * 2
            reward = price - support if 0 < support < price else 0
            rr_structure = reward / risk if risk > 0 else 0
            rr = {
                "rr_used": round(rr_structure if rr_structure > 0 else 2.0, 2),
                "rr_atr": 2.0,
                "rr_structure": round(rr_structure, 2),
                "rr_method": "structure" if rr_structure > 0 else "atr",
                "reward_price": round(support if reward > 0 else price - atr * 4, 8),
                "risk_price": round(price + risk, 8),
                "support": support,
                "resistance": float(sr.get("resistance") or 0),
            }
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
                "tp1": round(price + (-2 if side == "SHORT" else 2)*atr, 4) if price > 0 else 0,
                "tp2": round(price + (-4 if side == "SHORT" else 4)*atr, 4) if price > 0 else 0,
                "tp3": round(price + (-6 if side == "SHORT" else 6)*atr, 4) if price > 0 else 0,
                "stop": round(price + (2 if side == "SHORT" else -2)*atr, 4) if price > 0 else 0,
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
    ttl = _cache_ttl_for_path(request.url.path)
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
    _ensure_trading_runtime_status_refresh()
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
    except Exception:
        pass


@app.on_event("shutdown")
async def shutdown_runtime_status_refresh():
    await _shutdown_trading_runtime_status_refresh()


async def get_user():
    return "viewer"


async def require_admin(
    x_dark_horse_token: str | None = Header(
        default=None,
        alias="X-Dark-Horse-Token",
    ),
):
    if not _admin_token:
        raise HTTPException(
            status_code=503,
            detail="admin token is not configured",
        )
    if not x_dark_horse_token or not hmac.compare_digest(
        x_dark_horse_token,
        _admin_token,
    ):
        raise HTTPException(
            status_code=401,
            detail="valid admin token required",
            headers={"WWW-Authenticate": "DarkHorseToken"},
        )
    return "admin"


@app.get("/api/auth/status")
async def auth_status():
    return {"admin_token_configured": bool(_admin_token)}


@app.get("/api/market-data/health")
async def get_market_data_health(user=Depends(get_user)):
    return json_response(await asyncio.to_thread(fetch_market_data_health))


@app.get("/api/market-data/minute/status")
async def get_minute_market_data_status(user=Depends(get_user)):
    status = await asyncio.to_thread(fetch_candle_sync_status)
    alerts = []
    now = datetime.now(timezone.utc)
    for runtime in status.get("runtime") or []:
        metrics = runtime.pop("metrics_json", "{}")
        try:
            runtime["metrics"] = json.loads(metrics or "{}")
        except (TypeError, ValueError):
            runtime["metrics"] = {}
        heartbeat = runtime.get("heartbeat_at")
        if heartbeat and runtime.get("status") != "completed":
            try:
                observed = datetime.fromisoformat(
                    str(heartbeat).replace("Z", "+00:00")
                )
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age = (now - observed.astimezone(timezone.utc)).total_seconds()
                runtime["heartbeat_age_seconds"] = round(max(0, age), 1)
                if age > 180:
                    alerts.append(
                        {
                            "severity": "error",
                            "code": "minute_collector_stale",
                            "collector_id": runtime.get("collector_id"),
                            "age_seconds": round(age, 1),
                        }
                    )
            except (TypeError, ValueError):
                pass
        if runtime.get("status") == "degraded":
            alerts.append(
                {
                    "severity": "warning",
                    "code": "minute_collector_degraded",
                    "collector_id": runtime.get("collector_id"),
                    "error": runtime.get("last_error"),
                }
            )
    for gap in status.get("gaps") or []:
        alerts.append(
            {
                "severity": "warning",
                "code": "minute_candle_gap",
                "market_kind": gap.get("market_kind"),
                "source_env": gap.get("source_env"),
                "count": gap.get("count"),
                "oldest_start": gap.get("oldest_start"),
            }
        )
    for aggregate in status.get("aggregates") or []:
        mismatch_count = int(aggregate.get("mismatch_count") or 0)
        if mismatch_count:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "minute_aggregate_mismatch",
                    "market_kind": aggregate.get("market_kind"),
                    "interval": aggregate.get("interval"),
                    "count": mismatch_count,
                }
            )
    status["mode"] = os.getenv("MINUTE_PIPELINE_MODE", "live")
    status["alerts"] = alerts
    return json_response(status)


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
        v3_signals = compute_v3_signals(r["symbol"], r, tech, plain.get("side"))
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
            "short_entry_alpha": float(features.get("short_entry_alpha") or 0),
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
        "short_entry_alpha": float(features.get("short_entry_alpha") or 0),
        "hold_alpha": float(row.get("hold_alpha") or 0),
    }
    
    # V3.0 signal state (real-time calculation)
    try:
        from trader.entry_profiles import evaluate_profile_entry
        sym = symbol.upper()
        result["v3_signals"] = compute_v3_signals(sym, row, tech, result["plain_signal"].get("side"))
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
async def label_policy_outcomes_now(user=Depends(require_admin)):
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
async def clear_legacy_backtest(vacuum: bool = False, user=Depends(require_admin)):
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
async def save_factor_weights(body: dict, user=Depends(require_admin)):
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
async def update_strategy_learning_status(candidate_id: int, body: dict, user=Depends(require_admin)):
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


def _clear_api_caches():
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
    from shared.accounts import account_exchange_config, get_default_account
    from shared.db import get_conn, reset_account_context, set_account_context
    from trader.exchange import BinanceFutures
    from trader.execution import ExecutionEngine

    account = get_default_account(include_secrets=True)
    config = account_exchange_config(account, require_credentials=True)
    token = set_account_context(account["id"])
    ex = BinanceFutures(
        config=config,
        account_id=account["id"],
        account_name=account["name"],
    )
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
        reset_account_context(token)


@app.get("/api/trading/controls")
async def get_trading_controls(user=Depends(get_user)):
    return get_trading_runtime_controls()


@app.post("/api/trading/controls")
async def update_trading_controls(body: dict, user=Depends(require_admin)):
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
    _clear_api_caches()
    return {
        "ok": True,
        "controls": controls,
        "close_result": close_result,
    }


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
            "roll_max_layers": _roll_max_layers(),
            "roll_price": None, "protected_stop": None,
            "last_roll_time": None, "protected_profit": 0,
            "max_floating_pnl": 0, "max_floating_roi": 0,
            "alpha_profit_lock_stage": 0, "alpha_locked_roi": 0,
            "alpha_stall_protect_price": None, "alpha_stall_protect_time": None,
            "roll_enabled": False,
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
        "roll_max_layers": _roll_max_layers(),
        "roll_status": roll_status,
        "roll_price": number("roll_price"),
        "protected_stop": number("protected_stop"),
        "last_roll_time": state.get("last_roll_time"),
        "protected_profit": number("protected_profit", 0),
        "max_floating_pnl": number("max_floating_pnl", 0),
        "max_floating_roi": number("max_floating_roi", 0),
        "alpha_profit_lock_stage": int(state.get("alpha_profit_lock_stage") or 0),
        "alpha_locked_roi": number("alpha_locked_roi", 0),
        "alpha_stall_protect_price": number("alpha_stall_protect_price"),
        "alpha_stall_protect_time": state.get("alpha_stall_protect_time"),
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
           WHERE account_id=? ORDER BY time DESC, id DESC LIMIT 1""",
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


def _exchange_account_status_payload(account: dict) -> dict:
    from shared.accounts import account_exchange_config
    from shared.db import (
        get_conn,
        reset_account_context,
        set_account_context,
    )
    from trader.exchange import BinanceFutures

    account_token = set_account_context(account["id"])
    ex = None
    try:
        ex = BinanceFutures(
            config=account_exchange_config(account, require_credentials=True),
            account_id=account["id"],
            account_name=account["name"],
        )
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
            "position_count": len(positions), "positions": positions,
            "normal_trading_enabled": bool(account.get("normal_trading_enabled")),
            "alpha_trading_enabled": bool(account.get("alpha_trading_enabled")),
            "auto_trading_enabled": bool(account.get("auto_trading_enabled")),
        }
    except Exception as exc:
        return {
            "account_id": account["id"], "account_name": account["name"],
            "environment": account["environment"], "status": "degraded",
            "error": str(exc), "positions": [],
            "initial_capital": float(account.get("initial_capital") or 0),
            "max_positions": int(account.get("max_positions") or 5),
        }
    finally:
        if ex is not None:
            ex.close()
        reset_account_context(account_token)


def _parse_snapshot_time(value) -> float | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _account_status_payload(account: dict) -> dict:
    """Build one live-account view from bounded current-state database reads."""
    from shared.db import get_conn
    from shared.live_account_store import fetch_live_account_snapshot

    snapshot = fetch_live_account_snapshot(account["id"])
    balance = snapshot.get("balance")
    stream_state = snapshot.get("state") or {}
    raw_positions = snapshot.get("positions") or []
    updated_timestamp = _parse_snapshot_time(
        stream_state.get("last_success_at")
        or stream_state.get("updated_at")
        or (balance or {}).get("updated_at")
    )
    age_seconds = (
        max(0.0, time.time() - updated_timestamp)
        if updated_timestamp is not None
        else None
    )
    stale = balance is None or age_seconds is None or age_seconds >= _TRADING_ACCOUNT_STATUS_STALE_AFTER
    stream_status = str(stream_state.get("status") or "starting")
    status = "ok" if balance is not None and not stale and stream_status == "ok" else "degraded"

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
        position_states = {
            row["symbol"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM account_position_history WHERE account_id=?",
                (account["id"],),
            ).fetchall()
        }
        positions = []
        equity = float((balance or {}).get("equity") or 0)
        total_maint_margin = float((balance or {}).get("total_maint_margin") or 0)
        cross_margin_ratio = (
            total_maint_margin / equity * 100 if equity > 0 else None
        )
        for raw_position in raw_positions:
            position = dict(raw_position)
            symbol = str(position.get("symbol") or "")
            state = position_states.get(symbol) or {}
            open_order = conn.execute(
                """SELECT created_at FROM orders
                   WHERE account_id=? AND symbol=? AND order_type='MARKET'
                   ORDER BY id DESC LIMIT 1""",
                (account["id"], symbol),
            ).fetchone()
            latest_action = conn.execute(
                """SELECT filter_reason, decision_result
                   FROM strategy_decisions
                   WHERE account_id=? AND symbol=?
                     AND decision_stage IN
                         ('position_management','roll_position','execution')
                   ORDER BY id DESC LIMIT 1""",
                (account["id"], symbol),
            ).fetchone()
            score_row = conn.execute(
                """SELECT raw_features FROM alpha_scores
                   WHERE symbol=? ORDER BY time DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            if score_row:
                market_phase = (_parse_json(score_row["raw_features"], {}) or {}).get(
                    "market_phase"
                ) or {}
            else:
                alpha_row = conn.execute(
                    """SELECT raw_features FROM alpha_scan_scores
                       WHERE futures_symbol=? ORDER BY time DESC LIMIT 1""",
                    (symbol,),
                ).fetchone()
                market_phase = (
                    (_parse_json(alpha_row["raw_features"], {}) or {}).get("market_phase")
                    if alpha_row
                    else {}
                ) or {}

            position["positionSide"] = position.get("position_side") or "BOTH"
            position["account_id"] = account["id"]
            position["account_name"] = account["name"]
            entry_time = state.get("entry_time") or (
                open_order["created_at"] if open_order else None
            )
            position.update(_live_holding_fields(entry_time))
            position.update(_live_position_management_fields(state))
            position["market_phase"] = market_phase
            position["last_system_action"] = (
                latest_action["filter_reason"] or latest_action["decision_result"]
                if latest_action
                else None
            )
            position["invested"] = round(
                float(position.get("notional") or 0)
                or abs(
                    float(position.get("entry_price") or 0)
                    * float(position.get("quantity") or 0)
                ),
                2,
            )
            if state.get("strategy_source") == "alpha":
                from shared.db import fetch_latest_alpha_position_context

                alpha_context = fetch_latest_alpha_position_context(
                    symbol=symbol,
                    alpha_symbol=state.get("alpha_symbol"),
                ) or {}
                reasons = _parse_json(
                    alpha_context.get("volume_price_reasons_json"), []
                )
                position.update({
                    "alpha_current_score": (
                        float(alpha_context.get("alpha_score") or 0)
                        if alpha_context
                        else None
                    ),
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
            position_margin = float(
                position.get("margin")
                or position.get("position_initial_margin")
                or 0
            )
            position["pnl_pct"] = (
                round(float(position.get("unrealized_pnl") or 0) / position_margin * 100, 2)
                if position_margin > 0
                else None
            )
            if str(position.get("margin_type") or "").lower() in {"cross", "crossed"}:
                position["margin_ratio"] = (
                    round(cross_margin_ratio, 4)
                    if cross_margin_ratio is not None
                    else None
                )
            else:
                isolated_balance = float(position.get("isolated_margin") or 0) + float(
                    position.get("unrealized_pnl") or 0
                )
                position["margin_ratio"] = (
                    round(
                        float(position.get("maint_margin") or 0)
                        / isolated_balance
                        * 100,
                        4,
                    )
                    if isolated_balance > 0
                    else None
                )
            positions.append(position)
    finally:
        conn.close()

    initial = float(account.get("initial_capital") or 0)
    wallet = float((balance or {}).get("wallet_balance") or 0)
    equity = float((balance or {}).get("equity") or wallet)
    total_pnl = equity - initial - adjustments
    base = initial + adjustments
    return {
        "account_id": account["id"],
        "account_name": account["name"],
        "environment": account["environment"],
        "status": status,
        "stale": stale,
        "error": stream_state.get("last_error"),
        "snapshot_at": updated_timestamp,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "initial_capital": initial,
        "net_capital_adjustments": adjustments,
        "wallet_balance": wallet,
        "equity": equity,
        "available_balance": float((balance or {}).get("available_balance") or 0),
        "unrealized_pnl": float((balance or {}).get("unrealized_pnl") or 0),
        "total_pnl": total_pnl,
        "return_pct": total_pnl / base * 100 if base else 0,
        "max_positions": int(account.get("max_positions") or 5),
        "position_count": len(positions),
        "positions": positions,
        "open_orders": snapshot.get("orders") or [],
        "normal_trading_enabled": bool(account.get("normal_trading_enabled")),
        "alpha_trading_enabled": bool(account.get("alpha_trading_enabled")),
        "auto_trading_enabled": bool(account.get("auto_trading_enabled")),
    }


def _refresh_all_account_statuses_sync() -> dict:
    from shared.accounts import list_accounts

    accounts = list_accounts(enabled_only=True)
    results = [_account_status_payload(account) for account in accounts]
    available = [row for row in results if row.get("snapshot_at") is not None]
    environments = {row.get("environment") for row in results}
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
            "initial_capital": sum(float(r.get("initial_capital") or 0) for r in results),
            "equity": sum(float(r.get("equity") or 0) for r in available),
            "total_pnl": sum(float(r.get("total_pnl") or 0) for r in available),
            "unrealized_pnl": sum(float(r.get("unrealized_pnl") or 0) for r in available),
            "position_count": sum(len(r.get("positions") or []) for r in available),
        },
        "snapshot_at": min((r["snapshot_at"] for r in available), default=None),
        "age_seconds": max((float(r.get("age_seconds") or 0) for r in available), default=None),
        "fresh": bool(results) and all(
            row.get("status") == "ok" and not row.get("stale")
            for row in results
        ),
        "last_error": next((row.get("error") for row in results if row.get("error")), None),
    }


def _load_persisted_trading_account_status_snapshot() -> None:
    persisted = load_account_snapshot(_ACCOUNT_STATUS_SNAPSHOT_PATH)
    if not persisted:
        return
    data = persisted.get("data")
    snapshot_at = persisted.get("snapshot_at")
    if not isinstance(data, dict) or not isinstance(snapshot_at, (int, float)):
        logger.warning("Ignoring invalid persisted account status snapshot")
        return
    _account_status_snapshot.update({
        "data": sanitize_account_status_snapshot(data),
        "time": float(snapshot_at),
        "last_error": None,
    })


async def _run_trading_account_status_refresh() -> dict:
    global _account_status_refresh_task
    try:
        data = sanitize_account_status_snapshot(
            await asyncio.to_thread(_refresh_all_account_statuses_sync)
        )
        snapshot_at = time.time()
        save_account_snapshot(_ACCOUNT_STATUS_SNAPSHOT_PATH, {
            "data": data,
            "snapshot_at": snapshot_at,
        })
        _account_status_snapshot.update({
            "data": data,
            "time": snapshot_at,
            "last_error": None,
        })
        return data
    except Exception as exc:
        logger.exception("Trading account status refresh failed")
        _account_status_snapshot["last_error"] = str(exc)
        if _account_status_snapshot.get("data") is not None:
            return _account_status_snapshot["data"]
        raise
    finally:
        if asyncio.current_task() is _account_status_refresh_task:
            _account_status_refresh_task = None


def _ensure_trading_account_status_refresh() -> asyncio.Task:
    global _account_status_refresh_task
    if _account_status_refresh_task is None or _account_status_refresh_task.done():
        _account_status_refresh_task = asyncio.create_task(_run_trading_account_status_refresh())
    return _account_status_refresh_task


async def _get_trading_account_status_snapshot() -> tuple[dict, str]:
    data = _account_status_snapshot.get("data")
    age = max(0.0, time.time() - float(_account_status_snapshot.get("time") or 0))
    cache_status = (
        "MISS"
        if data is None
        else "STALE"
        if age >= _TRADING_ACCOUNT_STATUS_STALE_AFTER
        else "HIT"
    )
    if data is not None and age >= _TRADING_ACCOUNT_STATUS_CACHE_TTL:
        _ensure_trading_account_status_refresh()
    snapshot_at = float(_account_status_snapshot.get("time") or 0)
    payload = dict(data or {})
    payload["accounts"] = payload.get("accounts") or []
    payload["summary"] = payload.get("summary") or {}
    payload["snapshot_at"] = snapshot_at or None
    payload["age_seconds"] = round(age, 1) if snapshot_at else None
    payload["fresh"] = cache_status == "HIT"
    payload["last_error"] = _account_status_snapshot.get("last_error")
    return payload, cache_status


async def _account_status_snapshot_refresher():
    while True:
        started = time.monotonic()
        try:
            await _ensure_trading_account_status_refresh()
        except Exception:
            pass
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, _TRADING_ACCOUNT_STATUS_CACHE_TTL - elapsed))


@app.get("/api/trading/accounts")
async def get_trading_accounts(user=Depends(get_user)):
    from shared.accounts import list_accounts
    return {"accounts": list_accounts()}


@app.post("/api/trading/accounts")
async def create_trading_account(body: dict, user=Depends(require_admin)):
    from shared.accounts import save_account
    try:
        account = save_account(body)
        _clear_api_caches()
        return {"status": "ok", "account": account}
    except Exception as exc:
        return {"error": str(exc)}


@app.patch("/api/trading/accounts/{account_id}")
async def update_trading_account(account_id: int, body: dict, user=Depends(require_admin)):
    from shared.accounts import save_account
    try:
        account = save_account(body, account_id=account_id)
        _clear_api_caches()
        return {"status": "ok", "account": account}
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/trading/accounts/{account_id}/controls")
async def update_account_trading_control(
    account_id: int,
    body: dict,
    user=Depends(get_user),
):
    from shared.accounts import set_account_trading_enabled

    mode = str(body.get("mode") or "").strip().lower()
    if mode not in {"normal", "alpha"} or not isinstance(body.get("enabled"), bool):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_trading_control"},
        )
    try:
        account = await asyncio.to_thread(
            set_account_trading_enabled,
            account_id,
            mode,
            body["enabled"],
        )
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={"code": "account_not_found"},
        ) from None
    _clear_api_caches()
    return {"ok": True, "account": account}


@app.delete("/api/trading/accounts/{account_id}")
async def delete_trading_account(account_id: int, user=Depends(require_admin)):
    from trader.account_lifecycle import close_positions_and_delete_account
    try:
        result = await asyncio.to_thread(
            close_positions_and_delete_account,
            account_id,
        )
        _clear_api_caches()
        return {"status": "ok", **result}
    except Exception as exc:
        _clear_api_caches()
        return {"error": str(exc)}


@app.post("/api/trading/accounts/{account_id}/test")
async def test_trading_account(account_id: int, user=Depends(require_admin)):
    from shared.accounts import get_account
    account = get_account(account_id, include_secrets=True)
    if not account:
        return {"error": "账户不存在"}
    result = await asyncio.to_thread(_exchange_account_status_payload, account)
    return result


@app.post("/api/trading/accounts/{account_id}/capital-adjustments")
async def add_account_capital_adjustment(account_id: int, body: dict, user=Depends(require_admin)):
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
async def get_all_trading_account_status(response: Response, user=Depends(get_user)):
    payload = await asyncio.to_thread(_refresh_all_account_statuses_sync)
    response.headers["X-Cache"] = "DB"
    return payload


def _trading_account_or_404(account_id: int) -> dict:
    from shared.accounts import get_account

    account = get_account(account_id)
    if not account:
        raise HTTPException(
            status_code=404,
            detail={"code": "account_not_found"},
        )
    return account


def _parse_history_timestamp(value: str | None):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_date"},
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _account_history_payload(
    account_id: int,
    *,
    cursor: str | None,
    limit: int,
    symbol: str | None,
    direction: str | None,
    source: str | None,
    from_time: str | None,
    to_time: str | None,
) -> dict:
    from shared.trade_history import fetch_trade_history_summaries

    _trading_account_or_404(account_id)
    return fetch_trade_history_summaries(
        account_id,
        cursor=cursor,
        limit=limit,
        symbol=symbol,
        direction=direction,
        source=source,
        from_time=from_time,
        to_time=to_time,
    )


@app.get("/api/trading/accounts/{account_id}/history")
async def get_account_history(
    account_id: int,
    cursor: str | None = None,
    limit: int = 20,
    symbol: str | None = None,
    direction: str | None = None,
    source: str | None = None,
    from_time: Annotated[str | None, Query(alias="from")] = None,
    to_time: Annotated[str | None, Query(alias="to")] = None,
    user=Depends(get_user),
):
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_limit"},
        )
    normalized_direction = str(direction).strip().upper() if direction else None
    if normalized_direction not in {None, "LONG", "SHORT"}:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_direction"},
        )
    parsed_from = _parse_history_timestamp(from_time)
    parsed_to = _parse_history_timestamp(to_time)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_date"},
        )
    try:
        return await asyncio.to_thread(
            _account_history_payload,
            account_id,
            cursor=cursor,
            limit=limit,
            symbol=symbol,
            direction=normalized_direction,
            source=source,
            from_time=parsed_from,
            to_time=parsed_to,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "invalid_history_cursor":
            raise HTTPException(
                status_code=400,
                detail={"code": code},
            ) from None
        raise


def _account_decisions_payload(account_id: int) -> dict:
    from shared.db import get_conn

    _trading_account_or_404(account_id)
    with closing(get_conn()) as conn:
        return _account_decision_panel(conn, account_id)


@app.get("/api/trading/accounts/{account_id}/decisions")
async def get_account_decisions(account_id: int, user=Depends(get_user)):
    return await asyncio.to_thread(_account_decisions_payload, account_id)


def _trading_runtime_status_payload() -> dict:
    from shared.accounts import list_runtime_accounts
    from shared.live_diagnostics import build_live_diagnostics

    accounts = list_runtime_accounts()
    return {
        "trading_controls": _safe_trading_runtime_controls(),
        "accounts": [
            {
                "account_id": int(account["id"]),
                "account_name": account["name"],
                "environment": account["environment"],
                "runtime_diagnostics": build_live_diagnostics(account),
            }
            for account in accounts
        ],
    }


def _build_trading_runtime_status_snapshot() -> dict:
    try:
        data = _trading_runtime_status_payload()
        snapshot_at = time.time()
        _runtime_status_snapshot.update({
            "data": data,
            "time": snapshot_at,
            "last_error": None,
        })
        return data
    except Exception as exc:
        logger.exception("Trading runtime status refresh failed")
        _runtime_status_snapshot["last_error"] = _RUNTIME_STATUS_REFRESH_ERROR_CODE
        return _runtime_status_snapshot.get("data") or {
            "trading_controls": {},
            "accounts": [],
        }


def _release_trading_runtime_status_worker(future: Future) -> None:
    global _runtime_status_refresh_future
    with _runtime_status_refresh_lock:
        if _runtime_status_refresh_future is future:
            _runtime_status_refresh_future = None


def _ensure_trading_runtime_status_worker() -> tuple[Future, bool]:
    global _runtime_status_refresh_future
    with _runtime_status_refresh_lock:
        future = _runtime_status_refresh_future
        if future is not None and not future.done():
            return future, False
        future = _runtime_status_refresh_executor.submit(
            _build_trading_runtime_status_snapshot
        )
        _runtime_status_refresh_future = future
    future.add_done_callback(_release_trading_runtime_status_worker)
    return future, True


async def _await_trading_runtime_status_refresh(future: Future) -> dict:
    global _runtime_status_refresh_task
    try:
        return await asyncio.wrap_future(future)
    finally:
        if asyncio.current_task() is _runtime_status_refresh_task:
            _runtime_status_refresh_task = None


async def _run_trading_runtime_status_refresh() -> dict:
    future, _ = _ensure_trading_runtime_status_worker()
    return await _await_trading_runtime_status_refresh(future)


def _ensure_trading_runtime_status_refresh() -> asyncio.Task | None:
    global _runtime_status_refresh_task
    current_loop = asyncio.get_running_loop()
    future, worker_started = _ensure_trading_runtime_status_worker()
    task = _runtime_status_refresh_task
    if task is not None and not task.done():
        if task.get_loop() is current_loop:
            return task
        if not worker_started:
            return None
    _runtime_status_refresh_task = current_loop.create_task(
        _await_trading_runtime_status_refresh(future)
    )
    return _runtime_status_refresh_task


async def _shutdown_trading_runtime_status_refresh() -> None:
    global _runtime_status_refresh_task
    task = _runtime_status_refresh_task
    if task is None:
        return
    if task.get_loop() is not asyncio.get_running_loop():
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if _runtime_status_refresh_task is task:
        _runtime_status_refresh_task = None


async def _get_trading_runtime_status_snapshot() -> tuple[dict, str]:
    data = _runtime_status_snapshot.get("data")
    snapshot_at = float(_runtime_status_snapshot.get("time") or 0)
    age = max(0.0, time.time() - snapshot_at)
    cache_status = (
        "MISS"
        if data is None
        else "STALE"
        if age >= _TRADING_RUNTIME_STATUS_CACHE_TTL
        else "HIT"
    )
    payload = dict(data or {})
    payload["trading_controls"] = payload.get("trading_controls") or {}
    payload["accounts"] = payload.get("accounts") or []
    payload["snapshot_at"] = snapshot_at or None
    payload["age_seconds"] = round(age, 1) if snapshot_at else None
    payload["fresh"] = cache_status == "HIT"
    payload["last_error"] = (
        _RUNTIME_STATUS_REFRESH_ERROR_CODE
        if _runtime_status_snapshot.get("last_error")
        else None
    )
    if cache_status != "HIT":
        _ensure_trading_runtime_status_refresh()
    return payload, cache_status


@app.get("/api/trading/runtime/status")
async def get_trading_runtime_status(response: Response, user=Depends(get_user)):
    payload, cache_status = await _get_trading_runtime_status_snapshot()
    response.headers["X-Cache"] = cache_status
    return payload


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


def _alpha_strategy_json_row(row, *json_fields):
    result = dict(row)
    for field in json_fields:
        raw = result.pop(field, None)
        target = field.removesuffix("_json")
        try:
            result[target] = json.loads(raw or ("[]" if "reason" in field else "{}"))
        except (TypeError, ValueError):
            result[target] = [] if "reason" in field else {}
    return result


@app.get("/api/alpha-strategy/status")
async def get_alpha_strategy_status(
    market_env: str | None = None,
    recent_limit: int = 30,
    user=Depends(get_user),
):
    """Operational view of the Alpha V2 worker, models, states and delivery."""
    from alpha_engine.strategy.repository import AlphaStrategyRepository
    from shared.db import get_conn

    env = str(market_env or "mainnet").lower()
    if env != "mainnet":
        return {"error": f"unsupported market_env: {market_env}"}
    limit = max(1, min(int(recent_limit), 200))
    repository = AlphaStrategyRepository()
    status = repository.strategy_status(env)
    conn = get_conn()
    try:
        where = "WHERE market_env=?" if env else ""
        params = (env,) if env else ()
        state_rows = conn.execute(
            f"""SELECT * FROM alpha_signal_states
                {where}
                ORDER BY datetime(updated_at) DESC, futures_symbol
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        event_rows = conn.execute(
            f"""SELECT * FROM alpha_signal_events
                {where}
                ORDER BY datetime(event_time) DESC, event_id DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        consumption_rows = conn.execute(
            """SELECT c.*, e.market_env, e.strategy_mode, e.futures_symbol,
                      e.alpha_symbol, e.from_state, e.to_state
               FROM alpha_signal_consumptions c
               JOIN alpha_signal_events e ON e.event_id=c.event_id
               ORDER BY datetime(c.updated_at) DESC, c.event_id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        latest_snapshot = conn.execute(
            f"""SELECT MAX(candle_close_time) AS candle_close_time,
                       COUNT(*) AS snapshot_count,
                       SUM(CASE WHEN data_quality_status='ready' THEN 1 ELSE 0 END)
                           AS ready_count
                FROM alpha_feature_snapshots
                {where}""",
            params,
        ).fetchone()
    finally:
        conn.close()
    status.update(
        {
            "market_env": env,
            "snapshot_summary": dict(latest_snapshot or {}),
            "recent_states": [
                _alpha_strategy_json_row(
                    row,
                    "model_versions_json",
                    "reason_codes_json",
                    "metrics_json",
                )
                for row in state_rows
            ],
            "recent_events": [
                _alpha_strategy_json_row(
                    row,
                    "reason_codes_json",
                    "ai_decision_json",
                )
                for row in event_rows
            ],
            "recent_consumptions": [dict(row) for row in consumption_rows],
            "ai": await _ai_proxy.alpha_strategy_status(env),
        }
    )
    alerts = []
    now = datetime.now(timezone.utc)
    for runtime in status["runtime"]:
        for field, minutes, code in (
            ("heartbeat_at", 3, "worker_heartbeat_stale"),
            ("last_candle_close_time", 20, "closed_candle_stale"),
        ):
            value = runtime.get(field)
            if not value:
                continue
            try:
                observed = datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                )
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age_minutes = (
                    now - observed.astimezone(timezone.utc)
                ).total_seconds() / 60
                if age_minutes > minutes:
                    alerts.append(
                        {
                            "severity": "error",
                            "code": code,
                            "market_env": runtime["market_env"],
                            "age_minutes": round(age_minutes, 1),
                        }
                    )
            except (TypeError, ValueError):
                pass
        metrics = runtime.get("metrics") or {}
        processed = max(1, int(runtime.get("processed_count") or 0))
        ai_failure_rate = float(metrics.get("ai_failure_count") or 0) / processed
        if ai_failure_rate > 0.10:
            alerts.append(
                {
                    "severity": "error",
                    "code": "ai_failure_rate_high",
                    "market_env": runtime["market_env"],
                    "rate": round(ai_failure_rate, 4),
                }
            )
    ai_status = status["ai"]
    quality = ai_status.get("feature_quality") or {}
    if quality.get("samples", 0) >= 30 and quality.get("ready_rate", 1) < 0.80:
        alerts.append(
            {
                "severity": "warning",
                "code": "feature_readiness_low",
                "rate": quality.get("ready_rate"),
            }
        )
    # Only the champion is serving predictions. A drifting challenger should be
    # visible in the registry, but must not duplicate the operational alert for
    # the same target.
    for model in ai_status.get("models") or []:
        if model.get("status") != "champion":
            continue
        if (model.get("drift") or {}).get("status") == "drift":
            alerts.append(
                {
                    "severity": "warning",
                    "code": "model_input_drift",
                    "version": model.get("version"),
                    "target": model.get("target"),
                    "details": model.get("drift"),
                }
            )
    status["alerts"] = alerts
    return status


@app.get("/api/alpha-strategy/snapshots")
async def get_alpha_strategy_snapshots(
    market_env: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
    user=Depends(get_user),
):
    from shared.db import get_conn

    where = []
    params = []
    env = str(market_env or "mainnet").lower()
    if env != "mainnet":
        return {"error": f"unsupported market_env: {market_env}", "snapshots": []}
    where.append("market_env=?")
    params.append(env)
    if symbol:
        where.append("futures_symbol=?")
        params.append(str(symbol).upper())
    clause = "WHERE " + " AND ".join(where) if where else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT * FROM alpha_feature_snapshots
                {clause}
                ORDER BY datetime(candle_close_time) DESC, snapshot_id DESC
                LIMIT ?""",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        return {
            "snapshots": [
                _alpha_strategy_json_row(
                    row,
                    "data_quality_json",
                    "features_json",
                )
                for row in rows
            ]
        }
    finally:
        conn.close()
