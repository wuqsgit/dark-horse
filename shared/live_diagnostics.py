"""User-facing health diagnostics for the live trading page."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from shared.db import (
    fetch_service_runtime_status,
    get_conn,
    get_trading_runtime_controls,
)


LIVE_ALPHA_MODES = {"testnet_live", "mainnet_canary", "mainnet_live"}
SERVICE_LABELS = {
    "pipeline": "普通行情采集",
    "engine": "普通策略评分",
    "alpha_pipeline": "Alpha 行情采集",
    "alpha_engine": "Alpha 策略引擎",
    "trader": "实盘交易循环",
}


def _parse_time(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_minutes(value, now):
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _enabled(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_live_diagnostics(account, *, exchange_error=None, now=None):
    """Combine persisted heartbeats, data freshness, switches and order errors."""
    now = now or datetime.now(timezone.utc)
    account = dict(account or {})
    account_id = int(account.get("id") or account.get("account_id") or 0)
    normal_enabled = bool(account.get("normal_trading_enabled"))
    alpha_enabled = bool(account.get("alpha_trading_enabled"))
    auto_enabled = bool(account.get("auto_trading_enabled"))
    issues = []

    def add(severity, code, title, message, observed_at=None, service=None):
        issues.append(
            {
                "severity": severity,
                "code": code,
                "title": title,
                "message": str(message),
                "observed_at": observed_at,
                "service": service,
            }
        )

    if exchange_error:
        add(
            "error",
            "exchange_connection_failed",
            "Binance 账户连接失败",
            exchange_error,
            service="trader",
        )
    if not auto_enabled:
        add("error", "auto_trading_disabled", "自动交易未启动", "该账户的自动交易开关处于关闭状态。")
    if not normal_enabled:
        add("warning", "normal_trading_disabled", "普通交易已关闭", "普通策略不会提交新开仓。")
    if not alpha_enabled:
        add("warning", "alpha_trading_disabled", "Alpha 交易已关闭", "Alpha 策略不会提交新开仓。")

    controls = get_trading_runtime_controls()
    if normal_enabled and not controls.get("normal_trading_enabled", False):
        add("error", "normal_runtime_disabled", "普通交易运行总开关关闭", "账户虽已开启，但系统运行总开关会阻止普通策略开仓。")
    if alpha_enabled and not controls.get("alpha_trading_enabled", False):
        add("error", "alpha_runtime_disabled", "Alpha 运行总开关关闭", "账户虽已开启，但系统运行总开关会阻止 Alpha 开仓。")

    alpha_v2_enabled = _enabled(os.getenv("ALPHA_STRATEGY_V2_ENABLED", "false"))
    alpha_mode = os.getenv("ALPHA_STRATEGY_V2_MODE", "shadow").strip().lower()
    if alpha_enabled and (not alpha_v2_enabled or alpha_mode not in LIVE_ALPHA_MODES):
        add(
            "error",
            "alpha_strategy_not_live",
            "Alpha 策略未处于实盘模式",
            f"当前 enabled={alpha_v2_enabled}、mode={alpha_mode}；需要启用并使用 testnet_live/mainnet_live 模式。",
            service="alpha_engine",
        )

    conn = get_conn()
    try:
        normal_scan_at = conn.execute("SELECT MAX(time) FROM alpha_scores").fetchone()[0]
        alpha_scan_at = conn.execute("SELECT MAX(time) FROM alpha_scan_scores").fetchone()[0]
        last_execution_at = conn.execute(
            """SELECT MAX(time) FROM decision_actions
               WHERE account_id=? AND action_result != 'scanned'""",
            (account_id or 1,),
        ).fetchone()[0]
        failed_order = conn.execute(
            """SELECT symbol, status, reason, updated_at
               FROM orders
               WHERE account_id=?
                 AND UPPER(COALESCE(status, '')) IN ('ERROR','FAILED','REJECTED')
                 AND datetime(updated_at) >= datetime('now', '-24 hours')
               ORDER BY datetime(updated_at) DESC, id DESC LIMIT 1""",
            (account_id or 1,),
        ).fetchone()
        failed_signal = conn.execute(
            """SELECT e.futures_symbol, c.status, c.rejection_reason, c.updated_at
               FROM alpha_signal_consumptions c
               JOIN alpha_signal_events e ON e.event_id=c.event_id
               WHERE c.account_id=? AND c.status IN ('failed','error')
                 AND datetime(c.updated_at) >= datetime('now', '-24 hours')
               ORDER BY datetime(c.updated_at) DESC LIMIT 1""",
            (account_id or 1,),
        ).fetchone()
    finally:
        conn.close()

    for enabled, value, code, title in (
        (normal_enabled, normal_scan_at, "normal_scan_stale", "普通扫描数据异常"),
        (alpha_enabled, alpha_scan_at, "alpha_scan_stale", "Alpha 扫描数据异常"),
    ):
        if not enabled:
            continue
        age = _age_minutes(value, now)
        if age is None:
            add("error", code, title, "尚未生成任何评分数据，策略无法判断开仓。", service="pipeline")
        elif age > 20:
            add("error", code, title, f"最新评分已延迟 {age:.1f} 分钟，策略将使用过期或空数据。", value, "pipeline")

    if failed_order:
        add(
            "error",
            "recent_order_failed",
            f"{failed_order['symbol']} 下单失败",
            failed_order["reason"] or f"订单状态：{failed_order['status']}",
            failed_order["updated_at"],
            "trader",
        )
    if failed_signal:
        add(
            "error",
            "alpha_signal_execution_failed",
            f"{failed_signal['futures_symbol']} Alpha 信号执行失败",
            failed_signal["rejection_reason"] or f"消费状态：{failed_signal['status']}",
            failed_signal["updated_at"],
            "trader",
        )

    runtime_rows = fetch_service_runtime_status()
    runtime_by_key = {
        (row["service_name"], int(row.get("account_id") or 0)): row
        for row in runtime_rows
    }
    expected = []
    if normal_enabled:
        expected.extend((("pipeline", 0, 25), ("engine", 0, 15)))
    if alpha_enabled:
        expected.extend((("alpha_pipeline", 0, 25), ("alpha_engine", 0, 15)))
    if auto_enabled:
        expected.append(("trader", account_id, 15))
    selected_runtime = []
    for service, runtime_account, stale_after in expected:
        row = runtime_by_key.get((service, runtime_account))
        label = SERVICE_LABELS.get(service, service)
        if row is None:
            add("warning", f"{service}_heartbeat_missing", f"{label}暂无心跳", "服务刚启动或尚未写入运行状态。", service=service)
            continue
        selected_runtime.append(row)
        age = _age_minutes(row.get("heartbeat_at"), now)
        if age is None or age > stale_after:
            age_text = "时间无效" if age is None else f"已中断 {age:.1f} 分钟"
            add("error", f"{service}_heartbeat_stale", f"{label}可能已停止", age_text, row.get("heartbeat_at"), service)
        if row.get("status") == "error" or row.get("last_error"):
            add(
                "error",
                row.get("error_code") or f"{service}_runtime_error",
                f"{label}运行异常",
                row.get("last_error") or "服务报告运行错误。",
                row.get("last_error_at") or row.get("heartbeat_at"),
                service,
            )
        elif row.get("status") == "degraded":
            add(
                "warning",
                row.get("error_code") or f"{service}_degraded",
                f"{label}运行降级",
                row.get("last_error") or "服务仍在运行，但当前无法完整处理。",
                row.get("heartbeat_at"),
                service,
            )

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: (severity_rank.get(item["severity"], 9), item["code"]))
    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    return {
        "status": "blocked" if error_count else "degraded" if warning_count else "healthy",
        "can_open_new_positions": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
        "services": selected_runtime,
        "normal_scan_at": normal_scan_at,
        "alpha_scan_at": alpha_scan_at,
        "last_execution_at": last_execution_at,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
