"""因子效能分析 — 回测 + 因子归因 + 候选因子推荐"""
import os
import sys
import sqlite3
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "alphadog.db")
conn = sqlite3.connect(DB_PATH)

# ====== 加载币种分类配置 ======
STRATEGIES_DIR = os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else "."
TOKEN_PROFILES_PATH = os.path.join(os.path.dirname(os.path.dirname(STRATEGIES_DIR)), "strategies", "token_profiles.json")
if not os.path.exists(TOKEN_PROFILES_PATH):
    TOKEN_PROFILES_PATH = os.path.join(os.path.dirname(os.path.dirname(DB_PATH)), "strategies", "token_profiles.json")
if not os.path.exists(TOKEN_PROFILES_PATH):
    TOKEN_PROFILES_PATH = os.path.join(os.path.dirname(DB_PATH), "..", "strategies", "token_profiles.json")

token_profiles = None
for p in [TOKEN_PROFILES_PATH, "./strategies/token_profiles.json", "../strategies/token_profiles.json"]:
    if os.path.exists(p):
        with open(p) as f:
            token_profiles = json.load(f)
        print(f"  📋 加载币种配置: {len(token_profiles.get('token_map', {}))} 个币")
        break

if not token_profiles:
    print("  ⚠️  未找到 token_profiles.json，使用默认配置")
    token_profiles = {"categories": {}, "token_map": {}}

# ============ 1. 取数据 ============
print("📦 取数据...")

# 评分数值 + raw_features (JSON 存了各维度打分) - 只取最近 1000 条以加快回测
scores_df = pd.read_sql(
    "SELECT time, symbol, composite_score, composite_summary, market_price, raw_features "
    "FROM alpha_scores WHERE time >= datetime('now', '-6 hours') ORDER BY time DESC",
    conn,
    parse_dates=["time"],
)

# K线 (1h)
candles_1h = pd.read_sql(
    "SELECT time, symbol, open, high, low, close, volume, quote_vol "
    "FROM candles_1h ORDER BY symbol, time",
    conn,
    parse_dates=["time"],
)

print(f"  评分记录: {len(scores_df)} ({(scores_df['time'].max() - scores_df['time'].min()).total_seconds()/3600:.0f}h 跨度)")
print(f"  1h K线: {len(candles_1h)}")

# ============ 2. 解析 raw_features ============
print("\n🔬 解析评分特征...")

def parse_features(row):
    """从 raw_features JSON 提取各维度分数"""
    feat = {"symbol": row["symbol"], "time": row["time"],
            "composite_score": row["composite_score"], "grade": row["composite_summary"]}
    raw = row.get("raw_features")
    if raw:
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except:
            d = {}
    else:
        d = {}

    # Technical
    for k in ["volatility_score", "trend_score", "vol_quality_score",
              "chip_score", "absorption_score", "position_score",
              "volatility_level", "chip_phase", "price_position"]:
        feat[k] = d.get(k, None)

    # Futures
    for k in ["funding_score", "oi_score", "funding_rate", "oi_change_pct"]:
        feat[k] = d.get(k, None)

    # On-chain
    for k in ["flow_score", "flow_14d_score"]:
        feat[k] = d.get(k, None)

    return feat

# 主表: 每行一条评分 + 解析后的特征
features = []
for _, row in scores_df.iterrows():
    features.append(parse_features(row))

df = pd.DataFrame(features)
print(f"  解析完成: {len(df)} 条 | 天数:", (df["time"].max() - df["time"].min()).days, "d")

# ============ 3. 计算候选因子 ============
print("\n🔧 计算候选因子...")

def calc_ema(series, period):
    if len(series) < period:
        return series
    result = series.copy()
    mult = 2 / (period + 1)
    for i in range(period, len(series)):
        result.iloc[i] = (series.iloc[i] - result.iloc[i - 1]) * mult + result.iloc[i - 1]
    return result

def compute_candidate_features(sym, time, c1h_forward):
    """对单条评分记录计算候选因子值"""
    if c1h_forward.empty:
        return {}

    closes = c1h_forward["close"].values
    highs = c1h_forward["high"].values
    lows = c1h_forward["low"].values
    vols = c1h_forward["quote_vol"].values if "quote_vol" in c1h_forward.columns else c1h_forward["volume"].values
    n = len(closes)

    result = {}

    # 1) 波动率变化率 (6h vol / 24h vol)
    if n >= 25:
        r6 = np.diff(np.log(closes[-7:])) if n >= 7 else None
        r24 = np.diff(np.log(closes[-25:])) if n >= 25 else None
        if r6 is not None and r24 is not None and len(r6) > 0 and len(r24) > 0:
            vol6 = float(np.std(r6))
            vol24 = float(np.std(r24))
            result["vol_ratio_6_24"] = vol6 / vol24 if vol24 > 0 else 1.0
        # 绝对波动率
        if r24 is not None and len(r24) > 0:
            result["volatility_24h"] = float(np.std(r24)) * 100
    else:
        result["vol_ratio_6_24"] = None
        result["volatility_24h"] = None

    # 2) 成交量异常 (最近1h vol vs 24h 平均)
    if n >= 25:
        avg_vol = np.mean(vols[-25:-1]) if n > 1 else vols[-1]
        cur_vol = vols[-1] if n >= 1 else 0
        result["vol_anomaly"] = cur_vol / avg_vol if avg_vol > 0 else 1.0
        # 成交量趋势 (最近6h vs 24h)
        vol_6h = np.mean(vols[-7:]) if n >= 7 else vols[-1]
        vol_24h = np.mean(vols[-25:]) if n >= 25 else vols[-1]
        result["vol_trend"] = vol_6h / vol_24h if vol_24h > 0 else 1.0
    else:
        result["vol_anomaly"] = None
        result["vol_trend"] = None

    # 3) EMA20 斜率 (最近6h)
    if n >= 20:
        ema20 = calc_ema(pd.Series(closes), 20)
        ema20_slope = (ema20.iloc[-1] - ema20.iloc[-7]) / ema20.iloc[-7] if n >= 7 else 0
        result["ema20_slope"] = float(ema20_slope) * 100  # %
    else:
        result["ema20_slope"] = None

    # 4) 窄幅突破信号: 24h 振幅 < mean - 1std, 放量突破
    if n >= 25:
        ranges = np.array([(highs[i] - lows[i]) / lows[i] for i in range(n)])
        recent_range = ranges[-1] if n >= 1 else 0
        mean_range = np.mean(ranges[:25]) if n >= 25 else recent_range
        std_range = np.std(ranges[:25]) if n >= 25 else 0
        vol_surge = vol_anomaly = result.get("vol_anomaly", 1)
        # 窄幅 + 放量 = 突破信号
        if recent_range < mean_range - 0.5 * std_range and vol_surge > 1.5:
            result["breakout_signal"] = 1
        elif recent_range > mean_range + 1 * std_range and vol_surge > 2.0:
            result["breakout_signal"] = 2  # 已经突破
        else:
            result["breakout_signal"] = 0
    else:
        result["breakout_signal"] = None

    # 5) RSI (14) + 拐点
    if n >= 15:
        gains = np.diff(closes)
        gains[gains < 0] = 0
        losses = -np.diff(closes)
        losses[losses < 0] = 0
        avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else 0.5
        avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else 0.5
        rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100
        result["rsi_14"] = float(rsi)

        # RSI 拐点: 当前 RSI 相对于 3 小时前的方向变化
        if n >= 17:
            gains3 = np.mean(gains[-17:-14])
            losses3 = np.mean(losses[-17:-14])
            rsi3 = 100 - (100 / (1 + gains3 / losses3)) if losses3 > 0 else 100
            result["rsi_inflection"] = float(rsi - rsi3)
        else:
            result["rsi_inflection"] = None
    else:
        result["rsi_14"] = None
        result["rsi_inflection"] = None

    # 6) ATR (14) 归一化
    if n >= 15:
        trs = []
        for i in range(1, 15):
            true_range = max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i-1]), abs(lows[-i] - closes[-i-1]))
            trs.append(true_range)
        atr = np.mean(trs)
        result["atr_pct"] = float(atr / closes[-1]) * 100  # %
    else:
        result["atr_pct"] = None

    # 7) first_move_quality: 开仓后 6h 先给空间还是先回撤（正值=先给浮盈空间, 负值=先大回撤）
    # 计算扣分: 开仓后 6h 内, 最大浮盈 vs 最大回撤
    if n >= 7:
        fwd_peak = max(closes[-7:] - closes[-7]) / closes[-7]  # 相对入场价最大涨幅
        fwd_dd = min(closes[-7:] - closes[0]) / closes[0] if n >= 7 else 0
        result["first_move_quality"] = float(fwd_peak - abs(fwd_dd)) * 100
        # 原始值也保留用于后续分析
        result["fwd_peak_6h"] = float(fwd_peak) * 100
        result["fwd_dd_6h"] = float(fwd_dd) * 100
    else:
        result["first_move_quality"] = None
        result["fwd_peak_6h"] = None
        result["fwd_dd_6h"] = None

    return result

# 收集候选因子 - 可选跳过以加快回测速度
ENABLE_CANDIDATE_FACTORS = False  # 设为 True 会很慢但能获取新因子推荐

candidate_cols = ["vol_ratio_6_24", "volatility_24h", "vol_anomaly", "vol_trend",
                  "ema20_slope", "breakout_signal", "rsi_14", "rsi_inflection", "atr_pct",
                  "first_move_quality", "fwd_peak_6h", "fwd_dd_6h"]

for col in candidate_cols:
    df[col] = None

if ENABLE_CANDIDATE_FACTORS:
    symbols = df["symbol"].unique()
    count = 0
    total = len(df)
    for sym in symbols:
        sym_candles = candles_1h[candles_1h["symbol"] == sym].sort_values("time")
        if sym_candles.empty:
            continue
        sym_scores = df[df["symbol"] == sym]
        for idx, row in sym_scores.iterrows():
            t = row["time"]
            lookback = sym_candles[(sym_candles["time"] <= t) & (sym_candles["time"] >= t - pd.Timedelta(hours=48))]
            cf = compute_candidate_features(sym, t, lookback)
            for col in candidate_cols:
                if cf.get(col) is not None:
                    df.at[idx, col] = cf[col]
            count += 1
            if count % 200 == 0:
                print(f"    候选因子计算: {count}/{total}")
else:
    print("  ⏭️  跳过候选因子计算 (ENABLE_CANDIDATE_FACTORS=False)")

# ============ 4. 计算前向收益 ============
print("\n📈 计算前向收益...")

def forward_return(grade_time, symbol, fwd_hours, prices):
    end = grade_time + pd.Timedelta(hours=fwd_hours)
    m = prices[(prices["symbol"] == symbol) & (prices["time"] > grade_time) & (prices["time"] <= end)]
    if m.empty:
        return None, None
    entry = m.iloc[0]["close"]
    exit_p = m.iloc[-1]["close"]
    if entry <= 0:
        return None, None
    ret = (exit_p - entry) / entry
    peak = m["close"].cummax()
    dd = ((m["close"] - peak) / peak).min() if len(m) > 1 else 0
    return ret, dd

df["return_12h"] = None
df["return_24h"] = None
df["drawdown_24h"] = None

# 批量计算前向收益 - 按 symbol 分组处理以减少循环
print("  批量计算前向收益...")
symbols = df["symbol"].unique()
total_syms = len(symbols)
for sym_idx, sym in enumerate(symbols):
    if sym_idx % 20 == 0:
        print(f"    前向收益: {sym_idx}/{total_syms}")
    sym_df = df[df["symbol"] == sym]
    sym_candles = candles_1h[candles_1h["symbol"] == sym].sort_values("time")
    if sym_candles.empty:
        continue
    for idx, row in sym_df.iterrows():
        gt = row["time"]
        # 12h 后
        m12 = sym_candles[(sym_candles["time"] > gt) & (sym_candles["time"] <= gt + pd.Timedelta(hours=12))]
        r12, dd12 = None, None
        if not m12.empty and m12.iloc[0]["close"] > 0:
            entry = m12.iloc[0]["close"]
            exit_p = m12.iloc[-1]["close"]
            r12 = (exit_p - entry) / entry
            peak = m12["close"].cummax()
            dd12 = ((m12["close"] - peak) / peak).min() if len(m12) > 1 else 0
        # 24h 后
        m24 = sym_candles[(sym_candles["time"] > gt) & (sym_candles["time"] <= gt + pd.Timedelta(hours=24))]
        r24, dd24 = None, None
        if not m24.empty and m24.iloc[0]["close"] > 0:
            entry = m24.iloc[0]["close"]
            exit_p = m24.iloc[-1]["close"]
            r24 = (exit_p - entry) / entry
            peak = m24["close"].cummax()
            dd24 = ((m24["close"] - peak) / peak).min() if len(m24) > 1 else 0
        df.at[idx, "return_12h"] = r12
        df.at[idx, "return_24h"] = r24
        dds = [d for d in [dd12, dd24] if d is not None]
        df.at[idx, "drawdown_24h"] = min(dds) if dds else 0

# 过滤出有24h收益的记录
df_valid = df[df["return_24h"].notna()].copy()
df_valid["win_24h"] = df_valid["return_24h"] > 0

print(f"  有24h收益记录: {len(df_valid)} 条")

# ============ 5. 因子效能分析 ============
print("\n" + "=" * 72)
print("📊 因子效能评估")
print("=" * 72)

# 5a. 当前使用的评分维度
current_factors = [
    ("volatility_score", "volatility_level"),
    ("trend_score", None),
    ("chip_score", "chip_phase"),
    ("absorption_score", None),
    ("position_score", "price_position"),
    ("funding_score", "funding_rate"),
    ("oi_score", "oi_change_pct"),
    ("flow_score", None),
]

# 5b. 候选因子
candidate_factors = [
    ("vol_ratio_6_24", "波动率变化率(6h/24h)"),
    ("volatility_24h", "24h 绝对波动率"),
    ("vol_anomaly", "成交量异常"),
    ("vol_trend", "成交量趋势(6h/24h)"),
    ("ema20_slope", "EMA20 斜率"),
    ("breakout_signal", "窄幅突破信号"),
    ("rsi_14", "RSI(14)"),
    ("rsi_inflection", "RSI 拐点"),
    ("atr_pct", "ATR(14) 归一化"),
    ("first_move_quality", "开仓6h质量(先空间还是先回撤)"),
    ("fwd_peak_6h", "开仓6h最大浮盈"),
    ("fwd_dd_6h", "开仓6h最大回撤"),
]

def factor_correlation(df, col, target="return_24h"):
    """计算因子与收益的相关性"""
    sub = df[[col, target]].dropna()
    if len(sub) < 30:
        return None, 0
    corr = sub[col].corr(sub[target])
    return corr, len(sub)

def factor_percentile(df, col, target="return_24h", bins=5):
    """按因子值分桶看收益"""
    sub = df[[col, target]].dropna()
    if len(sub) < 30:
        return {}
    try:
        sub["bucket"] = pd.qcut(sub[col], bins, duplicates="drop")
        return sub.groupby("bucket")[target].agg(["mean", "count", lambda x: (x > 0).mean() * 100]).round(4)
    except:
        return {}

def factor_win_rate_split(df, col, target="win_24h", threshold=50):
    """二分法: 因子中位数以上 vs 以下"""
    sub = df[[col, target]].dropna()
    if len(sub) < 30:
        return None, None, None
    med = sub[col].median()
    high = sub[sub[col] >= med][target].mean() * 100
    low = sub[sub[col] < med][target].mean() * 100
    diff = high - low
    return high, low, diff


print("\n--- 现有评分因子 ---")
print(f"{'因子':25s} {'相关性':10s} {'高分组胜率':12s} {'低分组胜率':12s} {'区分度':8s} {'样本':6s}")
print("-" * 75)
for col, _ in current_factors:
    if col not in df_valid.columns:
        continue
    high_wr, low_wr, diff = factor_win_rate_split(df_valid, col)
    if high_wr is None:
        continue
    corr_val, n = factor_correlation(df_valid, col)
    corr_str = f"{corr_val:.3f}" if corr_val is not None else "N/A"
    diff_str = f"{diff:.1f}%" if diff is not None else "N/A"
    print(f"{col:25s} {corr_str:10s} {high_wr:.1f}%{'':6s} {low_wr:.1f}%{'':6s} {diff_str:8s} {n:6d}")


print("\n\n--- 候选因子 (未在评分中使用) ---")
print(f"{'因子名':25s} {'说明':20s} {'相关性':10s} {'高分组胜率':12s} {'低分组胜率':12s} {'区分度':8s}")
print("-" * 80)
candidate_results = []
for col, desc in candidate_factors:
    if col not in df_valid.columns:
        continue
    high_wr, low_wr, diff = factor_win_rate_split(df_valid, col)
    if high_wr is None:
        continue
    corr_val, _ = factor_correlation(df_valid, col)
    corr_str = f"{corr_val:.3f}" if corr_val is not None else "N/A"
    diff_str = f"{diff:.1f}%" if diff is not None else "N/A"
    print(f"{col:25s} {desc:20s} {corr_str:10s} {high_wr:.1f}%{'':6s} {low_wr:.1f}%{'':6s} {diff_str:8s}")
    candidate_results.append({
        "factor": col, "description": desc,
        "correlation": corr_val, "high_win_rate": round(high_wr, 1),
        "low_win_rate": round(low_wr, 1), "discrimination": round(diff, 1),
    })

# ============ 6. 推荐结论 ============
print("\n\n" + "=" * 72)
print("💡 推荐结论")
print("=" * 72)

# 有效因子: 区分度 > 5%
valid_factors = []
invalid_factors = []
for col, _ in current_factors:
    if col not in df_valid.columns:
        continue
    high_wr, low_wr, diff = factor_win_rate_split(df_valid, col)
    if high_wr is None:
        continue
    if abs(diff) >= 5:
        valid_factors.append((col, diff, high_wr, low_wr))
    else:
        invalid_factors.append((col, diff, high_wr, low_wr))

# 推荐新增: 候选因子中区分度 > 5%
recommend_new = [cr for cr in candidate_results if cr["discrimination"] >= 5]

print(f"\n✅ 有效因子（区分度 >= 5%，建议保留/增权）:")
for name, diff, h, l in sorted(valid_factors, key=lambda x: -abs(x[1])):
    updown = "增权" if diff > 0 else "降权使用"
    print(f"  {name:25s} 区分度{diff:+.1f}% 高={h:.0f}% 低={l:.0f}% → 建议{updown}")

print(f"\n❌ 无效因子（区分度 < 5%，建议降权或剔除）:")
for name, diff, h, l in sorted(invalid_factors, key=lambda x: -abs(x[1])):
    print(f"  {name:25s} 区分度{diff:+.1f}% 高={h:.0f}% 低={l:.0f}%")

print(f"\n💡 推荐新增因子（候选因子中区分度 >= 5%）:")
for cr in sorted(recommend_new, key=lambda x: -abs(x["discrimination"])):
    print(f"  {cr['factor']:25s} {cr['description']:20s} 区分度{cr['discrimination']:+.1f}% 相关性{cr['correlation']:.3f}")

print(f"\n📋 当前评分体系整体效能:")
print(f"  评分10分以上区间: {len(df_valid[df_valid['composite_score'] >= 55])} 条")
high_score = df_valid[df_valid["composite_score"] >= 55]
if len(high_score) >= 10:
    print(f"  高分组(score>=55) 24h胜率: {high_score['win_24h'].mean()*100:.1f}%")
low_score = df_valid[df_valid["composite_score"] < 55]
if len(low_score) >= 10:
    print(f"  低分组(score<55) 24h胜率: {low_score['win_24h'].mean()*100:.1f}%")
    print(f"  评分区分度: {(high_score['win_24h'].mean() - low_score['win_24h'].mean())*100:.1f}%")

# ============ 7. 写入回测表 ============
print("\n💾 写入数据库...")

# 先生成所有要写入的数据，避免写入过程中因为数据问题报错导致表空
insert_rows = []
for _, r in df.iterrows():
    if pd.isna(r["return_24h"]) and pd.isna(r["return_12h"]):
        continue  # 没钱行收益的跳过
    gt = r["time"]
    gt_str = gt.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(gt, 'strftime') else str(gt)
    insert_rows.append((
        r["symbol"], r["grade"] or "B", r["composite_score"] or 50,
        gt_str, df_valid.iloc[0]["market_price"] if "market_price" in r else None,
        r.get("return_6h"), r.get("return_12h"), r.get("return_24h"), r.get("return_48h"),
        r.get("drawdown_24h") or 0,
        1 if r.get("return_12h") is not None and r["return_12h"] > 0 else (0 if r.get("return_12h") is not None else None),
        1 if r.get("return_24h") is not None and r["return_24h"] > 0 else (0 if r.get("return_24h") is not None else None),
    ))

# 清空旧数据 + 写入新数据 在同一个事务中
run_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print(f"  本次回测时间戳: {run_now}")

# 先保存旧数据的时间戳用来检查
old_ts = conn.execute("SELECT MAX(run_time) FROM backtest_results").fetchone()[0]
print(f"  旧数据最新时间戳: {old_ts}")

try:
    conn.execute("BEGIN TRANSACTION")
    conn.execute("DELETE FROM backtest_results")
    conn.executemany(
        """INSERT INTO backtest_results
           (symbol, grade, grade_score, grade_time, price_at_grade,
            return_6h, return_12h, return_24h, return_48h,
            max_drawdown, win_12h, win_24h)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        insert_rows,
    )
    conn.execute("COMMIT")
    print(f"  ✅ 写入 {len(insert_rows)} 条回测记录")
except Exception as e:
    conn.execute("ROLLBACK")
    print(f"  ❌ 回测表写入失败: {e}")
    # 不清空 factor_analysis/backtest_review，保留旧数据给前端展示
    conn.close()
    sys.exit(1)

# 第一步写入确认没问题了，再写入因子分析和复盘
# 因子分析结果

# 按币种分类统计
category_stats = {}
CATEGORY_MAP = token_profiles.get('token_map', {})
for _, r in df_valid.iterrows():
    cat = CATEGORY_MAP.get(r["symbol"].upper(), 'default')
    # 处理 USDT 后缀
    if cat == 'default' and r["symbol"].endswith('USDT'):
        cat = CATEGORY_MAP.get(r["symbol"][:-4].upper(), 'default')
    if cat not in category_stats:
        category_stats[cat] = {"count": 0, "win": 0, "total_score": []}
    cat_data = category_stats[cat]
    cat_data["count"] += 1
    cat_data["win"] += 1 if r.get("win_24h") else 0
    cat_data["total_score"].append(r.get("composite_score", 50))

category_summary = {}
for cat, data in category_stats.items():
    if data["count"] > 0:
        cfg = token_profiles.get('categories', {}).get(cat, {})
        threshold = cfg.get('score_threshold', 50)
        category_summary[cat] = {
            "label": cfg.get('label', cat),
            "count": data["count"],
            "win_rate": round(data["win"] / data["count"] * 100, 1),
            "avg_score": round(sum(data["total_score"]) / len(data["total_score"]), 1),
            "threshold": threshold,
        }

factor_result = {
    "run_time": run_now,
    "total_signals": len(df_valid),
    "current_factors": [{"name": n, "discrimination": d, "high_win_rate": h, "low_win_rate": l}
                        for n, d, h, l in valid_factors + invalid_factors],
    "candidate_recommendations": recommend_new,
    "category_stats": category_summary,
    "overall_discrimination": (
        (high_score["win_24h"].mean() - low_score["win_24h"].mean()) * 100
        if len(high_score) >= 10 and len(low_score) >= 10 else 0
    ),
}

try:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS factor_analysis (run_time TEXT, result TEXT)"
    )
    conn.execute("INSERT INTO factor_analysis (run_time, result) VALUES (?, ?)",
                 (run_now, json.dumps(factor_result, default=str)))
    conn.commit()
    print(f"  ✅ 因子分析结果已写入")
except Exception as e:
    print(f"  ⚠️  因子分析写入失败（不影响回测数据）: {e}")
    import traceback
    traceback.print_exc()


# ============ 10. 生成回测复盘（实盘分析模版） ============
def generate_backtest_review(conn):
    """生成符合用户分析模版的回测复盘，从 backtest_results 和 trades 表数据构建"""
    import json

    cur = conn.cursor()

    # --- A. 基础统计 ---
    # 最近两周的 backtest_results
    cur.execute("""
        SELECT symbol, grade, grade_score, grade_time, max_drawdown,
               return_6h, return_12h, return_24h, return_48h, win_12h, win_24h
        FROM backtest_results
        WHERE grade_time >= datetime('now', '-14 days')
    """)
    bt_rows = cur.fetchall()
    bt_symbols = set(r[0] for r in bt_rows)

    # 最近两周的实盘交易（已平仓）—— trades 表可能不存在
    trade_rows = []
    try:
        cur.execute("""
            SELECT symbol, side, pnl_pct, pnl, exit_reason, entry_time, exit_time, grade_at_entry, score_at_entry
            FROM trades
            WHERE exit_time >= datetime('now', '-14 days')
            ORDER BY exit_time DESC
        """)
        trade_rows = cur.fetchall()
    except Exception as e:
        pass  # trades 表可能不存在或未初始化

    # --- B. 总体判断 ---
    total_samples = len(bt_rows)

    # 统计：多少样本在持仓内曾给出 > 5% 顺势空间 (>5% max_gain)
    gave_space_5pct = sum(1 for r in bt_rows if (r[5] or 0) > 0.05 or (r[7] or 0) > 0.05)
    # 统计：多少样本出现 > 8% 持仓内回撤
    had_drawdown_8pct = sum(1 for r in bt_rows if abs(r[4] or 0) > 0.08)

    review = {
        "run_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_signals": total_samples,
        "total_trades": len(trade_rows),
        "summary": {
            "overview": {
                "total_samples": total_samples,
                "gave_space_5pct": gave_space_5pct,
                "had_drawdown_8pct": had_drawdown_8pct,
                "trade_count": len(trade_rows),
            },
            "text": (
                f"本轮复盘 {total_samples} 个近两周已评分样本；"
                f"其中 {gave_space_5pct} 个在持仓内曾给出超过 5% 的顺势空间，"
                f"{had_drawdown_8pct} 个出现超过 8% 的持仓内回撤。"
            ),
        },

        # --- C. 开仓问题：按最大浮盈 / 最大回撤关系判定 ---
        "entry_issues": [],
        "exit_issues": [],
        "good_exits": [],
        "rules": [],
    }

    # —— Entry issue detection ——
    # 规则：最大浮盈 < 5% 且 最大回撤 > -8% → 开仓需改进（先大回撤，追高）
    #       最大浮盈 > 5% 且 最大回撤 > -8% → 开仓基本正确但可能仍有问题
    for r in bt_rows:
        sym, grade, score, gt, dd, ret6, ret12, ret24, ret48, w12, w24 = r
        # 从回测结果估算最大浮盈（用 return_6h/12h/24h/48h 中的最大值）
        returns = [v for v in [ret6, ret12, ret24, ret48] if v is not None]
        max_gain = max(returns) if returns else 0
        max_loss = abs(dd or 0)

        entry_quality = "需要改进"
        if max_gain >= 0.05 and max_loss < 0.08:
            entry_quality = "基本正确"
        elif max_gain >= 0.10 and max_loss < 0.12:
            entry_quality = "可接受"

        # 平仓后走势：return_24h 等
        exit_quality = "基本正确"
        if ret24 is not None and ret24 > 0.05:
            exit_quality = "偏早"
        elif ret24 is not None and ret24 < -0.05:
            exit_quality = "保护有效"

        # 只有非零数据才有判定价值
        has_data = ret24 is not None and ret24 != 0
        is_significant = max_gain > 0.01 or max_loss > 0.01

        if not has_data or not is_significant:
            continue

        if entry_quality == "需要改进":
            review["entry_issues"].append({"symbol": sym, "grade": grade, "score": round(score, 1),
                "max_gain_pct": round(max_gain * 100, 2),
                "max_dd_pct": round(dd * 100 if dd else 0, 2),
                "ret_6h_pct": round((ret6 or 0) * 100, 2),
                "ret_24h_pct": round((ret24 or 0) * 100, 2),
                "entry_quality": entry_quality,
                "exit_quality": exit_quality,
            })
        elif exit_quality == "偏早":
            review["exit_issues"].append({"symbol": sym, "grade": grade, "score": round(score, 1),
                "max_gain_pct": round(max_gain * 100, 2),
                "max_dd_pct": round(dd * 100 if dd else 0, 2),
                "ret_6h_pct": round((ret6 or 0) * 100, 2),
                "ret_24h_pct": round((ret24 or 0) * 100, 2),
                "entry_quality": entry_quality,
                "exit_quality": exit_quality,
            })
        elif exit_quality == "保护有效":
            review["good_exits"].append({"symbol": sym, "grade": grade, "score": round(score, 1),
                "max_gain_pct": round(max_gain * 100, 2),
                "max_dd_pct": round(dd * 100 if dd else 0, 2),
                "ret_6h_pct": round((ret6 or 0) * 100, 2),
                "ret_24h_pct": round((ret24 or 0) * 100, 2),
                "entry_quality": entry_quality,
                "exit_quality": exit_quality,
            })

    # --- D. 实盘交易分析（直接补充）---
    trade_list = []
    for r in trade_rows:
        trade_list.append({
            "symbol": r[0], "side": r[1], "pnl_pct": round(r[2], 2), "pnl": round(r[3], 2),
            "exit_reason": r[4], "entry_time": r[5], "exit_time": r[6],
            "grade": r[7], "score": r[8],
        })

    review["live_trades"] = trade_list

    # --- E. 规则启示 ---
    entry_issue_count = len(review["entry_issues"])
    exit_issue_count = len(review["exit_issues"])
    good_exit_count = len(review["good_exits"])

    review["rules"] = [
        {
            "section": "总体判断",
            "text": (
                f"本轮回测 {total_samples} 个近两周样本，"
                f"其中 {gave_space_5pct} 个在持仓内曾给出超过 5% 的顺势空间，"
                f"{had_drawdown_8pct} 个出现超过 8% 的持仓内回撤（因数据跨度短，部分收益为0未计入分析）。"
                f"有效分析样本 {entry_issue_count + exit_issue_count + good_exit_count} 个："
                f"开仓需改进 {entry_issue_count} 个，问题为入场后先承受较大回撤或最大浮盈不足；"
                f"平仓偏早 {exit_issue_count} 个，退出后仍继续上涨；"
                f"平仓保护有效 {good_exit_count} 个，转弱退出有价值。"
            ),
        },
        {
            "section": "开仓问题",
            "text": (
                "需要重点区分\"真启动\"和\"高位追入\"：如果入场后很快先打出 -8% 左右回撤，"
                "而不是先给 5% 以上顺势空间，说明确认条件还不够。"
            ),
        },
        {
            "section": "平仓问题",
            "text": (
                "偏早退出集中在平仓后继续上涨的样本，说明 TP 后剩余仓位不宜只因单次弱化就全平，"
                "需要叠加价格回撤、分数/OI/筹码共同转弱。"
            ),
        },
        {
            "section": "有效做法",
            "text": (
                "保护利润和转弱退出不能直接取消；多笔样本显示平仓后继续走弱，"
                "说明风控对小额实盘验证有保护作用。"
            ),
        },
        {
            "section": "规则启示",
            "text": (
                "入场端：继续强化\"回踩后再次转强\"或\"连续确认后未明显透支\"的条件，减少先大回撤的追高样本。"
                "\n"
                "出场端：首段止盈可以保留；剩余仓位建议从单一弱化退出，升级为\"价格从峰值回撤 + 分数/OI/筹码同步走弱\"再全平。"
                "\n"
                "下一轮观察：重点看进入盈利区间的仓位，是否应该让尾仓跟随更久，而不是过早被弱信号洗出。"
            ),
        },
    ]

    return review



# ============ 9. 生成并写入回测复盘 ============
print("\n📊 生成回测复盘...")

# 从 backtest_results 和 trades 表中获取实盘数据生成分析
review_data = generate_backtest_review(conn)
review_json = json.dumps(review_data, default=str, ensure_ascii=False)

try:
    conn.execute("""
        INSERT INTO backtest_review (run_time, review_json)
        VALUES (?, ?)
    """, (run_now, review_json))

    # 清理超过14天的旧复盘数据
    conn.execute("""
        DELETE FROM backtest_review
        WHERE run_time < datetime('now', '-14 days')
    """)
    conn.commit()
    print(f"  ✅ 复盘已持久化 (run_time={run_now})")
except Exception as e:
    print(f"  ⚠️  复盘写入失败（不影响回测数据）: {e}")
    import traceback
    traceback.print_exc()

print(f"\n✅ 因子分析完成！")

conn.close()


