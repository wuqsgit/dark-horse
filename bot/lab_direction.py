#!/usr/bin/env python3
import requests
proxies = {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}

# Get 7d chart data
r = requests.get('https://api.coingecko.com/api/v3/coins/lab/market_chart?vs_currency=usd&days=7', proxies=proxies, timeout=10)
d = r.json()

prices = d.get('prices', [])
if not prices:
    print("No price data")
    exit()

# Data points (7d should be hourly = 168 points)
current = prices[-1][1]
week_ago = prices[0][1]

# Find high/low
high = max(p[1] for p in prices)
low = min(p[1] for p in prices)

# Recent trend
recent_prices = [p[1] for p in prices[-24:]]  # last 24 points
ma12 = sum(recent_prices) / len(recent_prices)
prev_prices = [p[1] for p in prices[-48:-24]]
ma_prev = sum(prev_prices) / len(prev_prices)

# 4h data
if len(prices) > 48:
    recent4h = [p[1] for p in prices[-4:]]
    ma4h = sum(recent4h) / 4

print("=" * 50)
print("LAB 多空分析")
print("=" * 50)
print(f"当前价格: ${current:.4f}")
print(f"7d涨跌: {(current-week_ago)/week_ago*100:+.2f}%")
print(f"7d最高: ${high:.4f}")
print(f"7d最低: ${low:.4f}")
print(f"当前位置: {(current-low)/(high-low)*100:.1f}%")
print()
print(f"均线对比 (MA12):")
print(f"  近12h均价: ${ma12:.4f}")
print(f"  前12h均价: ${ma_prev:.4f}")
print(f"  趋势: {'上涨' if ma12 > ma_prev else '下跌'}")
print("=" * 50)

# Decision
score = 50
reasons = []

# Trend
if current > ma12:
    score += 15
    reasons.append("价格>均线，看多")
else:
    score -= 10
    reasons.append("价格<均线，看空")

# Position in range
pos = (current - low) / (high - low) * 100 if high > low else 50
if pos < 30:
    score += 10
    reasons.append("低位区间，安全边际高")
elif pos > 70:
    score -= 5
    reasons.append("高位区间，风险累积")

# 7d momentum
week_change = (current - week_ago) / week_ago * 100
if week_change > 50:
    score += 10
    reasons.append("周强势，但警惕回调")
elif week_change < 0:
    score -= 10
    reasons.append("周下跌")

# Volatility check
import statistics
returns = [(prices[i][1]-prices[i-1][1])/prices[i-1][1] for i in range(1, len(prices))]
vol = statistics.stdev(returns) * 100 if len(returns) > 1 else 0

if vol > 15:
    reasons.append(f"波动率极高({vol:.1f}%)，不适合重仓")
elif vol > 10:
    reasons.append(f"波动率高({vol:.1f}%)，注意止损")

score = max(0, min(100, score))

print()
print("📊 综合评分:", score)
print()

# Recommendation
if score >= 75:
    direction = "做多"
    strength = "强烈"
elif score >= 60:
    direction = "做多"
    strength = "中等"
elif score >= 45:
    direction = "观望"
    strength = "中性"
elif score >= 30:
    direction = "做空"
    strength = "中等"
else:
    direction = "做空"
    strength = "强烈"

print(f"🚦 建议方向: {direction} ({strength})")
print(f"理由: {' | '.join(reasons)}")

# Stop loss / take profit levels
print()
if direction == "做多":
    stop = current * 0.95
    tp1 = current * 1.05
    tp2 = current * 1.10
    print(f"止损: ${stop:.4f} (-5%)")
    print(f"止盈1: ${tp1:.4f} (+5%)")
    print(f"止盈2: ${tp2:.4f} (+10%)")
else:
    stop = current * 1.05
    tp1 = current * 0.95
    tp2 = current * 0.90
    print(f"止损: ${stop:.4f} (+5%)")
    print(f"止盈1: ${tp1:.4f} (-5%)")
    print(f"止盈2: ${tp2:.4f} (-10%)")