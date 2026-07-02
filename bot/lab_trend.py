#!/usr/bin/env python3
import requests, statistics
proxies = {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}

# Get 30d data for comprehensive analysis
r = requests.get('https://api.coingecko.com/api/v3/coins/lab/market_chart?vs_currency=usd&days=30', proxies=proxies, timeout=10)
d = r.json()
prices_data = d.get('prices', [])

# Get basic current data
r2 = requests.get('https://api.coingecko.com/api/v3/coins/lab', proxies=proxies, timeout=10)
coin = r2.json()
current_price = coin['market_data']['current_price']['usd']
change_24h = coin['market_data']['price_change_percentage_24h']
change_7d = coin['market_data']['price_change_percentage_7d']
vol_24h = coin['market_data']['total_volume']['usd']

# Extract prices
prices = [p[1] for p in prices_data]

# Calculate trend strength metrics
def calc_ma(data, period):
    return sum(data[-period:]) / period if len(data) >= period else sum(data) / len(data)

# Moving averages
ma4 = calc_ma(prices, 4)    # 4h MA
ma12 = calc_ma(prices, 12) # 12h MA
ma24 = calc_ma(prices, 24) # 24h MA
ma72 = calc_ma(prices, 72) # 72h MA

# Current price vs MAs
price = prices[-1]
above_ma = sum(1 for m in [ma4, ma12, ma24, ma72] if price > m)

# Trend score components
score = 50
components = {}

# 1. MA排列 (最多25分)
if price > ma4 > ma12 > ma24 > ma72:
    ma_score = 25
    ma_signal = "完美多头排列"
elif price > ma4 and price > ma12 and above_ma >= 3:
    ma_score = 20
    ma_signal = "均线支撑，看多"
elif price > ma4 and above_ma >= 2:
    ma_score = 15
    ma_signal = "价格强于多数均线"
elif above_ma >= 2:
    ma_score = 10
    ma_signal = "价格高于部分均线"
elif above_ma == 1:
    ma_score = 5
    ma_signal = "价格仅高于1条均线"
else:
    ma_score = 0
    ma_signal = "均线全线压制"

components['均线排列'] = {'score': ma_score, 'signal': ma_signal, 'value': f'价格${price:.2f} > {above_ma}/4条均线'}

# 2. 成交量 (最多25分)
vol_ratio = vol_24h / coin['market_data']['market_cap']['usd'] * 100 if coin['market_data']['market_cap']['usd'] > 0 else 0
if vol_ratio > 15:
    vol_score = 25
    vol_signal = f"成交量活跃({vol_ratio:.1f}%)"
elif vol_ratio > 8:
    vol_score = 20
    vol_signal = f"成交量良好({vol_ratio:.1f}%)"
elif vol_ratio > 3:
    vol_score = 10
    vol_signal = f"成交量正常({vol_ratio:.1f}%)"
else:
    vol_score = 5
    vol_signal = f"成交量偏低({vol_ratio:.1f}%)"

components['成交量'] = {'score': vol_score, 'signal': vol_signal}

# 3. 动量 (最多25分)
recent_4h = prices[-4:]
prev_4h = prices[-8:-4]
momentum = (sum(recent_4h)/4) / (sum(prev_4h)/4) - 1

if momentum > 0.05:
    mom_score = 25
    mom_signal = f"强动量(+{(momentum*100):.1f}%)"
elif momentum > 0.02:
    mom_score = 20
    mom_signal = f"中等动量(+{(momentum*100):.1f}%)"
elif momentum > 0:
    mom_score = 10
    mom_signal = f"微弱动量(+{(momentum*100):.1f}%)"
elif momentum > -0.02:
    mom_score = 5
    mom_signal = f"动量走弱({(momentum*100):.1f}%)"
else:
    mom_score = 0
    mom_signal = f"负动量({(momentum*100):.1f}%)"

components['短期动量'] = {'score': mom_score, 'signal': mom_signal}

# 4. 位置/波动 (最多25分)
high_30d = max(prices)
low_30d = min(prices)
pos = (price - low_30d) / (high_30d - low_30d) * 100 if high_30d > low_30d else 50

# Volatility
returns = [(prices[i]-prices[i-1])/prices[i-1] for i in range(1, len(prices))]
volatility = statistics.stdev(returns) * 100 if len(returns) > 1 else 0

if pos > 85:
    pos_score = 0
    pos_signal = f"超高位置({pos:.1f}%), 回调风险大"
elif pos > 70:
    pos_score = 10
    pos_signal = f"高位区间({pos:.1f}%), 注意风险"
elif pos > 40:
    pos_score = 20
    pos_signal = f"中间位置({pos:.1f}%), 合理"
elif pos > 20:
    pos_score = 15
    pos_signal = f"低位区间({pos:.1f}%), 安全性高"
else:
    pos_score = 25
    pos_signal = f"底部区间({pos:.1f}%), 价值凸显"

components['位置/波动'] = {'score': pos_score, 'signal': pos_signal}

# Total score
total_score = sum(c['score'] for c in components.values())

# Trend strength
if total_score >= 85:
    strength = "极强"
    color = "🟢"
elif total_score >= 70:
    strength = "强"
    color = "🟢"
elif total_score >= 55:
    strength = "中等"
    color = "🟡"
elif total_score >= 40:
    strength = "弱"
    color = "🟠"
else:
    strength = "极弱"
    color = "🔴"

# Output
print("=" * 60)
print("LAB 趋势强度分析")
print("=" * 60)
print(f"💰 价格: ${price:.4f}")
print(f"📈 24h: {change_24h:+.2f}% | 7d: {change_7d:+.2f}%")
print(f"📊 30d高位: ${high_30d:.2f} | 低位: ${low_30d:.2f}")
print()
print("─" * 40)
print("四大维度评分：")
print()
for name, data in components.items():
    bar = "█" * (data['score'] // 5) + "░" * ((25 - data['score']) // 5)
    print(f"  {name}: [{bar}] {data['score']}分")
    print(f"    信号: {data['signal']}")
    print()

print("─" * 40)
print(f"📊 总分: {total_score}/100")
print(f"{color} 趋势强度: {strength}")
print("=" * 60)

# Additional interpretation
print()
print("📋 均线详情:")
print(f"  MA4:  ${ma4:.4f}")
print(f"  MA12: ${ma12:.4f}")
print(f"  MA24: ${ma24:.4f}")
print(f"  MA72: ${ma72:.4f}")
print()
print(f"📋 其他指标:")
print(f"  30d波动率: {volatility:.2f}%")
print(f"  位置: {pos:.1f}% ({(price-low_30d)/(high_30d-low_30d)*100:.1f}%从低到高)")
print()
print("💡 综合判断:")
if total_score >= 70 and above_ma >= 3:
    print("  ✅ 趋势向上，但位置偏高，追高需谨慎")
    print("  ✅ 回踩均线入场更安全")
elif total_score >= 55 and above_ma >= 2:
    print("  ⚠️ 中等趋势，适合区间操作")
    print("  ⚠️ 高抛低吸为主")
elif total_score < 40:
    print("  ❌ 趋势转弱，不宜追多")
    print("  ❌ 等待趋势修复或等待方向明确")