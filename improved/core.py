#!/usr/bin/env python3
"""AlphaDog Improved - 量化研究平台核心"""
import os
import sys
import json
import time
import random
import sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path

# Alpha 币池
ALPHA_COINS = [
    "BTC", "ETH", "BNB", "XRP", "ADA", "DOGE", "SOL", "DOT", "MATIC", "SHIB",
    "LTC", "TRX", "AVAX", "LINK", "ATOM", "UNI", "XMR", "ETC", "XLM", "BCH",
    "FIL", "APT", "NEAR", "ALGO", "VET", "ICP", "FTM", "AAVE", "EGLD", "AXS",
    "THETA", "EOS", "XTZ", "CAKE", "GRT", "SNX", "NEO", "KLAY", "ZEC", "ENJ",
    "COMP", "BAT", "DASH", "ZIL", "WAVES", "YFI", "MINA", "REN", "1INCH", "CHZ",
    "CRV", "KSM", "CEL", "QTUM", "HOT", "ZRX", "SKL", "ANKR", "SUSHI", "STORJ",
    "BTT", "SAND", "MANA", "GALA", "AXL", "ROSE", "KAVA", "LUNA", "ENS", "CRO",
    "OKB", "GT", "LDO", "ARB", "OP", "PEPE", "WLD", "INJ", "IMX", "RARE",
    "GMR", "HOOK", "MEW", "ACT", "PNUT", "MUBI", "BILL", "COLLECT", "FIGHT", "SIREN",
]

@dataclass
class CoinData:
    symbol: str
    price: float
    price_change_24h: float
    volume_24h: float
    market_cap: float
    funding_rate: float = 0.0
    oi_change_24h: float = 0.0
    whale_long_ratio: float = 0.5
    whale_short_ratio: float = 0.3
    cex_inflow_24h: float = 0.0
    cex_inflow_14d: float = 0.0
    top20_change_14d: float = 0.0
    social_score: float = 0.0
    volatility: float = 0.0
    risk_tags: List[str] = field(default_factory=list)

@dataclass
class CoinScore:
    symbol: str
    total_score: float
    fundamental_score: float
    technical_score: float
    sentiment_score: float
    risk_score: float
    recommendation: str
    interpretation: str
    chart_pattern: str
    risk_level: str
    confidence: str = "中"
    key_factors: List[str] = field(default_factory=list)

class ScoringEngine:
    def __init__(self):
        self.weights = {"fundamental": 0.3, "technical": 0.3, "sentiment": 0.2, "risk": 0.2}
    
    def calculate_scores(self, coin: CoinData) -> CoinScore:
        fundamental = self._calc_fundamental(coin)
        technical = self._calc_technical(coin)
        sentiment = self._calc_sentiment(coin)
        risk_score, risk_tags = self._calc_risk(coin)
        
        total = (fundamental * self.weights["fundamental"] +
                technical * self.weights["technical"] +
                sentiment * self.weights["sentiment"] +
                risk_score * self.weights["risk"])
        
        recommendation = self._get_recommendation(total, coin, risk_tags)
        factors = self._get_key_factors(coin, fundamental, technical, sentiment)
        
        return CoinScore(
            symbol=coin.symbol,
            total_score=round(total, 1),
            fundamental_score=round(fundamental, 1),
            technical_score=round(technical, 1),
            sentiment_score=round(sentiment, 1),
            risk_score=round(risk_score, 1),
            recommendation=recommendation["grade"],
            interpretation=recommendation["interpretation"],
            chart_pattern=recommendation["pattern"],
            risk_level=recommendation["risk_level"],
            confidence=recommendation["confidence"],
            key_factors=factors,
        )
    
    def _calc_fundamental(self, coin: CoinData) -> float:
        score = 50.0
        if coin.volume_24h > 1e9: score += 20
        elif coin.volume_24h > 1e8: score += 15
        elif coin.volume_24h > 1e7: score += 10
        change = coin.price_change_24h
        if 1 <= change <= 5: score += 15
        elif -3 <= change <= 1: score += 10
        elif change < -10: score -= 10
        if coin.cex_inflow_24h > 0: score += 10
        elif coin.cex_inflow_24h < 0: score -= 5
        if coin.top20_change_14d > 3: score += 5
        elif coin.top20_change_14d < -3: score -= 5
        return max(0, min(100, score))
    
    def _calc_technical(self, coin: CoinData) -> float:
        score = 50.0
        if -0.001 < coin.funding_rate < 0: score += 15
        elif 0 < coin.funding_rate < 0.001: score += 5
        elif coin.funding_rate >= 0.001: score -= 10
        if coin.oi_change_24h > 10: score += 10
        elif coin.oi_change_24h < -10: score -= 10
        if coin.whale_long_ratio > coin.whale_short_ratio + 0.2: score += 15
        elif coin.whale_long_ratio < coin.whale_short_ratio: score -= 10
        return max(0, min(100, score))
    
    def _calc_sentiment(self, coin: CoinData) -> float:
        score = 50.0
        if coin.social_score > 80: score += 20
        elif coin.social_score > 50: score += 10
        elif coin.social_score < 20: score -= 10
        if 0.3 < coin.volatility < 0.7: score += 10
        elif coin.volatility > 0.9: score -= 15
        return max(0, min(100, score))
    
    def _calc_risk(self, coin: CoinData) -> Tuple[float, List[str]]:
        score = 70.0
        tags = []
        if coin.volatility > 0.8: score -= 20; tags.append("高波动")
        if coin.funding_rate > 0.001: score -= 15; tags.append("杠杆拥挤")
        if coin.cex_inflow_24h < -50: score -= 10; tags.append("资金流出")
        if coin.price_change_24h < -15: score -= 20; tags.append("大跌")
        return max(0, min(100, score)), tags
    
    def _get_recommendation(self, total: float, coin: CoinData, risk_tags: List[str]) -> Dict:
        if coin.price_change_24h > 3 and coin.volume_24h > 1e8: pattern = "吸筹拉盘"
        elif coin.price_change_24h < -3: pattern = "疑似出货"
        else: pattern = "中性震荡"
        risk_level = "正常" if not risk_tags else "/".join(risk_tags[:2])
        
        if total >= 80 and not risk_tags: grade, interpretation, confidence = "S1", "重点参与", "高"
        elif total >= 75 and not risk_tags: grade, interpretation, confidence = "S2", "可参与", "高"
        elif total >= 65: grade, interpretation, confidence = "A", "可小仓", "中"
        elif total >= 55: grade, interpretation, confidence = "B", "观察", "中"
        elif total >= 45: grade, interpretation, confidence = "C", "观望", "低"
        else: grade, interpretation, confidence = "D", "不推荐", "低"
        
        return {"grade": grade, "interpretation": interpretation, "pattern": pattern, "risk_level": risk_level, "confidence": confidence}
    
    def _get_key_factors(self, coin: CoinData, f: float, t: float, s: float) -> List[str]:
        factors = []
        if f > 70: factors.append("成交量放大")
        if t > 70 and coin.whale_long_ratio > coin.whale_short_ratio: factors.append("巨鲸看多")
        if s > 70: factors.append("社区热度高")
        if coin.cex_inflow_24h > 0: factors.append("CEX净流入")
        if coin.funding_rate < 0: factors.append("多头补贴")
        return factors[:3]

class BacktestEngine:
    def __init__(self):
        self.db_path = Path(__file__).parent / "alphadog.db"
    
    def save_scan(self, scan_data: dict):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, score REAL, grade TEXT)''')
        c.execute('''INSERT INTO scans (timestamp, symbol, score, grade) VALUES (?, ?, ?, ?)''',
                 (scan_data.get("timestamp"), scan_data.get("symbol"), scan_data.get("score"), scan_data.get("grade")))
        conn.commit()
        conn.close()
    
    def run_backtest(self, signals: List[dict], take_profit: float = 0.12, stop_loss: float = 0.08, max_holding_hours: int = 72) -> dict:
        results = []
        for signal in signals:
            entry_price = signal.get("entry_price", 1.0)
            holding_hours = signal.get("holding_hours", 24)
            price_exit = signal.get("price_exit", entry_price)
            return_pct = (price_exit - entry_price) / entry_price
            if return_pct >= take_profit: reason = "take_profit"
            elif return_pct <= -stop_loss: reason = "stop_loss"
            elif holding_hours >= max_holding_hours: reason = "time"
            else: reason = "manual"
            results.append({"return_pct": return_pct * 100, "reason": reason})
        
        if not results:
            return {"trade_count": 0, "win_rate": 0, "avg_return": 0}
        
        winning = sum(1 for r in results if r["return_pct"] > 0)
        total_return = sum(r["return_pct"] for r in results)
        
        return {
            "trade_count": len(results),
            "win_rate": winning / len(results) * 100,
            "avg_return": total_return / len(results),
            "sample_warning": f"⚠️ 样本不足" if len(results) < 30 else "",
        }