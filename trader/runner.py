"""交易主循环 — 定时拉评分 Top → 决策 → 执行"""
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

# 确保能找到 shared/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.db import (
    fetch_latest_scan,
    get_conn,
    init_db,
    insert_fill,
    insert_position_snapshot,
    record_strategy_decisions,
    update_order_status,
)
from trader.exchange import BinanceFutures
from trader.execution import ExecutionEngine
from trader.config import EXCHANGE_CONFIG, TRADING_CONFIG
from trader.risk import get_symbol_threshold, get_category_config

import warnings, urllib3
warnings.filterwarnings("ignore", category=DeprecationWarning)
urllib3.disable_warnings()
logger = logging.getLogger("trader")


async def _account_trading_loop(account):
    from shared.accounts import account_exchange_config
    from shared.db import set_account_context
    set_account_context(account["id"])
    exchange_cfg = account_exchange_config(account)
    missing_vars = []
    if not exchange_cfg.get("api_key"):
        missing_vars.append("API_KEY")
    if not exchange_cfg.get("api_secret"):
        missing_vars.append("API_SECRET")
    if missing_vars:
        logger.error("Binance API credentials missing: %s", ", ".join(missing_vars))
        return

    network = "Testnet" if exchange_cfg.get("testnet") else "Mainnet"
    logger.info("=== 启动实盘交易引擎 (%s) ===", network)

    # 初始化
    ex = BinanceFutures(exchange_cfg, account_id=account["id"], account_name=account["name"])
    engine = ExecutionEngine(ex)
    engine.cfg = dict(engine.cfg)
    engine.cfg["max_positions"] = int(account.get("max_positions") or 5)
    engine.account_controls = {
        "normal_trading_enabled": bool(account.get("normal_trading_enabled")),
        "alpha_trading_enabled": bool(account.get("alpha_trading_enabled")),
    }
    last_income_sync = 0  # 上次 income 同步时间
    loop_interval = int(TRADING_CONFIG.get("rebalance_interval_min", 5) * 60)

    while True:
        try:
            run_id = f"live-a{account['id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            now = asyncio.get_event_loop().time()
            
            # 每 5 分钟同步一次 income 数据
            if now - last_income_sync > 300:
                fetch_and_store_income(ex)
                last_income_sync = now
            # 1. 检查账户状态
            balance = ex.get_balance()
            positions = engine.get_current_positions()
            if positions:
                snap_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                insert_position_snapshot([
                    (
                        snap_time,
                        p["symbol"],
                        p["side"],
                        p.get("positionSide"),
                        p["quantity"],
                        p["entry_price"],
                        p["mark_price"],
                        p["unrealized_pnl"],
                        p.get("leverage", 1),
                        None,
                        None,
                    )
                    for p in positions
                ])

            logger.info(f"余额: ${balance:.2f} | 持仓: {len(positions)}")
            for p in positions:
                logger.info(f"  {p['symbol']} {p['side']} x{p['quantity']} entry=${p['entry_price']:.4f} PnL=${p['unrealized_pnl']:.2f}")

            # 2. 获取最新评分 Top
            scan, rows = fetch_latest_scan()
            if not rows:
                logger.warning("暂无评分数据")
                await asyncio.sleep(60)
                continue

            # 评分降序排
            top = sorted(rows, key=lambda r: -r["composite_score"])
            top_symbols = []
            for r in top:
                top_symbols.append({
                    "symbol": r["symbol"],
                    "time": r["time"],
                    "scan_id": r["scan_id"],
                    "composite_score": float(r["composite_score"] or 0),
                    "composite_summary": r["composite_summary"],
                    "price": float(r["market_price"] or 0),
                    "trend_state": r["trend_state"],
                    "trend_direction": r["trend_direction"],
                    "chip_phase": r["chip_phase"],
                    "price_position": r["price_position"],
                    "volatility_level": r["volatility_level"],
                    "relative_strength": float(r["relative_strength"] or 50),
                    "entry_alpha": float(r["entry_alpha"] or 0),
                    "hold_alpha": float(r["hold_alpha"] or 0),
                    "raw_features": r["raw_features"],
                })

            record_strategy_decisions([
                {
                    "decision_id": f"{run_id}:scan:{r['symbol']}",
                    "run_id": run_id,
                    "time": r.get("time"),
                    "scan_id": r.get("scan_id"),
                    "symbol": r["symbol"],
                    "side": "SKIP",
                    "mode": "live",
                    "decision_stage": "scan",
                    "decision_result": "scanned",
                    "composite_score": r.get("composite_score"),
                    "grade": r.get("composite_summary"),
                    "price": r.get("price"),
                    "features": r.get("raw_features"),
                    "reason": {
                        "trend_state": r.get("trend_state"),
                        "trend_direction": r.get("trend_direction"),
                        "chip_phase": r.get("chip_phase"),
                        "volatility_level": r.get("volatility_level"),
                        "price_position": r.get("price_position"),
                        "relative_strength": r.get("relative_strength"),
                    },
                }
                for r in top_symbols
            ])

            logger.info(f"评分 Top: {top_symbols[0]['symbol']} ({top_symbols[0]['composite_score']:.1f})")

            # 3. 决策
            actions = engine.decide(top_symbols, positions, run_id=run_id)
            actions = [
                action for action in actions
                if action.get("action") != "open"
                or (
                    (action.get("strategy_source") == "alpha" and bool(account.get("alpha_trading_enabled")))
                    or (action.get("strategy_source") != "alpha" and bool(account.get("normal_trading_enabled")))
                )
            ]

            # 3.5 按类别打印评分排序 + 未开仓原因
            pending = [a.get('symbol') for a in actions if a.get('action') == 'open']
            _log_category_ranking(top_symbols, positions, pending)

            # 4. 执行
            if actions:
                logger.info(f"操作计划 ({len(actions)} 条):")
                for a in actions:
                    logger.info(f"  [{a['action']}] {a.get('symbol','?')} reason: {a.get('reason','')}")
                results = engine.execute(actions)
                logger.info(f"执行完成: {sum(1 for r in results if r['status']=='ok')} OK / {sum(1 for r in results if r['status']=='error')} ERR")
            else:
                logger.info("无需操作")

        except Exception as e:
            logger.error(f"循环异常: {e}", exc_info=True)

        await asyncio.sleep(loop_interval)

    # === 以下在 while True 之外（不会执行），保留供手动调用 ===


async def trading_loop():
    from shared.accounts import list_accounts

    tasks = {}
    signatures = {}
    while True:
        try:
            accounts = list_accounts(include_secrets=True, enabled_only=True)
        except Exception as exc:
            logger.warning("Trading account configuration temporarily unavailable: %s", exc)
            await asyncio.sleep(5)
            continue
        active_ids = set()
        for account in accounts:
            if not account.get("auto_trading_enabled"):
                continue
            account_id = int(account["id"])
            active_ids.add(account_id)
            signature = (
                account.get("environment"), account.get("api_key"), account.get("api_secret"),
                account.get("max_positions"), account.get("normal_trading_enabled"),
                account.get("alpha_trading_enabled"), account.get("auto_trading_enabled"),
            )
            if account_id in tasks and not tasks[account_id].done() and signatures.get(account_id) == signature:
                continue
            if account_id in tasks and not tasks[account_id].done():
                tasks[account_id].cancel()
            tasks[account_id] = asyncio.create_task(_account_trading_loop(account))
            signatures[account_id] = signature
        for account_id in list(tasks):
            if account_id not in active_ids:
                if not tasks[account_id].done():
                    tasks[account_id].cancel()
                tasks.pop(account_id, None)
                signatures.pop(account_id, None)
        await asyncio.sleep(30)


def fetch_and_store_income(ex, days_back=7):
    """🔧 拉取币安 Income API 并写入 fills 表

    作为交易记录的单一真相源。
    """
    from datetime import timedelta
    from collections import defaultdict
    from shared.db import (
        backfill_income_ledger_from_fills,
        rebuild_position_trades_from_income,
        upsert_exchange_income,
    )
    conn = get_conn()
    now = datetime.now(timezone.utc)
    account_id = int(getattr(ex, "account_id", 1) or 1)
    
    # 获取最后一次同步的时间戳
    last_sync = conn.execute(
        "SELECT MAX(created_at) FROM fills WHERE account_id=? AND side='REALIZED_PNL'",
        (account_id,),
    ).fetchone()[0]
    
    # 默认查最近 7 天
    start_ts = int((now - timedelta(days=days_back)).timestamp() * 1000)
    if not last_sync:
        start_ts = int((now - timedelta(minutes=10)).timestamp() * 1000)
    if last_sync:
        # 转换为毫秒时间戳
        try:
            last_dt = datetime.fromisoformat(str(last_sync).replace('Z', '+00:00'))
            start_ts = max(start_ts, int(last_dt.timestamp() * 1000))
        except:
            pass
    
    params = {"limit": 1000, "startTime": start_ts}
    
    try:
        inc_data = ex._request("GET", "/fapi/v1/income", signed=True, params=params)
        if not isinstance(inc_data, list):
            logger.warning(f"Income API 返回异常: {type(inc_data)}")
            conn.close()
            return

        ledger_count = 0
        for item in inc_data:
            try:
                upsert_exchange_income(item)
                ledger_count += 1
            except Exception as e:
                logger.warning(f"income ledger upsert error: {e}")

        real_pnl = [i for i in inc_data if i.get("incomeType") == "REALIZED_PNL"]
        funding = [i for i in inc_data if i.get("incomeType") == "FUNDING_FEE"]
        commission = [i for i in inc_data if i.get("incomeType") == "COMMISSION"]

        # 汇总按币种
        by_symbol = defaultdict(float)
        for i in real_pnl:
            sym = i.get("symbol", "")
            income = float(i.get("income", 0))
            by_symbol[sym] += income

        logger.info(f"Income sync: {len(real_pnl)} PnL, {len(funding)} funding, {len(commission)} commission, ledger={ledger_count}")
        
        # 打印按币种汇总
        for sym, pnl in sorted(by_symbol.items(), key=lambda x: -x[1]):
            logger.info(f"  {sym}: {pnl:.2f}")

        # REALIZED_PNL → fills 表
        fill_count = 0
        for i in real_pnl:
            try:
                sym = i.get("symbol", "")
                ts = int(i["time"])
                income = float(i["income"])
                trade_id = i.get("tradeId", "")
                # 检查是否已存在
                stored_trade_id = f"A{account_id}:{trade_id}" if trade_id else ""
                exists = conn.execute(
                    "SELECT id FROM fills WHERE account_id=? AND trade_id=?", (account_id, stored_trade_id)
                ).fetchone()
                if not exists:
                    insert_fill(sym, None, "REALIZED_PNL", abs(income), 0, income, 0, "USDT", trade_id)
                    fill_count += 1
            except Exception as e:
                logger.warning(f"insert_fill error: {e}")

        logger.info(f"写入 {fill_count} 条 fills")

        # 对账
        try:
            backfill_income_ledger_from_fills()
            rebuilt = rebuild_position_trades_from_income()
            logger.info(f"Position trade rebuild complete: {rebuilt} rows")
            _apply_recent_alpha_close_cooldowns()
            try:
                from shared.policy_loop import (
                    review_position_trade_entries,
                    review_position_trade_exits,
                    summarize_entry_reviews,
                    summarize_exit_reviews,
                )

                entry_reviewed = review_position_trade_entries(limit=300)
                entry_summarized = summarize_entry_reviews(recent_limit=30)
                reviewed = review_position_trade_exits(limit=200)
                summarized = summarize_exit_reviews(window_days=7, recent_limit=30)
                logger.info(
                    "Position review complete: entries=%s entry_summaries=%s exits=%s exit_summaries=%s",
                    entry_reviewed.get("reviewed", 0),
                    len(entry_summarized.get("summaries", [])),
                    reviewed.get("reviewed", 0),
                    len(summarized.get("summaries", [])),
                )
            except Exception as e:
                logger.warning(f"exit review sync error: {e}")
        except Exception as e:
            logger.warning(f"position trade rebuild error: {e}")

        total_income_pnl = sum(float(i["income"]) for i in real_pnl)
        local_pnl = conn.execute(
            "SELECT SUM(pnl) FROM trades WHERE account_id=? AND source IN ('system','income_auto')",
            (account_id,),
        ).fetchone()[0] or 0
        diff = abs(total_income_pnl - local_pnl)
        if diff > 1.0:
            logger.warning(f"⚠️ 对账差异: trades表=${local_pnl:.2f} vs incomeAPI=${total_income_pnl:.2f} 差${diff:.2f}")
        else:
            logger.info(f"✅ 对账一致: trades表=${local_pnl:.2f} ≈ incomeAPI=${total_income_pnl:.2f}")

    except Exception as e:
        logger.warning(f"Income sync error: {e}")
    finally:
        conn.close()
    # 不关闭 ex.client，后续循环还要用


def _apply_recent_alpha_close_cooldowns(minutes_back=12):
    """Backfill alpha cooldowns for exchange-side closes discovered by income sync."""
    from shared.db import set_alpha_cooldown

    cfg = TRADING_CONFIG.get("alpha_trading") or {}
    post_minutes = int(cfg.get("post_close_cooldown_minutes", 45))
    loss_minutes = int(cfg.get("loss_cooldown_minutes", 120))
    stop_minutes = int(cfg.get("stop_cooldown_minutes", 180))
    conn = get_conn()
    try:
        from shared.db import current_account_id
        account_id = current_account_id()
        rows = conn.execute(
            """
            SELECT pt.symbol,
                   pt.net_pnl,
                   pt.exit_reason,
                   pt.exit_time,
                   pt.strategy_source,
                   pt.alpha_symbol,
                   EXISTS(
                       SELECT 1
                       FROM orders o
                       WHERE o.account_id = pt.account_id
                         AND o.symbol = pt.symbol
                         AND COALESCE(o.strategy_source, '') = 'alpha'
                         AND datetime(o.created_at) >= datetime(pt.exit_time, '-6 hours')
                         AND datetime(o.created_at) <= datetime(pt.exit_time, '+5 minutes')
                   ) AS has_alpha_order
            FROM position_trades pt
            WHERE pt.account_id=?
              AND pt.symbol <> 'ACCOUNT'
              AND datetime(pt.exit_time) >= datetime('now', ?)
              AND (
                  COALESCE(pt.strategy_source, '') = 'alpha'
                  OR COALESCE(pt.alpha_symbol, '') <> ''
                  OR pt.exit_reason LIKE 'alpha_%'
                  OR has_alpha_order = 1
              )
            ORDER BY datetime(pt.exit_time) DESC
            LIMIT 100
            """,
            (account_id, f"-{int(minutes_back)} minutes"),
        ).fetchall()

        for row in rows:
            pnl = float(row["net_pnl"] or 0)
            reason = row["exit_reason"] or "exchange income close"
            reason_l = reason.lower()
            stop_like = "stop" in reason_l or "止损" in reason
            if stop_like:
                cooldown_type, minutes, loss_count = "stop", stop_minutes, 1
            elif pnl < 0:
                cooldown_type, minutes, loss_count = "loss", loss_minutes, 1
            else:
                cooldown_type, minutes, loss_count = "post_close", post_minutes, 0
            set_alpha_cooldown(
                row["symbol"],
                cooldown_type,
                f"income close cooldown: pnl={pnl:.2f}; {reason}"[:240],
                minutes,
                loss_count=loss_count,
            )
        if rows:
            logger.info("Applied alpha close cooldowns from income sync: %s", len(rows))
    except Exception as e:
        logger.warning("apply alpha close cooldowns failed: %s", e)
    finally:
        conn.close()


def _get_cat_name_by_threshold(th: float) -> str:
    """通过 token_map 反查类别名"""
    import json
    from pathlib import Path
    p = Path(__file__).parent.parent / "strategies" / "token_profiles.json"
    if p.exists():
        try:
            with open(p) as f:
                cfg = json.load(f)
            _tm = cfg.get("token_map", {})
            # 使用第一个已知币种反查
            for sym, cat in _tm.items():
                return cat
        except:
            pass
    return "其他"


def _log_category_ranking(top_symbols, positions, pending_symbols):
    """按类别打印评分排序 + 每类Top 5未开原因"""
    try:
        cfg = get_category_config()
        CAT_ORDER = ["蓝筹", "基本面", "叙事/庄股", "Meme/超高风险"]
        pos_symbols = {p.get('symbol','').upper() for p in positions}
        pending_set = {s.upper() for s in pending_symbols}

        groups = {}
        for r in top_symbols:
            sym = r["symbol"].upper().replace('USDT', '')
            entry = cfg.get(sym)
            if not entry:
                continue
            cat_name = _get_cat_name_by_threshold(entry.get("threshold"))
            if cat_name not in groups:
                groups[cat_name] = []
            groups[cat_name].append((r, entry.get("threshold", 50)))

        for cat_name in CAT_ORDER:
            items = groups.get(cat_name, [])
            if not items:
                continue
            items.sort(key=lambda x: -x[0]["composite_score"])

            logger.info(f"  📊 {cat_name} (Top {min(5, len(items))}/{len(items)}):")
            for idx in range(min(5, len(items))):
                r, th = items[idx]
                sym = r["symbol"]
                score = r["composite_score"]

                # 判定未开原因
                if sym.upper() in pos_symbols:
                    reason = "📌 已持仓"
                elif sym.upper() in pending_set:
                    reason = "✅ 本次开仓"
                elif score < th:
                    reason = f"评分{score:.1f}<阈{th}"
                elif r.get("volatility_level") in ("偏高", "极高"):
                    reason = f"波动率{r.get('volatility_level')}"
                elif r.get("price_position") in ("高位", "overbought"):
                    reason = f"价格位{r.get('price_position')}"
                elif r.get("relative_strength", 50) < 30:
                    reason = f"相对强度{r.get('relative_strength', 0):.0f}<30"
                else:
                    reason = "其他过滤"

                logger.info(f"    #{idx+1} {sym} {score:.1f}/阈{th} | 强度{r.get('relative_strength',50):.0f} | 波动{r.get('volatility_level','?')} | 位{r.get('price_position','?')} → {reason}")
    except Exception:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(trading_loop())
