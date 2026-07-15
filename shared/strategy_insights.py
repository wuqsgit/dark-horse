import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


AI_METRICS = (
    "futures_volume_growth_6h",
    "trend_score",
    "spread_pct",
    "pullback_from_high_pct",
    "oi_change_4h",
    "funding_rate",
)


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values):
    numbers = [_num(value) for value in values if value is not None]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _pct(value):
    return f"{_num(value) * 100:.1f}%"


def _money(value):
    return f"{_num(value):.2f}U"


def _has_table(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn, table):
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _fetch_trade_rows(main_db_path, limit):
    if not Path(main_db_path).exists():
        return []
    conn = _connect(main_db_path)
    try:
        if not _has_table(conn, "trade_entry_reviews"):
            return []
        columns = _table_columns(conn, "trade_entry_reviews")
        needed = {
            "position_trade_id", "symbol", "strategy_source", "category", "entry_template",
            "review_label", "net_pnl", "max_favorable_return", "max_adverse_return",
            "entry_time", "exit_time",
        }
        select_cols = [col for col in needed if col in columns]
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM trade_entry_reviews ORDER BY rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _fetch_ai_rows(ai_db_path, limit):
    if not Path(ai_db_path).exists():
        return []
    conn = _connect(ai_db_path)
    try:
        if not _has_table(conn, "entry_quality_samples"):
            return []
        rows = conn.execute(
            """SELECT model_key, symbol, template, category, label, mfe_r, mae_r, features_json
               FROM entry_quality_samples
               WHERE label_status='ready' AND label IS NOT NULL
               ORDER BY datetime(observed_at) DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["features"] = _loads(item.pop("features_json", None), {})
            result.append(item)
        return result
    finally:
        conn.close()


def _trade_conclusion(category, template, bad):
    avg_mfe = _mean([item.get("max_favorable_return") for item in bad])
    avg_mae = _mean([item.get("max_adverse_return") for item in bad])
    loss = sum(_num(item.get("net_pnl")) for item in bad)
    if avg_mfe >= 0.08 and loss < 0:
        return (
            f"{category}/{template} 的主要问题是浮盈回吐：问题样本平均 "
            f"MFE {_pct(avg_mfe)}，MAE {_pct(avg_mae)}，合计盈亏 {_money(loss)}。"
        )
    return (
        f"{category}/{template} 问题样本平均 MFE {_pct(avg_mfe)}，"
        f"MAE {_pct(avg_mae)}，合计盈亏 {_money(loss)}。"
    )


def _trade_recommendation(source, category, template, bad, cases):
    avg_mfe = _mean([item.get("max_favorable_return") for item in bad])
    avg_mae = _mean([item.get("max_adverse_return") for item in bad])
    case_text = "; ".join(case["action"] for case in cases[:2])
    category_u = str(category or "").lower()
    template_u = str(template or "").lower()
    source_u = str(source or "").lower()

    if source_u == "alpha" and "probe" in template_u:
        base = (
            "Alpha 试探仓：只保留试探仓位；趋势分低于 82，或试探超时前没有走到 1R，"
            "都不允许加仓，直接减仓或退出。"
        )
    elif source_u == "alpha":
        base = (
            "Alpha 趋势仓：TP1 后必须把剩余仓位同步到交易所保护止损；"
            "如果趋势分或量能衰减，先减 25%-30%，不要等硬止损兜底。"
        )
    elif category_u in {"discovery", "narrative"}:
        base = (
            "discovery/题材币：MFE 超过 8% 后锁住 30%-50% 浮盈；"
            "若从最高浮盈回撤超过一半，再减 25%；没有交易所保护止损，不允许隔夜持仓。"
        )
    elif category_u in {"core_bluechip", "bluechip"}:
        base = (
            "蓝筹仓：不要追快速拉升；只在回踩 EMA 后重新站稳、1h 趋势未破时进场；"
            "用更宽的 ATR 保护，避免过窄止损反复磨损。"
        )
    elif category_u == "fundamental":
        base = (
            "基本面仓：不要追消息面第一波拉升，等回踩确认；"
            "如果 MFE 不到 4% 且 MAE 扩大到 3% 以上，同类机会下一次降级为观察。"
        )
    else:
        base = (
            f"{category}/{template}：平均 MFE {_pct(avg_mfe)}、MAE {_pct(avg_mae)}，"
            "先降低仓位，并提高入场确认强度。"
        )

    if avg_mfe >= 0.08 and avg_mae <= -0.05:
        base += " 这类样本有明显先赚后亏特征，优先修复盈利保护，而不是继续放宽入场。"
    if case_text:
        base += f" 代表样本动作：{case_text}"
    return base


def _representative_trade_cases(source, category, template, bad, limit=3):
    cases = []
    seen_symbols = set()
    for item in sorted(bad, key=lambda row: _num(row.get("net_pnl"))):
        symbol = item.get("symbol") or "-"
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        pnl = _num(item.get("net_pnl"))
        mfe = _num(item.get("max_favorable_return"))
        mae = _num(item.get("max_adverse_return"))
        diagnosis = f"{symbol}：盈亏 {_money(pnl)}，MFE {_pct(mfe)}，MAE {_pct(mae)}"
        if mfe >= 0.08 and pnl < 0:
            action = (
                f"{symbol} 属于高浮盈回吐样本：TP1 后或 MFE>8% 时，必须在交易所挂保本上方保护止损；"
                "若浮盈回吐超过 50%，再减 25%。"
            )
        elif mae <= -0.08:
            action = (
                f"{symbol} 逆向波动过深：初始仓位减半；盘口价差/深度弱，或首根 15m K 线守不住入场价时拒绝开仓。"
            )
        elif "probe" in str(template or "").lower():
            action = (
                f"{symbol} 试探仓没有扩展：只保留试探仓；试探超时前没有 1R 进展就退出。"
            )
        else:
            action = (
                f"{symbol} 弱样本：开仓前要求更强趋势/放量确认；该分组改善前降低仓位。"
            )
        cases.append({
            "symbol": symbol,
            "net_pnl": pnl,
            "max_favorable_return": mfe,
            "max_adverse_return": mae,
            "diagnosis": diagnosis,
            "action": action,
        })
        if len(cases) >= limit:
            break
    return cases


def _trade_insights(rows, min_samples):
    groups = defaultdict(list)
    for row in rows:
        source = row.get("strategy_source") or "unknown"
        category = row.get("category") or ("alpha" if source == "alpha" else "unknown")
        template = row.get("entry_template") or "unknown"
        groups[(source, category, template)].append(row)

    insights = []
    for (source, category, template), items in groups.items():
        if len(items) < min_samples:
            continue
        bad = [
            item for item in items
            if str(item.get("review_label") or "").lower() in {"bad_condition", "chased", "early"}
            or _num(item.get("net_pnl")) < 0
        ]
        if len(bad) < max(2, int(len(items) * 0.4)):
            continue
        symbols = []
        for item in sorted(bad, key=lambda row: _num(row.get("net_pnl"))):
            symbol = item.get("symbol")
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        issue_rate = len(bad) / len(items)
        total_pnl = sum(_num(item.get("net_pnl")) for item in bad)
        priority = "\u6025\u9700\u4fee\u590d" if issue_rate >= 0.5 or total_pnl <= -5 else "\u5efa\u8bae\u4f18\u5316"
        representative_cases = _representative_trade_cases(source, category, template, bad)
        avg_mfe = _mean([item.get("max_favorable_return") for item in bad])
        avg_mae = _mean([item.get("max_adverse_return") for item in bad])
        insights.append({
            "source": "real_trades",
            "priority": priority,
            "strategy_source": source,
            "category": category,
            "template": template,
            "sample_size": len(items),
            "issue_count": len(bad),
            "issue_rate": issue_rate,
            "representative_symbols": symbols[:3],
            "representative_cases": representative_cases,
            "key_metrics": ["max_favorable_return", "max_adverse_return", "net_pnl"],
            "evidence": (
                f"{category}/{template}：最近 {len(items)} 笔里有 {len(bad)} 笔问题样本；"
                f"问题样本合计 {total_pnl:.2f}U，平均 MFE {avg_mfe:.2%}，"
                f"平均 MAE {avg_mae:.2%}。"
            ),
            "conclusion": _trade_conclusion(category, template, bad),
            "recommendation": _trade_recommendation(source, category, template, bad, representative_cases),
            "confidence": min(0.95, 0.45 + issue_rate * 0.4 + min(len(items), 20) / 100),
        })
    return insights

def _ai_insights(rows, min_samples):
    groups = defaultdict(list)
    for row in rows:
        source = row.get("model_key") or "unknown"
        category = row.get("category") or source
        template = row.get("template") or "unknown"
        groups[(source, category, template)].append(row)

    insights = []
    for (source, category, template), items in groups.items():
        if len(items) < min_samples:
            continue
        winners = [item for item in items if int(item.get("label") or 0) == 1]
        losers = [item for item in items if int(item.get("label") or 0) == 0]
        if len(winners) < 2 or len(losers) < 2:
            continue

        metric_diffs = []
        for metric in AI_METRICS:
            win_avg = _mean([(item.get("features") or {}).get(metric) for item in winners])
            loss_avg = _mean([(item.get("features") or {}).get(metric) for item in losers])
            diff = win_avg - loss_avg
            if abs(diff) > 0:
                metric_diffs.append({
                    "metric": metric,
                    "winner_avg": win_avg,
                    "loser_avg": loss_avg,
                    "diff": diff,
                    "strength": abs(diff) / (abs(loss_avg) + 1e-9),
                })
        metric_diffs = sorted(metric_diffs, key=lambda item: item["strength"], reverse=True)[:3]
        if not metric_diffs:
            continue
        key_metrics = [item["metric"] for item in metric_diffs]
        win_rate = len(winners) / len(items)
        recommendation = _ai_recommendation(source, category, key_metrics)
        insights.append({
            "source": "ai_candidates",
            "priority": "建议加强" if win_rate >= 0.45 else "继续观察",
            "strategy_source": source,
            "category": category,
            "template": template,
            "sample_size": len(items),
            "issue_count": len(losers),
            "issue_rate": len(losers) / len(items),
            "representative_symbols": _representative_ai_symbols(losers),
            "key_metrics": key_metrics,
            "metric_diffs": metric_diffs,
            "evidence": _ai_evidence(category, template, len(winners), len(losers), metric_diffs),
            "conclusion": "盈利和失败候选已经出现可分辨的指标差异，可作为后续策略权重和过滤条件依据。",
            "recommendation": recommendation,
            "confidence": min(0.90, 0.35 + len(items) / 30 + len(metric_diffs) * 0.08),
        })
    return insights


def _representative_ai_symbols(rows, limit=3):
    symbols = []
    for row in sorted(rows, key=lambda item: _num(item.get("mae_r"))):
        symbol = row.get("symbol")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def _ai_evidence(category, template, wins, losses, diffs):
    parts = []
    for diff in diffs:
        parts.append(
            f"{diff['metric']} 盈利均值 {diff['winner_avg']:.2f} vs 失败均值 {diff['loser_avg']:.2f}"
        )
    return f"{category}/{template} 已标注样本中，成功 {wins} 笔、失败 {losses} 笔；" + "；".join(parts) + "。"


def _ai_recommendation(source, category, metrics):
    metric_text = "、".join(metrics)
    if source == "alpha" or category == "alpha":
        return f"Alpha 类优先强化合约同步放量和趋势确认：重点监控 {metric_text}；低于盈利样本区间时只观察，不直接开仓。"
    if category in {"core_bluechip", "bluechip"}:
        return f"蓝筹类优先强化趋势结构和回调承接：重点监控 {metric_text}；成交量不作为唯一开仓依据。"
    return f"普通币按分类强化入场过滤：重点监控 {metric_text}；指标组合不达标时降为试探或观察。"


def generate_strategy_insights(
    main_db_path,
    ai_db_path,
    *,
    min_ai_samples=8,
    min_trade_samples=8,
    limit=8,
) -> dict:
    trade_rows = _fetch_trade_rows(main_db_path, 500)
    ai_rows = _fetch_ai_rows(ai_db_path, 1000)
    insights = [
        *_trade_insights(trade_rows, int(min_trade_samples)),
        *_ai_insights(ai_rows, int(min_ai_samples)),
    ]
    priority_rank = {
        "\u6025\u9700\u4fee\u590d": 0,
        "\u5efa\u8bae\u52a0\u5f3a": 1,
        "\u5efa\u8bae\u4f18\u5316": 2,
        "\u7ee7\u7eed\u89c2\u5bdf": 3,
        "???????": 0,
        "??????": 1,
        "???????": 2,
        "???????": 3,
    }
    insights = sorted(
        insights,
        key=lambda item: (
            priority_rank.get(item.get("priority"), 9),
            -float(item.get("confidence") or 0),
            -int(item.get("sample_size") or 0),
        ),
    )[:max(1, int(limit))]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sample_overview": {
            "trade_reviews": len(trade_rows),
            "ai_labeled_samples": len(ai_rows),
        },
        "insights": insights,
    }
