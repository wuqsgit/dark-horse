"""
AlphaDog Crypto Bot - 核心策略引擎
基于 AlphaDog 评分体系的交易信号
"""
import random
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Signal:
    """交易信号"""
    symbol: str
    side: str  # LONG / SHORT
    score: float
    grade: str  # S1/S2/A/B/C/D
    entry_price: float
    stop_loss: float
    take_profit: float
    key_factors: List[str]


class StrategyEngine:
    """策略引擎"""
    
    def __init__(self, config: dict):
        self.threshold = config.get("signal_threshold", 75)
        self.min_volume = config.get("min_volume_24h", 1e8)
        
        # 权重配置
        self.weights = config.get("score_weights", {
            "fundamental": 0.30,
            "technical": 0.30,
            "sentiment": 0.20,
            "risk": 0.20,
        })
    
    def analyze(self, market_data: dict) -> Signal:
        """
        分析市场数据，生成交易信号
        """
        price = market_data["price"]
        change_24h = market_data.get("price_change", 0)
        volume = market_data.get("quote_volume_24h", 0)
        
        # === 计算各维度分数 ===
        
        # 1. 基础面分数
        fundamental = self._calc_fundamental(change_24h, volume)
        
        # 2. 技术面分数  
        technical = self._calc_technical(market_data)
        
        # 3. 情绪面分数
        sentiment = self._calc_sentiment(market_data)
        
        # 4. 风险分数
        risk_score, risk_tags = self._calc_risk(market_data)
        
        # === 计算总分 ===
        total = (
            fundamental * self.weights["fundamental"] +
            technical * self.weights["technical"] +
            sentiment * self.weights["sentiment"] +
            risk_score * self.weights["risk"]
        )
        
        # === 确定评级 ===
        grade, side, factors = self._get_grade(total, change_24h, risk_tags, fundamental, technical, sentiment)
        
        # === 计算止损止盈 ===
        stop_loss = price * (1 - 0.015)  # 1.5% 止损
        take_profit = price * (1 + 0.03)  # 3% 止盈
        
        return Signal(
            symbol=market_data["symbol"],
            side=side,
            score=total,
            grade=grade,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            key_factors=factors
        )
    
    def _calc_fundamental(self, change_24h: float, volume: float) -> float:
        """基础面评分"""
        score = 50.0
        
        # 成交量
        if volume > 1e9:
            score += 20
        elif volume > 1e8:
            score += 15
        elif volume > 1e7:
            score += 10
        
        # 价格变化
        if 1 <= change_24h <= 5:
            score += 15
        elif -3 <= change_24h <= 1:
            score += 10
        elif change_24h < -10:
            score -= 10
        
        return max(0, min(100, score))
    
    def _calc_technical(self, data: dict) -> float:
        """技术面评分"""
        score = 50.0
        
        # 简化版：基于价格位置
        price = data["price"]
        high = data.get("high_24h", price)
        low = data.get("low_24h", price)
        
        # 价格在低位区域
        price_position = (price - low) / (high - low) if high > low else 0.5
        
        if price_position < 0.3:  # 接近低点
            score += 20
        elif price_position < 0.5:
            score += 10
        
        return max(0, min(100, score))
    
    def _calc_sentiment(self, data: dict) -> float:
        """情绪面评分"""
        score = 50.0
        
        change_24h = data.get("price_change", 0)
        
        # 温和上涨利于做多
        if 0 < change_24h < 3:
            score += 15
        elif change_24h < -5:
            score -= 10
        
        return max(0, min(100, score))
    
    def _calc_risk(self, data: dict) -> tuple[float, List[str]]:
        """风险评估"""
        score = 70.0
        tags = []
        
        change_24h = data.get("price_change", 0)
        
        if abs(change_24h) > 15:
            score -= 20
            tags.append("剧烈波动")
        elif abs(change_24h) > 10:
            score -= 10
            tags.append("波动较大")
        
        return max(0, min(100, score)), tags
    
    def _get_grade(self, total: float, change_24h: float, risk_tags: List[str], 
                 f: float, t: float, s: float) -> tuple[str, str, List[str]]:
        """确定评级"""
        
        # 确定方向
        if change_24h > 3 and f > 60:
            side = "LONG"
        elif change_24h < -3:
            side = "SHORT"
        else:
            side = "LONG"  # 默认做多
        
        # 确定评级
        if total >= 80 and not risk_tags:
            grade = "S1"
            factors = ["强势信号"]
        elif total >= 75 and not risk_tags:
            grade = "S2"
            factors = ["信号良好"]
        elif total >= 65:
            grade = "A"
            factors = ["可小仓"]
        elif total >= 55:
            grade = "B"
            factors = ["观察"]
        else:
            grade = "C"
            factors = ["观望"]
        
        # 添加关键因素
        if f > 70:
            factors.append("成交量放大")
        if t > 70:
            factors.append("价格低位")
        
        return grade, side, factors[:2]
    
    def should_entry(self, signal: Signal) -> tuple[bool, str]:
        """判断是否入场"""
        if signal.score >= self.threshold:
            return True, f"信号达标 {signal.score:.1f}"
        
        return False, f"信号不足 {signal.score:.1f} < {self.threshold}"


def create_strategy(config: dict = None) -> StrategyEngine:
    """创建策略引擎"""
    if config is None:
        from config import SIGNAL_THRESHOLD, MIN_VOLUME_24H, SCORE_WEIGHTS
        config = {
            "signal_threshold": SIGNAL_THRESHOLD,
            "min_volume_24h": MIN_VOLUME_24H,
            "score_weights": SCORE_WEIGHTS,
        }
    
    return StrategyEngine(config)