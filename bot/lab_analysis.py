#!/usr/bin/env python3
import requests
proxies = {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}

r = requests.get('https://api.coingecko.com/api/v3/coins/lab', proxies=proxies, timeout=10)
d = r.json()
m = d.get('market_data', {})

spark = m.get('sparkline_7d', {})
prices = spark.get('price', []) if spark else []
high7d = max(prices) if prices else 0
low7d = min(prices) if prices else 0
current = prices[-1] if prices else m['current_price']['usd']
pos = (current - low7d) / (high7d - low7d) * 100 if high7d > low7d else 50

p1h = m.get('price_change_percentage_1h_in_currency', {}).get('usd', 0)
p24h = m.get('price_change_percentage_24h', 0)
p7d = m.get('price_change_percentage_7d', 0)
p30d = m.get('price_change_percentage_30d_in_currency', {}).get('usd', 0)

print("=" * 50)
print("📊 LAB 分析报告")
print("=" * 50)
print(f"💰 价格: ${current:.4f} (24h: {p24h:+.2f}%)")
print(f"📈 7d涨跌: {p7d:+.2f}% | 30d涨跌: {p30d:+.2f}%")
print(f"🏆 市值排名: #{d.get('market_cap_rank', '?')}")
print(f"📊 24h成交量: ${m['total_volume']['usd']:,.0f}")
print(f"💵 市值: ${m['market_cap']['usd']:,.0f}")
print(f"🔄 流通量: {m['circulating_supply']:,.0f} LAB")
print()
print(f"📉 7d位置:")
print(f"  最低: ${low7d:.4f}")
print(f"  最高: ${high7d:.4f}")
print(f"  当前: {pos:.1f}%")
print()
print(f"🕐 1h涨跌: {p1h:+.2f}%")
print("=" * 50)

# Simple signal
score = 50
signals = []

if p24h > 10:
    score += 15
    signals.append("24h涨幅大")
elif p24h > 5:
    score += 10
    signals.append("温和上涨")

if p7d > 50:
    score += 15
    signals.append("7d强势")
elif p7d < 0:
    score -= 10
    signals.append("7d下跌")

if pos < 30:
    score += 10
    signals.append("价格低位")
elif pos > 80:
    score -= 10
    signals.append("价格高位")

if p1h > 3:
    score += 5
    signals.append("1h强势")
elif p1h < -3:
    score -= 5
    signals.append("1h走弱")

score = max(0, min(100, score))

if score >= 80:
    action = "S1-强烈买入"
elif score >= 70:
    action = "S2-建议买入"
elif score >= 55:
    action = "A-可小仓"
else:
    action = "B-观望"

print(f"\n🚦 入场信号分数: {score}")
print(f"建议: {action}")
print(f"理由: {', '.join(signals) if signals else '无明显信号'}")