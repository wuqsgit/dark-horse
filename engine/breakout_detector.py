"""V3.0 Breakout 确认 + 支撑阻力位计算
- 价格突破最近N周期高点 + 成交量放大
- 计算支撑位/阻力位供 EV+R:R 使用
"""

from shared.db import get_conn
import numpy as np

def compute_breakout_metrics(symbol: str, period: int = 20) -> dict:
    """计算突破指标
    Args:
        symbol: 币种
        period: 回溯周期
    Returns:
        {breakout: bool, volume_ratio: float, high_price: float, volume_ma: float}
    """
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT time, high, volume, close FROM futures_candles_1h
            WHERE symbol = ?
            ORDER BY time DESC LIMIT ?
        """, (symbol, period + 2)).fetchall()
        conn.close()

        if not rows or len(rows) < period + 1:
            return {"breakout": False, "volume_ratio": 0, "high_price": 0, "volume_ma": 0, "volume_source": "insufficient_data"}

        latest = rows[0]
        closed_window = rows[1:period + 1]
        volume_base = rows[2:period + 2] if len(rows) >= period + 2 else rows[1:period + 1]

        highs = [r["high"] for r in closed_window]
        volumes = [r["volume"] for r in volume_base]

        highest_high = max(highs)
        volume_ma = np.mean(volumes) if volumes else 0
        last_closed_volume = closed_window[0]["volume"] if closed_window else 0
        current_price = latest["close"]

        breakout = current_price > highest_high
        volume_ratio = last_closed_volume / volume_ma if volume_ma > 0 else 0

        return {
            "breakout": breakout,
            "volume_ratio": volume_ratio,
            "high_price": highest_high,
            "breakout_level": highest_high,
            "volume_ma": volume_ma,
            "last_closed_volume": last_closed_volume,
            "volume_source": "last_closed_1h",
            "current_price": current_price,
            "distance_to_breakout_pct": ((highest_high - current_price) / current_price) if current_price and highest_high > current_price else 0,
            "latest_time": latest["time"],
            "last_closed_time": closed_window[0]["time"] if closed_window else None,
        }
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return {"breakout": False, "volume_ratio": 0, "high_price": 0, "volume_ma": 0}


def compute_sr_levels(symbol: str, period: int = 20) -> dict:
    """计算支撑位和阻力位
    Returns:
        {resistance: float, support: float, rr_ratio: float}
    """
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT high, low, close FROM futures_candles_1h
            WHERE symbol = ?
            ORDER BY time DESC LIMIT ?
        """, (symbol, period)).fetchall()
        conn.close()
        
        if not rows or len(rows) < 5:
            return {"resistance": 0, "support": 0, "rr_ratio": 0}
        
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        
        # 阻力位：最近 period 的最高价
        resistance = max(highs)
        # 支撑位：最近 period 的最低价
        support = min(lows)
        
        current_price = rows[0]["close"]
        
        # 计算距离
        upside = resistance - current_price if resistance > current_price else 0
        downside = current_price - support if current_price > support else 0
        
        return {
            "resistance": resistance,
            "support": support,
            "upside_pct": upside / current_price * 100 if current_price > 0 else 0,
            "downside_pct": downside / current_price * 100 if current_price > 0 else 0,
            "current_price": current_price,
        }
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return {"resistance": 0, "support": 0, "rr_ratio": 0}


def check_breakout_confirmation(symbol: str, config: dict = None) -> tuple:
    """检查是否满足突破确认条件
    Args:
        symbol: 币种
        config: 配置 {breakout_period, volume_multiplier}
    Returns:
        (bool, reason)
    """
    if config is None:
        config = {
            "breakout_period": 20,
            "volume_multiplier": 1.5,
        }
    
    period = config.get("breakout_period", 20)
    vol_mult = config.get("volume_multiplier", 1.5)
    
    metrics = compute_breakout_metrics(symbol, period)
    
    if not metrics["breakout"]:
        return False, f"未突破{period}周期高点"
    
    if metrics["volume_ratio"] < vol_mult:
        return False, f"成交量未放大(vol_ratio={metrics['volume_ratio']:.2f}<{vol_mult})"
    
    return True, f"突破确认(vol_ratio={metrics['volume_ratio']:.2f})"


def compute_rr_ratio(symbol: str, entry_price: float, atr: float, 
                     use_sr: bool = True) -> float:
    """计算 Risk Reward 比率
    Args:
        symbol: 币种
        entry_price: 开仓价
        atr: ATR值
        use_sr: 是否使用SR计算（True=用SR位，False=用ATR估算）
    Returns:
        float: R:R 比率
    """
    if use_sr:
        sr = compute_sr_levels(symbol)
        if sr["resistance"] > 0:
            reward = sr["resistance"] - entry_price
            risk = atr * 2  # 2xATR 止损
            rr = reward / risk if risk > 0 else 0
            return rr
    
    # 备用：用ATR估算
    reward = atr * 4  # TP2 = 4xATR
    risk = atr * 2    # 止损 = 2xATR
    return reward / risk if risk > 0 else 2.0


def compute_rr_detail(symbol: str, entry_price: float, atr: float, use_sr: bool = True) -> dict:
    """Return both structure-based and ATR-based R:R details."""
    risk = atr * 2 if atr and atr > 0 else 0
    rr_atr = 2.0 if risk > 0 else 0
    rr_structure = 0
    reward_price = entry_price + atr * 4 if entry_price and atr else 0
    risk_price = entry_price - risk if entry_price and risk else 0
    resistance = 0
    support = 0
    if use_sr:
        sr = compute_sr_levels(symbol)
        resistance = sr.get("resistance", 0) or 0
        support = sr.get("support", 0) or 0
        if resistance > entry_price and risk > 0:
            reward_price = resistance
            rr_structure = (resistance - entry_price) / risk
    rr_used = rr_structure if rr_structure > 0 else rr_atr
    method = "structure" if rr_structure > 0 else "atr"
    return {
        "rr_atr": round(rr_atr, 2),
        "rr_structure": round(rr_structure, 2),
        "rr_used": round(rr_used, 2),
        "rr_method": method,
        "reward_price": round(reward_price, 8) if reward_price else 0,
        "risk_price": round(risk_price, 8) if risk_price else 0,
        "resistance": round(resistance, 8) if resistance else 0,
        "support": round(support, 8) if support else 0,
    }
