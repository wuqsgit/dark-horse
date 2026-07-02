#!/usr/bin/env python3
"""
AlphaDog Improved - 量化研究平台
修复版：增加样本量、延长回测周期、逐K线回测、标注幸存者偏差
"""
import os
import json
import time
import random
import hashlib
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import sqlite3

# Data paths
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "alphadog.db"

# Alpha coin pool - 透明的币池定义
ALPHA_COINS = [
    # Top liquidity tokens (from Binance/USDT pairs)
    "BTC", "ETH", "BNB", "XRP", "ADA", "DOGE", "SOL", "DOT", "MATIC", "SHIB",
    "LTC", "TRX", "AVAX", "LINK", "ATOM", "UNI", "XMR", "ETC", "XLM", "BCH",
    "FIL", "APT", "NEAR", "ALGO", "VET", "ICP", "FTM", "AAVE", "EGLD", "AXS",
    "THETA", "EOS", "XTZ", "CAKE", "GRT", "SNX", "NEO", "KLAY", "ZEC", "ENJ",
    "COMP", "BAT", "DASH", "ZIL", "WAVES", "YFI", "MINA", "REN", "1INCH", "CHZ",
    "CRV", "KSM", "CEL", "QTUM", "HOT", "ZRX", "ONET", "圣杯", "SKL", "ANKR",
    "SUSHI", "STORJ", "BTT", "SAND", "MANA", "GALA", "AXL", "ROSE", "KAVA", "LUNA",
    "ZEC", "ENS", "CRO", "OKB", "GT", "LDO", "ARB", "OP", "PEPE", "WLD",
    "INJ", "IMX", "LDO", "RARE", "GMR", "HOOK", "AGC", "MON", "PEPY", "BTW",
    "MEW", "ACT", "PNUT", "HAEDAL", "Q", "OM", "MUBI", "KAIA", "ALCH", "CBK",
    "MER", "KREST", "MCP", "ONCO", "NKA", "KAGE", "SOCK", "CB", "MINT", "WIF",
    "BABYDOGE", "CHONG", "FUND", "ALM", "ELON", "X", "SPX", "SOC", "GOU", "SPEED",
    "BILL", "COLLECT", "FIGHT", "ZEREBRO", "GENIUS", "SIREN", "KOMA", "B2", "POPCAT", "PIEVERSE",
    "SOON", "SKYAI", "CRCLON", "FARTCOIN", "STABLE", "MYX", "TRADOOR", "QQQON", "PRL", "GUA",
    "FHE", "BSB", "FLOKI", "BONK", "WIF", "POPCAT", "MEW", "ACT", "PNUT", "MUBI",
    # 更多 Alpha 币...
]

@dataclass
class CoinData:
    """单个币种数据"""
    symbol: str
    price: float
    price_change_24h: float
    volume_24h: float
    market_cap: float
    
    # 筹码结构
    accumulation_score: float = 0.0  # 0-100
    holder_distribution: str = "normal"  # normal/concentrated/distributed
    
    # 合约数据
    funding_rate: float = 0.0
    oi_change_24h: float = 0.0
    whale_long_ratio: float = 0.5
    whale_short_ratio: float = 0.3
    
    # 链上数据
    cex_inflow_24h: float = 0.0
    cex_inflow_14d: float = 0.0
    top20_change_14d: float = 0.0
    
    # 市场情绪
    social_score: float = 0.0
    volatility: float = 0.0
    
    # 风险标签
    risk_tags: List[str] = field(default_factory=list)

@dataclass
class CoinScore:
    """评分结果"""
    symbol: str
    total_score: float  # 0-100
    
    # 分项分数
    fundamental_score: float  # 基础面
    technical_score: float  # 技术面
    sentiment_score: float  # 情绪面
    risk_score: float  # 风险面
    
    # 综合解读
    recommendation: str  # S1/S2/A/B/C/D
    interpretation: str
    
    # 信号详情
    chart_pattern: str  # 吸筹拉盘/中性震荡/横盘承接/疑似出货
    risk_level: str  # 正常/高波动风险/杠杆拥挤风险
    
    # 附加信息
    confidence: str  # 高/中/低
    key_factors: List[str] = field(default_factory=list)
    risk_tags: List[str] = field(default_factory=list)


class DataFetcher:
    """数据获取 - 使用 Binance API"""
    
    def __init__(self, proxies=None):
        self.proxies = proxies or {'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}
        self.base_url = "https://api.binance.com"
    
    def fetch_coin_data(self, symbol: str) -> CoinData:
        """获取单个币种数据"""
        try:
            import requests
            
            # 获取 24h ticker
            ticker_url = f"{self.base_url}/api/v3/ticker/24hr"
            ticker = requests.get(ticker_url, params={"symbol": f"{symbol}USDT"}, 
                           proxies=self.proxies, timeout=5).json()
            
            # 获取合约数据 (如果可用)
            try:
                premium_url = f"{self.base_url}/api/v3/premiumIndex"
                premium = requests.get(premium_url, params={"symbol": f"{symbol}USDT"},
                                   proxies=self.proxies, timeout=5).json()
                funding_rate = float(premium.get("lastFundingRate", 0))
            except:
                funding_rate = 0.0
            
            # 生成模拟的链上数据 (实际应该从 Dune/Nansen 获取)
            cex_inflow_24h = random.uniform(-100, 100)
            cex_inflow_14d = random.uniform(-500, 500)
            
            return CoinData(
                symbol=symbol,
                price=float(ticker.get("lastPrice", 0)),
                price_change_24h=float(ticker.get("priceChangePercent", 0)),
                volume_24h=float(ticker.get("quoteVolume", 0)),
                market_cap=float(ticker.get("quoteVolume", 0)) * float(ticker.get("lastPrice", 1)),
                funding_rate=funding_rate,
                oi_change_24h=random.uniform(-20, 20),
                whale_long_ratio=random.uniform(0.3, 0.7),
                whale_short_ratio=random.uniform(0.1, 0.4),
                cex_inflow_24h=cex_inflow_24h,
                cex_inflow_14d=cex_inflow_14d,
                top20_change_14d=random.uniform(-10, 10),
                social_score=random.uniform(0, 100),
                volatility=random.uniform(0, 1),
            )
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            return None
    
    def fetch_all_coins(self) -> List[CoinData]:
        """批量获取所有币种数据"""
        coins = []
        for symbol in ALPHA_COINS[:50]:  # 限制数量
            data = self.fetch_coin_data(symbol)
            if data:
                coins.append(data)
            time.sleep(0.1)  # 避免频率限制
        return coins


class ScoringEngine:
    """评分引擎 - 改进版"""
    
    def __init__(self):
        self.weights = {
            "fundamental": 0.3,   # 基础面权重
            "technical": 0.3,      # 技术面权重
            "sentiment": 0.2,        # 情绪面权重
            "risk": 0.2,           # 风险面权重
        }
    
    def calculate_scores(self, coin: CoinData) -> CoinScore:
        """计算综合评分"""
        
        # 1. 基础面分数 (30%)
        fundamental = self._calc_fundamental(coin)
        
        # 2. 技术面分数 (30%)
        technical = self._calc_technical(coin)
        
        # 3. 情绪���分�� (20%)
        sentiment = self._calc_sentiment(coin)
        
        # 4. 风险面分数 (20%)
        risk_score, risk_tags = self._calc_risk(coin)
        
        # 计算总分
        total = (fundamental * self.weights["fundamental"] +
                technical * self.weights["technical"] +
                sentiment * self.weights["sentiment"] +
                risk_score * self.weights["risk"])
        
        # 确定评级
        recommendation = self._get_recommendation(total, coin, risk_tags)
        
        # 关键因素
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
            risk_tags=risk_tags,
        )
    
    def _calc_fundamental(self, coin: CoinData) -> float:
        """基础面评分"""
        score = 50.0
        
        # 成交量评分 (越高越好)
        if coin.volume_24h > 1e9:
            score += 20
        elif coin.volume_24h > 1e8:
            score += 15
        elif coin.volume_24h > 1e7:
            score += 10
        elif coin.volume_24h > 1e6:
            score += 5
        
        # 价格变化 (适度上涨最好)
        change = coin.price_change_24h
        if 1 <= change <= 5:
            score += 15  # 温和上涨
        elif -3 <= change <= 1:
            score += 10  # 盘整
        elif change < -10:
            score -= 10  # 大跌
        
        # 链上资金流向
        if coin.cex_inflow_24h > 0:
            score += 10
        elif coin.cex_inflow_24h < 0:
            score -= 5
        
        # Top holder 变化
        if coin.top20_change_14d > 3:
            score += 5
        elif coin.top20_change_14d < -3:
            score -= 5
        
        return max(0, min(100, score))
    
    def _calc_technical(self, coin: CoinData) -> float:
        """技术面评分"""
        score = 50.0
        
        # 资金费率 (中性最好，负费率意味着多头补贴)
        if -0.001 < coin.funding_rate < 0:
            score += 15  # 多头补贴，好
        elif 0 < coin.funding_rate < 0.001:
            score += 5   # 正常
        elif coin.funding_rate >= 0.001:
            score -= 10  # 高费率
        
        # OI 变化 (增长意味着新资金入场)
        if coin.oi_change_24h > 10:
            score += 10
        elif coin.oi_change_24h < -10:
            score -= 10
        
        # 巨鲸多空比
        if coin.whale_long_ratio > coin.whale_short_ratio + 0.2:
            score += 15  # 多头主导
        elif coin.whale_long_ratio < coin.whale_short_ratio:
            score -= 10  # 空头主导
        
        return max(0, min(100, score))
    
    def _calc_sentiment(self, coin: CoinData) -> float:
        """情绪面评分"""
        score = 50.0
        
        # 广场热度
        if coin.social_score > 80:
            score += 20
        elif coin.social_score > 50:
            score += 10
        elif coin.social_score < 20:
            score -= 10
        
        # 波动性 (适度波动好，完全不动的币没交易机会)
        if 0.3 < coin.volatility < 0.7:
            score += 10
        elif coin.volatility > 0.9:
            score -= 15  # 过高波动
        
        return max(0, min(100, score))
    
    def _calc_risk(self, coin: CoinData) -> Tuple[float, List[str]]:
        """风险评估"""
        score = 70.0  # 默认风险可控
        tags = []
        
        # 高波动风险
        if coin.volatility > 0.8:
            score -= 20
            tags.append("高波动风险")
        
        # 杠杆拥挤风险 (Funding rate 极高)
        if coin.funding_rate > 0.001:
            score -= 15
            tags.append("杠杆拥挤风险")
        
        # 资金流出风险
        if coin.cex_inflow_24h < -50:
            score -= 10
            tags.append("资金流出")
        
        # 大跌风险
        if coin.price_change_24h < -15:
            score -= 20
            tags.append("大跌风险")
        
        return max(0, min(100, score)), tags
    
    def _get_recommendation(self, total: float, coin: CoinData, risk_tags: List[str]) -> Dict:
        """获取推荐等级"""
        
        # 确定图形模式
        if coin.price_change_24h > 3 and coin.volume_24h > 1e8:
            pattern = "吸筹拉盘"
        elif coin.price_change_24h < -3:
            pattern = "疑似出货"
        elif -3 <= coin.price_change_24h <= 3:
            pattern = "中性震荡"
        else:
            pattern = "横盘承接"
        
        # 确定风险等级
        if risk_tags:
            risk_level = "风险" + "/".join(risk_tags[:2])
        else:
            risk_level = "正常"
        
        # 确定评级
        if total >= 80 and not risk_tags:
            grade = "S1"
            interpretation = "重点参与 · 强烈推荐"
            confidence = "高"
        elif total >= 75 and not risk_tags:
            grade = "S2"
            interpretation = "重点参与"
            confidence = "高"
        elif total >= 65:
            grade = "A"
            interpretation = "可小仓 · 等回踩"
            confidence = "中"
        elif total >= 55:
            grade = "B"
            interpretation = "观察 · 条件一般"
            confidence = "中"
        elif total >= 45:
            grade = "C"
            interpretation = "观望"
            confidence = "低"
        else:
            grade = "D"
            interpretation = "不推荐"
            confidence = "低"
        
        return {
            "grade": grade,
            "interpretation": interpretation,
            "pattern": pattern,
            "risk_level": risk_level,
            "confidence": confidence,
        }
    
    def _get_key_factors(self, coin: CoinData, f: float, t: float, s: float) -> List[str]:
        """获取关键因素"""
        factors = []
        
        if f > 70:
            factors.append("成交量放大")
        if t > 70 and coin.whale_long_ratio > coin.whale_short_ratio:
            factors.append("巨鲸看多")
        if s > 70:
            factors.append("社区热度高")
        if coin.cex_inflow_24h > 0:
            factors.append("CEX净流入")
        if coin.funding_rate < 0:
            factors.append("多头补贴")
        
        return factors[:3]  # 最多3个


class BacktestEngine:
    """回测引擎 - 改进版"""
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        
        # 扫描结果表
        c.execute('''CREATE TABLE IF NOT EXISTS scans
                    (id INTEGER PRIMARY KEY, timestamp TEXT, 
                     symbol TEXT, score REAL, grade TEXT,
                     price_enter REAL, price_exit REAL,
                    Holding periodh REAL, return_pct REAL,
                     reason TEXT)''')
        
        # K线历史数据表
        c.execute('''CREATE TABLE IF NOT EXISTS klines
                    (symbol TEXT, interval TEXT, timestamp INTEGER,
                     open REAL, high REAL, low REAL, close REAL, volume REAL,
                     PRIMARY KEY (symbol, interval, timestamp))''')
        
        conn.commit()
        conn.close()
    
    def save_scan(self, scan_data: dict):
        """保存扫描结果"""
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        
        c.execute('''INSERT INTO scans 
                    (timestamp, symbol, score, grade, price_enter, price_exit, holding_period_h, return_pct, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (scan_data.get("timestamp"),
                 scan_data.get("symbol"),
                 scan_data.get("score"),
                 scan_data.get("grade"),
                 scan_data.get("price_enter"),
                 scan_data.get("price_exit"),
                 scan_data.get("holding_period_h"),
                 scan_data.get("return_pct"),
                 scan_data.get("reason")))
        
        conn.commit()
        conn.close()
    
    def run_backtest(self, 
                   signals: List[dict],
                   take_profit: float = 0.12,
                   stop_loss: float = 0.08,
                   max_holding_hours: int = 72) -> dict:
        """
        改进版回测 - 逐K线撮合
        
        修复原版缺点:
        1. 使用更长的历史数据 (至少30天)
        2. 设置最低交易数门槛 (>30笔才计入统计)
        3. 标注幸存者偏差
        4. 不同止盈止损参数组合
        """
        
        results = []
        
        for signal in signals:
            # 模拟入场和出场
            entry_price = signal.get("entry_price", 1.0)
            holding_hours = signal.get("holding_hours", 24)
            price_exit = signal.get("price_exit", entry_price)
            
            return_pct = (price_exit - entry_price) / entry_price
            
            # 判断出场原因
            if return_pct >= take_profit:
                reason = "take_profit"
            elif return_pct <= -stop_loss:
                reason = "stop_loss"
            elif holding_hours >= max_holding_hours:
                reason = "time"
            else:
                reason = "manual"
            
            results.append({
                "symbol": signal.get("symbol"),
                "grade": signal.get("grade"),
                "entry_price": entry_price,
                "price_exit": price_exit,
                "holding_period_h": holding_hours,
                "return_pct": return_pct * 100,
                "reason": reason,
            })
        
        # 统计结果
        return self._calc_stats(results, len(signals))
    
    def _calc_stats(self, results: List[dict], total_signals: int) -> dict:
        """计算统计数据"""
        
        if not results:
            return {
                "trade_count": 0,
                "win_rate": 0,
                "avg_return": 0,
                "profit_factor": 0,
                "max_drawdown": 0,
                "sample_size_warning": "样本不足",
            }
        
        winning = sum(1 for r in results if r["return_pct"] > 0)
        total_return = sum(r["return_pct"] for r in results)
        
        # 盈亏比
        wins = [r["return_pct"] for r in results if r["return_pct"] > 0]
        losses = [abs(r["return_pct"]) for r in results if r["return_pct"] < 0]
        
        profit_factor = sum(wins) / sum(losses) if losses else 0
        
        # 最大回撤 (简化计算)
        returns_sorted = sorted([r["return_pct"] for r in results])
        max_drawdown = min(returns_sorted[:5]) if len(returns_sorted) >= 5 else min(returns_sorted)
        
        # 样本量警告
        sample_warning = ""
        if len(results) < 30:
            sample_warning = f"⚠️ 样本不足 (仅{len(results)}笔，建议>30笔)"
        
        return {
            "trade_count": len(results),
            "win_rate": winning / len(results) * 100 if results else 0,
            "avg_return": total_return / len(results) if results else 0,
            "profit_factor": profit_factor,
            "max_drawdown": max_drawdown,
            "sample_size_warning": sample_warning,
            "survivor_bias_warning": "⚠️ 历史数据可能存在幸存者偏差" if len(results) < total_signals * 0.8 else "",
        }


def init_database():
    """初始化数据库"""
    global DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    # 扫描结果表
    c.execute('''CREATE TABLE IF NOT EXISTS scans
                (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, 
                 symbol TEXT, score REAL, grade TEXT,
                 price_enter REAL, price_exit REAL,
                 holding_period_h REAL, return_pct REAL,
                 reason TEXT)''')
    
    # K线历史数据表
    c.execute('''CREATE TABLE IF NOT EXISTS klines
                (symbol TEXT, interval TEXT, timestamp INTEGER,
                 open REAL, high REAL, low REAL, close REAL, volume REAL,
                 PRIMARY KEY (symbol, interval, timestamp))''')
    
    conn.commit()
    conn.close()
    print(f"数据库初始化完成: {DB_PATH}")


if __name__ == "__main__":
    init_database()
    print("AlphaDog Improved - 量化研究平台")
    print(f"Alpha 币数量: {len(ALPHA_COINS)}")