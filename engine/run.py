"""AlphaDog Scoring Engine — main runner (SQLite)"""
import asyncio
import json
import logging
import sys, os
from datetime import datetime, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.db import (
    fetch_klines_1h, fetch_klines_15m, fetch_klines_6h, fetch_klines_24h, fetch_futures, fetch_onchain,
    fetch_active_symbols, fetch_historical_scores, fetch_price_history,
    insert_scores, insert_backtest, insert_factor_performance,
    label_signal_outcomes, update_training_sample_returns,
    get_conn, init_db, close_conn
)
from engine.scoring import ScoringEngine
from engine.backtest_advanced import WalkForwardBacktest, MonteCarloRisk, ExitOptimizer, EventDrivenBacktest
from shared.strategy_learning import generate_policy_candidates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("engine")


def rows_to_df(rows, cols):
    if not rows:
        return pd.DataFrame()
    data = [{k: r[k] for k in cols} for r in rows]
    return pd.DataFrame(data)


def generate_backtest_review(conn, auto_tune_records=None):
    rows = conn.execute(
        """SELECT symbol, grade, grade_score, grade_time, max_drawdown,
                  return_6h, return_12h, return_24h, return_48h, win_12h, win_24h
           FROM backtest_results
           WHERE datetime(substr(replace(grade_time, 'T', ' '), 1, 19)) >= datetime('now', '-1 day')
           ORDER BY datetime(substr(replace(grade_time, 'T', ' '), 1, 19)) DESC"""
    ).fetchall()
    trades = []
    try:
        trades = conn.execute(
            """SELECT symbol, side, pnl_pct, pnl, exit_reason, entry_time, exit_time,
                      grade_at_entry, score_at_entry
               FROM trades
               WHERE datetime(substr(replace(exit_time, 'T', ' '), 1, 19)) >= datetime('now', '-1 day')
               ORDER BY datetime(substr(replace(exit_time, 'T', ' '), 1, 19)) DESC"""
        ).fetchall()
    except Exception:
        trades = []

    total_samples = len(rows)
    gave_space_5pct = sum(1 for r in rows if max([v for v in (r["return_6h"], r["return_12h"], r["return_24h"], r["return_48h"]) if v is not None] or [0]) >= 0.05)
    had_drawdown_8pct = sum(1 for r in rows if abs(r["max_drawdown"] or 0) >= 0.08)
    review = {
        "run_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_signals": total_samples,
        "total_trades": len(trades),
        "summary": {
            "overview": {
                "total_samples": total_samples,
                "gave_space_5pct": gave_space_5pct,
                "had_drawdown_8pct": had_drawdown_8pct,
                "trade_count": len(trades),
            }
        },
        "entry_issues": [],
        "exit_issues": [],
        "good_exits": [],
        "live_trades": [
            {
                "symbol": t["symbol"],
                "side": t["side"],
                "pnl_pct": round(t["pnl_pct"] or 0, 2),
                "pnl": round(t["pnl"] or 0, 2),
                "exit_reason": t["exit_reason"],
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "grade": t["grade_at_entry"],
                "score": t["score_at_entry"],
            }
            for t in trades
        ],
        "rules": [],
    }

    for r in rows:
        returns = [v for v in (r["return_6h"], r["return_12h"], r["return_24h"], r["return_48h"]) if v is not None]
        if not returns:
            continue
        max_gain = max(returns)
        max_loss = abs(r["max_drawdown"] or 0)
        raw_dd = float(r["max_drawdown"] or 0)
        ret24 = r["return_24h"]
        if ret24 is None or (abs(ret24) < 0.0001 and max_gain < 0.01 and max_loss < 0.01):
            continue
        entry_quality = "需要改进"
        if max_gain >= 0.035 and max_loss < 0.08:
            entry_quality = "基本正确"
        elif max_gain >= 0.07 and max_loss < 0.12:
            entry_quality = "可接受"
        exit_quality = "基本正确"
        if ret24 > 0.035 or (max_gain >= 0.05 and ret24 > 0.015):
            exit_quality = "偏早"
        elif ret24 < -0.03 or max_loss >= 0.06:
            exit_quality = "保护有效"
        item = {
            "symbol": r["symbol"],
            "grade": r["grade"],
            "score": round(r["grade_score"] or 0, 1),
            "grade_time": r["grade_time"],
            "max_gain_pct": round(max_gain * 100, 2),
            "max_dd_pct": round((r["max_drawdown"] or 0) * 100, 2),
            "ret_6h_pct": round((r["return_6h"] or 0) * 100, 2),
            "ret_24h_pct": round(ret24 * 100, 2),
            "entry_quality": entry_quality,
            "exit_quality": exit_quality,
        }
        if entry_quality == "需要改进":
            item["reason"] = "入场后空间不足或回撤偏大"
            item["_severity"] = abs(min(ret24 or 0, 0)) * 100 + abs(min(raw_dd, 0)) * 50 + max(0, 0.015 - max_gain) * 100
            item["_type"] = "entry"
            review["entry_issues"].append(item)
        elif exit_quality == "偏早":
            item["reason"] = "信号后仍有上行空间，退出可能偏早"
            item["_severity"] = max_gain * 100 + (ret24 or 0) * 50
            item["_type"] = "exit"
            review["exit_issues"].append(item)
        elif exit_quality == "保护有效":
            item["reason"] = "后续走弱或回撤偏大，平仓保护有效"
            item["_severity"] = max_loss * 100
            review["good_exits"].append(item)

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

    issue_pool = review["entry_issues"] + review["exit_issues"]
    sorted_issues = sorted(issue_pool, key=lambda x: x.get("_severity", 0), reverse=True)
    unique_issues = []
    seen_symbols = set()
    for item in sorted_issues:
        symbol_key = item.get("symbol")
        if symbol_key in seen_symbols:
            continue
        seen_symbols.add(symbol_key)
        unique_issues.append(item)
        if len(unique_issues) >= 10:
            break
    if len(unique_issues) < 10:
        seen_ids = {(x.get("symbol"), x.get("grade_time"), x.get("_type")) for x in unique_issues}
        for item in sorted_issues:
            item_id = (item.get("symbol"), item.get("grade_time"), item.get("_type"))
            if item_id in seen_ids:
                continue
            unique_issues.append(item)
            seen_ids.add(item_id)
            if len(unique_issues) >= 10:
                break
    issue_pool = unique_issues
    review["entry_issues"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in issue_pool if x.get("_type") == "entry"]
    review["exit_issues"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in issue_pool if x.get("_type") == "exit"]
    good_pool = live_good + review["good_exits"]
    unique_good = []
    seen_good_symbols = set()
    for item in sorted(good_pool, key=lambda x: x.get("_severity", 0), reverse=True):
        symbol_key = item.get("symbol")
        if symbol_key in seen_good_symbols:
            continue
        seen_good_symbols.add(symbol_key)
        unique_good.append(item)
        if len(unique_good) >= 5:
            break
    review["good_exits"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in unique_good]
    review["rules"] = [
        {
            "section": "总体判断",
            "text": (
                f"本轮只看最近 1 天样本 {total_samples} 个；"
                f"重点问题只展示 {len(issue_pool)} 个，其中开仓问题 {len(review['entry_issues'])} 个，"
                f"平仓偏早 {len(review['exit_issues'])} 个，有效做法 {len(review['good_exits'])} 个。"
            ),
        },
        {"section": "开仓问题", "text": "最近需要特别注意的是入场后最大浮盈不足、或先出现较大回撤的信号。"},
        {"section": "平仓问题", "text": "偏早退出样本说明尾仓应结合价格回撤、评分、OI 和筹码同步转弱再全平。"},
        {"section": "有效做法", "text": "最近 1 天盈利退出、TP 或保护型退出会进入有效做法，用来保留真正有用的规则。"},
    ]
    if auto_tune_records:
        review["auto_tune"] = {
            "run_time": review["run_time"],
            "records": auto_tune_records,
        }
    return review


def build_factor_analysis_result(factor_rows, run_time):
    by_factor = {}
    for row in factor_rows:
        _, factor_name, bucket, samples, win_rate, avg_return, avg_drawdown, ev, ic, ir = row
        by_factor.setdefault(factor_name, []).append({
            "bucket": bucket,
            "samples": samples,
            "win_rate": win_rate,
            "avg_return": avg_return,
            "avg_drawdown": avg_drawdown,
            "ev": ev,
            "ic": ic,
            "ir": ir,
        })

    current_factors = []
    recommendations = []
    for name, buckets in by_factor.items():
        usable = [b for b in buckets if (b.get("samples") or 0) >= 5 and b.get("win_rate") is not None]
        if not usable:
            continue
        high = max(usable, key=lambda b: b["win_rate"])
        low = min(usable, key=lambda b: b["win_rate"])
        discrimination = (high["win_rate"] - low["win_rate"]) * 100
        current_factors.append({
            "name": name,
            "discrimination": round(discrimination, 1),
            "high_win_rate": round(high["win_rate"] * 100, 1),
            "low_win_rate": round(low["win_rate"] * 100, 1),
        })
        if discrimination >= 5:
            recommendations.append({
                "factor": name,
                "description": f"{name} 的 {high['bucket']} 桶明显优于 {low['bucket']} 桶",
                "correlation": high.get("ev"),
                "discrimination": round(discrimination, 1),
            })

    current_factors.sort(key=lambda x: abs(x["discrimination"]), reverse=True)
    recommendations.sort(key=lambda x: x["discrimination"], reverse=True)
    high_scores = [f["high_win_rate"] for f in current_factors]
    low_scores = [f["low_win_rate"] for f in current_factors]
    overall = (sum(high_scores) / len(high_scores) - sum(low_scores) / len(low_scores)) if high_scores and low_scores else 0
    return {
        "run_time": run_time,
        "total_signals": sum(int(r[3] or 0) for r in factor_rows),
        "current_factors": current_factors,
        "candidate_recommendations": recommendations,
        "category_stats": {},
        "overall_discrimination": round(overall, 1),
    }


def write_factor_analysis_result(conn, result):
    conn.execute("CREATE TABLE IF NOT EXISTS factor_analysis (run_time TEXT, result TEXT)")
    conn.execute(
        "INSERT INTO factor_analysis (run_time, result) VALUES (?, ?)",
        (result["run_time"], json.dumps(result, ensure_ascii=False, default=str)),
    )
    conn.execute("DELETE FROM factor_analysis WHERE rowid NOT IN (SELECT rowid FROM factor_analysis ORDER BY run_time DESC LIMIT 30)")
    conn.commit()


def write_factor_effectiveness(conn, rows):
    if not rows:
        return
    conn.executemany(
        """INSERT INTO factor_effectiveness
           (run_time, factor_name, layer, profile, bucket, samples,
            win_rate_6h, win_rate_24h, avg_return_6h, avg_return_24h,
            avg_drawdown, ev, ic)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.execute("DELETE FROM factor_effectiveness WHERE run_time < datetime('now', '-30 days')")
    conn.commit()


async def run_scoring():
    engine = ScoringEngine()
    try:
        symbols = fetch_active_symbols()
        if not symbols:
            logger.warning("No symbols")
            return
        logger.info(f"Scoring {len(symbols)} symbols")

        k1h = fetch_klines_1h(symbols)
        k15m = fetch_klines_15m(symbols)
        k6h = fetch_klines_6h(symbols)
        k24h = fetch_klines_24h(symbols)
        fut = fetch_futures(symbols)
        onc = fetch_onchain(symbols)

        df_1h = rows_to_df(k1h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_15m = rows_to_df(k15m, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_6h = rows_to_df(k6h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_24h = rows_to_df(k24h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_fut = rows_to_df(fut, ["time","symbol","open_interest","funding_rate","mark_price"])
        df_onc = rows_to_df(onc, ["time","symbol","chain","cex_net_flow_usd","cex_net_flow_14d_usd","cex_net_outflow_ratio"])

        logger.info(f"Data: 1h={len(df_1h)} 15m={len(df_15m)} 6h={len(df_6h)} 24h={len(df_24h)} fut={len(df_fut)} onc={len(df_onc)}")

        if df_1h.empty:
            logger.warning("No data yet")
            return

        results = engine.score_all(df_1h, df_15m, df_6h, df_24h, df_fut, df_onc)
        logger.info(f"Scored {len(results)}")

        if results:
            import json
            db_rows = [
                (
                    r["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    r["symbol"], r["composite_score"], r["composite_summary"],
                    r["risk_label"], r["chip_phase"], r["trend_state"],
                    r["trend_direction"], r["volatility_level"],
                    r["price_position"], r["relative_strength"],
                    r["market_price"], json.dumps(r["raw_features"], ensure_ascii=False),
                    r["scan_id"],
                    r.get("entry_alpha", 0),  # V3.0
                    r.get("hold_alpha", 0),    # V3.0
                )
                for r in results
            ]
            insert_scores(db_rows)

            top = sorted(results, key=lambda x: -x["composite_score"])[:5]
            for t in top:
                logger.info(f"  #{t['composite_score']:.1f} {t['symbol']} ({t['composite_summary']}) - {t['chip_phase']}")

    except Exception as e:
        logger.error(f"Scoring error: {e}", exc_info=True)


async def run_signal_labeling():
    try:
        count = label_signal_outcomes(max_rows=2000)
        if count:
            logger.info(f"[signal-outcomes] labeled/updated {count} decisions")
    except Exception as e:
        logger.warning(f"[signal-outcomes] failed: {e}")


async def run_backtest():
    engine = ScoringEngine()
    try:
        await run_signal_labeling()
        scores = fetch_historical_scores()
        if not scores:
            logger.info("No scores for backtest")
            return

        cols = ["time","symbol","composite_score","composite_summary","market_price","raw_features"]
        df_scores = pd.DataFrame([dict(zip(cols, [s[c] for c in cols])) for s in scores])
        df_scores["time"] = pd.to_datetime(df_scores["time"], errors="coerce", utc=True)
        df_scores["composite_score"] = pd.to_numeric(df_scores["composite_score"], errors="coerce")
        df_scores["market_price"] = pd.to_numeric(df_scores["market_price"], errors="coerce")
        df_scores = df_scores.dropna(subset=["time", "symbol"])

        symbols = df_scores["symbol"].unique().tolist()
        prices_raw = fetch_price_history(symbols)
        price_cols = ["time_bucket","symbol","close"]
        df_prices = pd.DataFrame([dict(zip(price_cols, [p[c] for c in price_cols])) for p in prices_raw])
        df_prices["time_bucket"] = pd.to_datetime(df_prices["time_bucket"], errors="coerce", utc=True)
        df_prices["close"] = pd.to_numeric(df_prices["close"], errors="coerce")
        df_prices = df_prices.dropna(subset=["time_bucket", "symbol", "close"])

        logger.info(f"Backtest: {len(df_scores)} scores, {len(df_prices)} prices")

        results = engine.compute_backtest(df_scores, df_prices)
        if results:
            db_rows = [
                (
                    r["symbol"], r["grade"], r["grade_score"],
                    r["grade_time"].strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(r["grade_time"], 'strftime') else r["grade_time"],
                    r["price_at_grade"],
                    float(r["return_6h"]) if r["return_6h"] is not None else None,
                    float(r["return_12h"]) if r["return_12h"] is not None else None,
                    float(r["return_24h"]) if r["return_24h"] is not None else None,
                    float(r["return_48h"]) if r["return_48h"] is not None else None,
                    float(r["max_drawdown"]) if r["max_drawdown"] is not None else None,
                    1 if r["win_12h"] else 0 if r["win_12h"] is False else None,
                    1 if r["win_24h"] else 0 if r["win_24h"] is False else None,
                )
                for r in results
            ]
            insert_backtest(db_rows)
            logger.info(f"Saved {len(db_rows)} backtest results")
            try:
                cleanup_conn = get_conn()
                deleted = cleanup_conn.execute(
                    """DELETE FROM backtest_results
                       WHERE return_6h IS NULL
                         AND return_12h IS NULL
                         AND return_24h IS NULL
                         AND return_48h IS NULL"""
                ).rowcount
                cleanup_conn.commit()
                cleanup_conn.close()
                if deleted:
                    logger.info(f"Removed {deleted} immature backtest rows without future returns")
            except Exception as e:
                logger.warning(f"Backtest immature cleanup failed: {e}")

            # ── 🆕 更新 training_samples 的未来收益标签 ──
            try:
                sample_updates = []
                _conn = get_conn()
                for r in results:
                    sym = r["symbol"]
                    gt = r["grade_time"]
                    gt_str = gt.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(gt, 'strftime') else str(gt)
                    scan_row = _conn.execute(
                        "SELECT scan_id FROM alpha_scores WHERE symbol = ? AND time = ? LIMIT 1",
                        (sym, gt_str),
                    ).fetchone()
                    if not scan_row:
                        continue
                    scan_id = scan_row["scan_id"]
                    sample_updates.append((
                        r.get("return_6h"), r.get("return_12h"),
                        r.get("return_24h"), r.get("return_48h"),
                        r.get("max_drawdown"), sym, scan_id,
                    ))
                _conn.close()

                if sample_updates:
                    _conn2 = get_conn()
                    _conn2.executemany(
                        """UPDATE training_samples
                           SET return_6h = ?, return_12h = ?, return_24h = ?, return_48h = ?, max_drawdown = ?
                           WHERE symbol = ? AND scan_id = ?""",
                        sample_updates,
                    )
                    _conn2.commit()
                    _conn2.close()
                    logger.info(f"[training-samples] Updated {len(sample_updates)} return labels")
            except Exception as e:
                logger.warning(f"[training-samples] Return update failed: {e}")

        # ── V3.0 高级回测 ──
        try:
            # Walk Forward 回测
            wf = WalkForwardBacktest(train_days=30, test_days=7, step_days=7)
            wf_result = wf.run(df_scores, df_prices)
            if wf_result:
                logger.info(f"[WalkForward] {len(wf_result.get('windows', []))} windows, oos={'OK' if wf_result.get('oos_result') else 'N/A'}")
            
            # Monte Carlo 风险分析
            conn = get_conn()
            trades = conn.execute("SELECT * FROM trades").fetchall()
            conn.close()
            if trades and len(trades) >= 10:
                mc = MonteCarloRisk(n_simulations=500)
                mc_result = mc.run(trades)
                logger.info(f"[MonteCarlo] p50={mc_result.get('percentiles',{}).get('p50',0):.2f}, worst_10pct={mc_result.get('worst_cases',{}).get('worst_10pct_avg',0):.2f}")
            
            # Exit Optimizer
            if trades and len(trades) >= 10:
                eo = ExitOptimizer()
                eo_result = eo.analyze(trades)
                if "suggestions" in eo_result:
                    for sug in eo_result["suggestions"]:
                        logger.info(f"[ExitOptimizer] {sug}")
            
            # Event Driven Backtest
            edb = EventDrivenBacktest()
            edb_result = edb.run(df_scores, df_prices)
            if edb_result and "error" not in edb_result:
                logger.info(f"[EventDriven] return={edb_result.get('total_return',0):.2f}%, trades={edb_result.get('num_trades',0)}")
                
        except Exception as e:
            logger.warning(f"[AdvancedBacktest] failed: {e}")

        # ── 自动调参 + 生成并写入回测复盘 ──
        auto_tune_records = {}
        try:
            import json
            from pathlib import Path

            # 读取当前配置
            cfg_paths = [
                Path(__file__).parent.parent / "strategies" / "token_profiles.json",
            ]
            cfg = None
            for p in cfg_paths:
                if p.exists():
                    with open(p, encoding="utf-8") as f:
                        cfg = json.load(f)
                    break

            if cfg and results:
                cats = cfg["categories"]
                token_map = cfg["token_map"]

                # 构建 {symbol -> category} 快速查找
                sym_to_cat = {}
                for sym, cat in token_map.items():
                    sym_to_cat[sym.upper()] = cat

                # 按类别收集回测结果
                from collections import defaultdict
                cat_samples = defaultdict(list)
                for r in results:
                    sym = r["symbol"].upper().replace("USDT", "")
                    cat = sym_to_cat.get(sym) or sym_to_cat.get(r["symbol"].upper())
                    if cat:
                        win_24 = r.get("win_24h")
                        if win_24 is not None:
                            cat_samples[cat].append({
                                "score": r["grade_score"],
                                "win_24": win_24,
                            })

                changed = False
                for cat_name, cat_cfg in cats.items():
                    samples = cat_samples.get(cat_name, [])
                    if len(samples) < 30:
                        logger.info(f"  [auto-tune] {cat_name}: 样本{len(samples)} < 30, 跳过")
                        continue

                    current_threshold = cat_cfg["score_threshold"]

                    best_threshold = current_threshold
                    best_win_rate = 0
                    best_count = 0
                    best_objective = -999

                    low = max(35, current_threshold - 10)
                    high = min(85, current_threshold + 10)
                    for th in range(low, high + 1):
                        filtered = [s for s in samples if s["score"] >= th]
                        if len(filtered) < 15:
                            continue
                        win_count = sum(1 for s in filtered if s["win_24"])
                        win_rate = win_count / len(filtered) * 100
                        sample_penalty = max(0, 30 - len(filtered)) * 0.25
                        distance_penalty = abs(th - current_threshold) * 0.15
                        objective = win_rate - sample_penalty - distance_penalty
                        if objective > best_objective:
                            best_objective = objective
                            best_win_rate = win_rate
                            best_threshold = th
                            best_count = len(filtered)

                    if best_threshold > current_threshold:
                        best_threshold = min(best_threshold, current_threshold + 3)
                    elif best_threshold < current_threshold:
                        best_threshold = max(best_threshold, current_threshold - 3)

                    record = {
                        "old_threshold": current_threshold,
                        "new_threshold": best_threshold,
                        "win_rate": round(best_win_rate, 1),
                        "samples": best_count,
                    }

                    if best_threshold != current_threshold:
                        old = cat_cfg["score_threshold"]
                        cat_cfg["score_threshold"] = best_threshold
                        changed = True
                        record["adjusted"] = True
                        logger.info(f"  [auto-tune] {cat_name}: 阈{old}→{best_threshold} (胜率{best_win_rate:.1f}% 样本{best_count})")
                    else:
                        record["adjusted"] = False
                        logger.info(f"  [auto-tune] {cat_name}: 保持{current_threshold} (胜率{best_win_rate:.1f}% 样本{best_count})")

                    auto_tune_records[cat_name] = record

                if changed:
                    cfg_path = cfg_paths[0]
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    logger.info(f"  [auto-tune] ✅ 自动调整阈值已写入 {cfg_path}")
                else:
                    logger.info(f"  [auto-tune] ✅ 当前阈值最优，无需调整")
        except Exception as e:
            logger.warning(f"[auto-tune] 调参失败: {e}")

        # 生成并写入回测复盘（带上调参记录）
        try:
            conn = get_conn()
            run_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            review_data = generate_backtest_review(conn, auto_tune_records)
            review_json = json.dumps(review_data, default=str, ensure_ascii=False)
            conn.execute("""
                INSERT INTO backtest_review (run_time, review_json)
                VALUES (?, ?)
            """, (run_now, review_json))
            conn.execute("""
                DELETE FROM backtest_review
                WHERE run_time < datetime('now', '-14 days')
            """)
            conn.commit()
            logger.info(f"Backtest review saved: {run_now}")
            try:
                candidate_ids = generate_policy_candidates(review=review_data)
                if candidate_ids:
                    logger.info(f"Generated/updated {len(candidate_ids)} strategy learning candidates from review")
            except Exception as e:
                logger.warning(f"Strategy learning candidate generation failed: {e}")
            conn.close()
        except Exception as e:
            logger.warning(f"Backtest review save failed: {e}")

        # ── 🔧 因子归因分析 ──
        try:
            matured_results = [
                r for r in results
                if any(r.get(k) is not None for k in ("return_6h", "return_12h", "return_24h", "return_48h"))
            ] if results else []
            if matured_results:
                factor_rows = compute_factor_performance(matured_results, df_scores)
                if factor_rows:
                    insert_factor_performance(factor_rows)
                    effectiveness_rows = compute_layer_effectiveness(matured_results, df_scores)
                    factor_result = build_factor_analysis_result(factor_rows, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    conn = get_conn()
                    write_factor_effectiveness(conn, effectiveness_rows)
                    write_factor_analysis_result(conn, factor_result)
                    conn.close()
                    try:
                        candidate_ids = generate_policy_candidates(factor_result=factor_result)
                        if candidate_ids:
                            logger.info(f"Generated/updated {len(candidate_ids)} strategy learning candidates from factors")
                    except Exception as e:
                        logger.warning(f"Factor learning candidate generation failed: {e}")
                    logger.info(f"Saved {len(factor_rows)} factor performance records")
            elif results:
                logger.info("Factor analysis skipped: no matured backtest returns yet")
        except Exception as e:
            logger.warning(f"Factor analysis error: {e}")

        # ── 🔧 24h 因子权重调整 ──
        try:
            adjust_weights_24h()
        except Exception as e:
            logger.warning(f"Weight adjustment error: {e}")

    except Exception as e:
        logger.error(f"Backtest error: {e}", exc_info=True)


# ── 🔧 因子归因分析 ──

def compute_factor_performance(results, df_scores):
    """根据回测结果计算因子归因"""
    import numpy as np
    from collections import defaultdict
    factor_buckets = defaultdict(lambda: defaultdict(list))
    for r in results:
        sym = r["symbol"]
        gt = r["grade_time"]
        score_rows = df_scores[df_scores["symbol"] == sym]
        if score_rows.empty:
            continue
        if isinstance(gt, (pd.Timestamp, datetime)):
            score_rows = score_rows.copy()
            score_rows["_diff"] = abs(pd.to_datetime(score_rows["time"]) - pd.to_datetime(gt))
            nearest = score_rows.sort_values("_diff").iloc[0] if not score_rows.empty else None
        else:
            nearest = score_rows.iloc[-1] if not score_rows.empty else None
        if nearest is None:
            continue
        ret_12h = (
            r.get("return_24h")
            if r.get("return_24h") is not None
            else r.get("return_12h")
            if r.get("return_12h") is not None
            else r.get("return_6h")
        )
        mdd = r.get("max_drawdown", 0)
        if ret_12h is None:
            continue
        win = 1 if ret_12h > 0 else 0
        score = r.get("grade_score", 50)
        sb = f"{int(score//10*10)}-{int(score//10*10+9)}"
        factor_buckets["composite_score"][sb].append({"ret": ret_12h, "mdd": mdd, "win": win})
        rf = nearest.get("raw_features", {})
        if isinstance(rf, str):
            import json; rf = json.loads(rf)
        tech = rf.get("technical", {}); fut = rf.get("futures", {})
        rsi = tech.get("rsi_14", 50)
        rb = "<30" if rsi < 30 else "30-40" if rsi < 40 else "40-50" if rsi < 50 else "50-60" if rsi < 60 else "60-70" if rsi < 70 else ">=70"
        factor_buckets["rsi"][rb].append({"ret": ret_12h, "mdd": mdd, "win": win})
        fr = fut.get("funding_rate", 0)
        fb = "<-0.1%" if fr < -0.001 else "-0.1%~-0.05%" if fr < -0.0005 else "-0.05%~+0.05%" if fr < 0.0005 else "0.05%~0.1%" if fr < 0.001 else ">0.1%"
        factor_buckets["funding_rate"][fb].append({"ret": ret_12h, "mdd": mdd, "win": win})
        ch = tech.get("chip_phase", "中性震荡")
        factor_buckets["chip"][ch].append({"ret": ret_12h, "mdd": mdd, "win": win})
    rows = []
    for fn, buckets in factor_buckets.items():
        for bucket, samples in buckets.items():
            if len(samples) < 5: continue
            n = len(samples)
            wr = sum(s["win"] for s in samples) / n
            avg_ret = float(np.mean([s["ret"] for s in samples]))
            avg_mdd = float(np.mean([s["mdd"] for s in samples]))
            aw = float(np.mean([s["ret"] for s in samples if s["win"]])) if wr > 0 else 0
            al = float(np.mean([s["ret"] for s in samples if not s["win"]])) if wr < 1 else 0
            ev = wr * aw - (1 - wr) * al
            rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        fn, bucket, n, round(wr, 3), round(avg_ret, 4),
                        round(avg_mdd, 4), round(ev, 4), None, None))
    return rows


def _bucket_score(value):
    try:
        value = float(value)
    except Exception:
        return None
    value = max(0, min(100, value))
    lo = int(value // 20 * 20)
    hi = min(100, lo + 20)
    return f"{lo}-{hi}"


def compute_layer_effectiveness(results, df_scores):
    import numpy as np
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in results:
        sym = r["symbol"]
        gt = r["grade_time"]
        score_rows = df_scores[df_scores["symbol"] == sym]
        if score_rows.empty:
            continue
        score_rows = score_rows.copy()
        score_rows["_diff"] = abs(pd.to_datetime(score_rows["time"]) - pd.to_datetime(gt))
        nearest = score_rows.sort_values("_diff").iloc[0]
        rf = nearest.get("raw_features", {})
        if isinstance(rf, str):
            try:
                rf = json.loads(rf)
            except Exception:
                rf = {}
        tech = rf.get("technical", {}) or {}
        score_layers = rf.get("score_layers", {}) or {}
        profile = score_layers.get("profile") or "default"
        layers = score_layers.get("layers") or {}
        values = {
            "chip_score": ("structure", tech.get("chip_score")),
            "absorption_score": ("structure", tech.get("absorption_score", tech.get("abs_score"))),
            "support_score": ("structure", tech.get("support_score")),
            "relative_strength": ("opportunity", nearest.get("relative_strength", 50)),
            "opportunity_score": ("opportunity", (layers.get("opportunity") or {}).get("score")),
            "entry_score": ("entry", (layers.get("entry") or {}).get("score")),
            "risk_score": ("risk", (layers.get("risk") or {}).get("score")),
            "execution_score": ("execution", (layers.get("execution") or {}).get("score")),
        }
        for factor_name, (layer, value) in values.items():
            bucket = _bucket_score(value)
            if not bucket:
                continue
            buckets[(factor_name, layer, profile, bucket)].append({
                "ret_6h": r.get("return_6h"),
            "ret_24h": r.get("return_24h") if r.get("return_24h") is not None else r.get("return_12h") if r.get("return_12h") is not None else r.get("return_6h"),
                "mdd": r.get("max_drawdown", 0),
            })
    rows = []
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for (factor_name, layer, profile, bucket), samples in buckets.items():
        if len(samples) < 5:
            continue
        ret6 = [float(s["ret_6h"]) for s in samples if s.get("ret_6h") is not None]
        ret24 = [float(s["ret_24h"]) for s in samples if s.get("ret_24h") is not None]
        mdds = [float(s.get("mdd") or 0) for s in samples]
        wr6 = sum(1 for x in ret6 if x > 0) / len(samples)
        wr24 = sum(1 for x in ret24 if x > 0) / len(samples)
        avg6 = float(np.mean(ret6)) if ret6 else 0
        avg24 = float(np.mean(ret24)) if ret24 else 0
        avg_mdd = float(np.mean(mdds)) if mdds else 0
        ev = wr24 * avg24 - (1 - wr24) * abs(avg_mdd)
        rows.append((run_time, factor_name, layer, profile, bucket, len(samples),
                     round(wr6, 3), round(wr24, 3), round(avg6, 4), round(avg24, 4),
                     round(avg_mdd, 4), round(ev, 4), None))
    return rows


# ── 🔧 24h 自动权重调整 ──

def adjust_weights_24h():
    """每24h根据因子IC调整权重"""
    import json
    from pathlib import Path
    from collections import defaultdict
    weights_path = Path(__file__).parent / "factor_weights.json"
    if not weights_path.exists():
        return
    with open(weights_path, encoding="utf-8") as f:
        weights = json.load(f)
    sub_w = weights.get("sub_weights", {})
    if not sub_w:
        return
    conn = get_conn()
    recent = conn.execute(
        "SELECT factor_name, bucket, samples, win_rate, avg_return, ev FROM factor_performance WHERE run_time > datetime('now', '-25 hours') ORDER BY run_time DESC"
    ).fetchall()
    conn.close()
    if len(recent) < 20:
        return
    factor_evs = defaultdict(list)
    for r in recent:
        factor_evs[r["factor_name"]].append(r["ev"] if r["ev"] is not None else 0)
    factor_avg_ev = {fn: sum(evs)/len(evs) for fn, evs in factor_evs.items()}
    name_map = {"composite_score": "composite", "rsi": "rsi", "funding_rate": "funding", "chip": "chip"}
    changed = False
    for fn, ev in factor_avg_ev.items():
        mapped = name_map.get(fn)
        if mapped and mapped in sub_w:
            if ev > 0.05:
                sub_w[mapped] = min(0.35, sub_w[mapped] * 1.1); changed = True
            elif ev < -0.02:
                sub_w[mapped] = max(0.05, sub_w[mapped] * 0.5); changed = True
    if changed:
        weights["sub_weights"] = sub_w
        with open(weights_path, "w", encoding="utf-8") as f:
            json.dump(weights, f, ensure_ascii=False, indent=2)
        logger.info(f"[weight-adjust] Factor weights adjusted")
    else:
        logger.info(f"[weight-adjust] Weights optimal, no change")


async def main():
    logger.info("AlphaDog Engine starting...")
    init_db()  # 确保所有表存在

    sched = AsyncIOScheduler()
    sched.add_job(run_scoring, "interval", minutes=5, id="scoring",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc))
    sched.add_job(run_signal_labeling, "interval", minutes=5, id="signal_labeling",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc))
    sched.add_job(run_backtest, "interval", hours=1, id="backtest",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc).replace(minute=5))
    sched.start()
    logger.info("Engine scheduler started")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    sched.shutdown()
    logger.info("Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
