#!/usr/bin/env python3
import requests
proxies = {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}

# Get LAB historical data
r = requests.get('https://api.coingecko.com/api/v3/coins/lab/market_chart?vs_currency=usd&days=30', proxies=proxies, timeout=10)
d = r.json()
prices_data = d.get('prices', [])
prices = [p[1] for p in prices_data]

high = max(prices)
low = min(prices)
current = prices[-1]

print("=" * 60)
print("LAB 多仓爆仓价格分析")
print("=" * 60)
print(f"当前价格: ${current:.4f}")
print(f"30d最高: ${high:.4f}")
print(f"30d最低: ${low:.4f}")
print()

# If entry was at the 30d high
entry_high = high
print("=" * 60)
print("如果在最高点 $5.13 入场做多:")
print("=" * 60)
leverages = [3, 5, 10, 15, 20]
for lev in leverages:
    # For futures perp: liquidation = entry * (1 - 1/lev) assuming 100% margin
    # Actually standard is: liquidation when mark price < bankruptcy price
    # Long liquidation = entry * (1 - maintenance_margin / leverage)
    # Let's assume 1% maintenance margin (common on Binance)
    # liq = entry * (1 - (1 - 0.01) / leverage) = entry * ((leverage - 1 + 0.01) / leverage)
    # Simplified: liq = entry * (1 - 1/leverage) approximately
    
    liq_price = entry_high * (1 - 1.0/lev)
    drop_pct = (entry_high - liq_price) / entry_high * 100
    print(f"{lev}x杠杆: 爆仓价 ${liq_price:.4f} (跌幅 {drop_pct:.1f}%)")

print()
print("=" * 60)
print("如果当前价格入场做多 (现价 $4.83):")
print("=" * 60)

entry_now = current
for lev in leverages:
    liq_price = entry_now * (1 - 1.0/lev)
    drop_pct = (entry_now - liq_price) / entry_now * 100
    print(f"{lev}x杠杆: 爆仓价 ${liq_price:.4f} (跌幅 {drop_pct:.1f}%)")

print()
print("=" * 60)
print("90%仓位(约10x杠杆)爆仓条件:")
print("=" * 60)
print(f"最高点入场: 10x做多 → 爆仓价 ${high * 0.9:.4f}")
print(f"当前入场: 10x做多 → 爆仓价 ${current * 0.9:.4f}")
print()
print(f"从$5.13跌到 ${high * 0.9:.4f} = 跌10.2%就会爆仓")
print(f"从$4.83跌到 ${current * 0.9:.4f} = 跌11.1%就会爆仓")
print()
print("⚠️ 注意: 实际爆仓还会受资金费率、杠杆调整等因素影响")
print("⚠️ 如果币安没有LAB永续合约，需要去其他交易所(OKX/Bybit/Hyperliquid)")
print("⚠️ 数据基于30d历史价格，实际最高点可能更高")