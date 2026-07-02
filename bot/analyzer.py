#!/usr/bin/env python3
"""
AlphaDog 分析工具 - 币种走势分析
"""
import requests
import json
from datetime import datetime
from typing import Dict, List


# 常用币种
COINS = [
    "BTC", "ETH", "BNB", "XRP", "ADA", "DOGE", "SOL", "DOT", "MATIC", "SHIB",
    "LTC", "TRX", "AVAX", "LINK", "ATOM", "UNI", "XMR", "ETC", "XLM", "BCH",
    "FIL", "APT", "NEAR", "ALGO", "VET", "ICP", "FTM", "AAVE", "EGLD", "AXS",
]

# 代理配置
PROXIES = {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}


class Analyzer:
    """分析器"""
    
    def __init__(self, proxies=None):
        self.base_url = "https://api.binance.com"
        self.proxies = proxies or PROXIES
    
    def get_ticker(self, symbol: str) -> Dict:
        """获取24h行情"""
        url = f"{self.base_url}/api/v3/ticker/24hr"
        params = {"symbol": f"{symbol}USDT"}
        r = requests.get(url, params=params, proxies=self.proxies, timeout=5)
        return r.json()
    
    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List:
        """获取K线"""
        url = f"{self.base_url}/api/v3/klines"
        params = {"symbol": f"{symbol}USDT", "interval": interval, "limit": limit}
        r = requests.get(url, params=params, proxies=self.proxies, timeout=5)
        return r.json()
    
    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        """获取订单簿"""
        url = f"{self.base_url}/api/v3/depth"
        params = {"symbol": f"{symbol}USDT", "limit": limit}
        r = requests.get(url, params=params, proxies=self.proxies, timeout=5)
        return r.json()
    
    def get_funding(self, symbol: str) -> Dict:
        """获取资金费率"""
        url = f"{self.base_url}/api/v3/premiumIndex"
        params = {"symbol": f"{symbol}USDT"}
        try:
            r = requests.get(url, params=params, proxies=self.proxies, timeout=5)
            return r.json()
        except:
            return {}


class StrategyAnalyzer:
    """策略分析"""
    
    def analyze(self, symbol: str, ticker: Dict, klines: List, funding: Dict = None) -> Dict:
        """综合分析"""
        price = float(ticker["lastPrice"])
        change = float(ticker["priceChangePercent"])
        volume = float(ticker["quoteVolume"])
        high = float(ticker["highPrice"])
        low = float(ticker["lowPrice"])
        
        # === K线分析 ===
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        
        # 计算均线
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else price
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else price
        
        # 趋势判断
        trend = self._calc_trend(ma5, ma20, ma60, price)
        
        # 强度计算
        strength = self._calc_strength(change, volume, klines)
        
        # 波动性
        volatility = self._calc_volatility(closes)
        
        # 支撑阻力
        sr = self._calc_sr(highs, lows)
        support = sr['support']
        resistance = sr['resistance']
        
        # 资金费率
        funding_rate = 0
        if funding:
            funding_rate = float(funding.get("lastFundingRate", 0)) * 100
        
        # 入场分析
        entry_signal = self._calc_entry_signal(trend, strength, change, funding_rate)
        
        return {
            "symbol": symbol,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price": price,
            "change_24h": change,
            "volume_24h": volume,
            "high_24h": high,
            "low_24h": low,
            "ma5": round(ma5, 4),
            "ma20": round(ma20, 4),
            "ma60": round(ma60, 4) if ma60 != price else None,
            "trend": trend,
            "strength": strength,
            "volatility": volatility,
            "support": support,
            "resistance": resistance,
            "funding_rate": funding_rate,
            "entry_signal": entry_signal,
        }
    
    def _calc_trend(self, ma5: float, ma20: float, ma60: float, price: float) -> Dict:
        """趋势判断"""
        signals = []
        
        if price > ma5:
            signals.append("price_above_ma5")
        if ma5 > ma20:
            signals.append("ma5_above_ma20")
        if ma20 > ma60 if ma60 != price else True:
            signals.append("ma20_above_ma60")
        
        # 判断
        if len(signals) >= 2 and "price_above_ma5" in signals:
            trend = "上涨"
            confidence = "高"
        elif len(signals) == 0:
            trend = "下跌"
            confidence = "高"
        else:
            trend = "震荡"
            confidence = "中"
        
        return {"direction": trend, "signals": signals, "confidence": confidence}
    
    def _calc_strength(self, change: float, volume: float, klines: List) -> Dict:
        """强度计算"""
        # 成交量对比
        recent_volume = sum(float(k[5]) for k in klines[-5:]) / 5
        avg_volume = sum(float(k[5]) for k in klines) / len(klines) if klines else 1
        volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1
        
        # 评分
        score = 50
        
        # 涨跌
        if change > 3:
            score += 20
        elif change > 1:
            score += 10
        elif change < -5:
            score -= 15
        elif change < -3:
            score -= 10
        
        # 成交量
        if volume_ratio > 1.5:
            score += 15
        elif volume_ratio > 1.2:
            score += 10
        elif volume_ratio < 0.7:
            score -= 10
        
        score = max(0, min(100, score))
        
        if score >= 75:
            level = "强"
        elif score >= 55:
            level = "中"
        else:
            level = "弱"
        
        return {"score": score, "level": level, "volume_ratio": round(volume_ratio, 2)}
    
    def _calc_volatility(self, closes: List) -> Dict:
        """波动性"""
        if len(closes) < 2:
            return {"value": 0, "level": "低"}
        
        import statistics
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        vol = statistics.stdev(returns) if len(returns) > 1 else 0
        
        if vol > 0.05:
            level = "极高"
        elif vol > 0.03:
            level = "高"
        elif vol > 0.015:
            level = "中"
        else:
            level = "低"
        
        return {"value": round(vol * 100, 2), "level": level}
    
    def _calc_sr(self, highs: List, lows: List) -> Dict:
        """支撑阻力"""
        if not highs:
            return {"support": 0, "resistance": 0}
        
        # 简化：取最近的高低
        resistance = float(max(highs[-5:]))
        support = float(min(lows[-5:]))
        
        return {"support": support, "resistance": resistance}
    
    def _calc_entry_signal(self, trend: Dict, strength: Dict, change: float, funding_rate: float) -> Dict:
        """入场信号"""
        signals = []
        score = 50
        
        # 趋势
        if trend["direction"] == "上涨":
            score += 20
            signals.append("趋势上涨")
        elif trend["direction"] == "下跌":
            score -= 15
            signals.append("趋势下跌")
        
        # 强度
        if strength["level"] == "强":
            score += 15
            signals.append("强度强")
        elif strength["level"] == "弱":
            score -= 10
            signals.append("强度弱")
        
        # 位置
        if -3 <= change <= 1:
            score += 10
            signals.append("回调位置")
        elif change > 5:
            score -= 10
            signals.append("追高风险")
        
        # 资金费率
        if funding_rate > 0.01:  # 高费率
            score -= 10
            signals.append("高费率")
        elif funding_rate < 0:
            score += 5
            signals.append("多头补贴")
        
        score = max(0, min(100, score))
        
        # 建议
        if score >= 75:
            action = "S1-强烈买入"
        elif score >= 65:
            action = "S2-建议买入"
        elif score >= 55:
            action = "A-可小仓"
        elif score >= 45:
            action = "B-观望"
        else:
            action = "C-不建议"
        
        return {"score": score, "action": action, "reasons": signals}


def analyze_coin(symbol: str, analyzer: Analyzer = None) -> Dict:
    """分析单个币"""
    if analyzer is None:
        analyzer = Analyzer()
    
    # 获取数据
    ticker = analyzer.get_ticker(symbol)
    klines = analyzer.get_klines(symbol, "1h", 100)
    funding = analyzer.get_funding(symbol)
    
    # 分析
    strategy = StrategyAnalyzer()
    result = strategy.analyze(symbol, ticker, klines, funding)
    
    return result


def print_report(result: Dict):
    """打印报告"""
    print("=" * 50)
    print(f"📊 {result['symbol']} 分析报告")
    print("=" * 50)
    print(f"更新时间: {result['update_time']}")
    print(f"💰 价格: ${result['price']:,.4f} ({result['change_24h']:+.2f}%)")
    print(f"📈 24h成交量: ${result['volume_24h']:,.0f}")
    print()
    print("─" * 30)
    print("📉 均线信号:")
    print(f"  MA5:  ${result['ma5']:,.4f}")
    print(f"  MA20: ${result['ma20']:,.4f}")
    if result['ma60']:
        print(f"  MA60: ${result['ma60']:,.4f}")
    print()
    print(f"  📌 趋势: {result['trend']['direction']} (置信度: {result['trend']['confidence']})")
    print(f"  📌 强度: {result['strength']['level']} ({result['strength']['score']}分)")
    print(f"  📌 波动: {result['volatility']['level']} ({result['volatility']['value']}%)")
    print()
    print("─" * 30)
    print("🎯 支撑阻力:")
    print(f"  支撑: ${float(result['support']):,.4f}")
    print(f"  阻力: ${float(result['resistance']):,.4f}")
    print()
    print("─" * 30)
    print("💵 资金费率:")
    print(f"  {result['funding_rate']:.4f}%")
    print()
    print("─" * 30)
    print("🚦 入场信号:")
    print(f"  分数: {result['entry_signal']['score']}")
    print(f"  ���议: {result['entry_signal']['action']}")
    print(f"  理由: {', '.join(result['entry_signal']['reasons'])}")
    print("=" * 50)


def main():
    """主程序"""
    import sys
    
    analyzer = Analyzer()
    
    if len(sys.argv) > 1:
        # 分析指定币
        symbol = sys.argv[1].upper()
        result = analyze_coin(symbol, analyzer)
        print_report(result)
    else:
        # 列出可用币种
        print("可用币种:", ", ".join(COINS[:10]))
        print("用法: python3 analyzer.py <SYMBOL>")
        print("例如: python3 analyzer.py BTC")


if __name__ == "__main__":
    main()