"""V3.0 EV + R:R 双重开仓过滤器
- R:R >= 2 才允许开仓
- 未来: EV = P(win) * AvgWin - P(loss) * AvgLoss > 0
"""

from engine.breakout_detector import compute_sr_levels, compute_breakout_metrics
from shared.db import get_conn

def check_ev_rr(symbol: str, entry_price: float, atr: float, 
                score: float, config: dict = None) -> tuple:
    """检查 EV+R:R 是否满足条件
    Args:
        symbol: 币种
        entry_price: 开仓价
        atr: ATR值
        score: Alpha Score
        config: 配置
    Returns:
        (bool, reason)
    """
    if config is None:
        config = {
            "min_rr_ratio": 2.0,  # 最小 R:R
            "min_ev_threshold": 0,  # 最小 EV（未来ML模型）
        }
    
    min_rr = config.get("min_rr_ratio", 2.0)
    
    # 计算 R:R
    sr = compute_sr_levels(symbol)
    if sr["resistance"] > 0 and entry_price > 0:
        reward = sr["resistance"] - entry_price  # 潜在上涨空间
        risk = atr * 2  # 止损距离 2xATR
        rr_ratio = reward / risk if risk > 0 else 0
    else:
        # 备用：用 ATR 估算
        reward = atr * 4  # TP2
        risk = atr * 2    # 止损
        rr_ratio = reward / risk if risk > 0 else 0
    
    if rr_ratio < min_rr:
        return False, f"R:R不足({rr_ratio:.1f}<{min_rr})"
    
    # 未来: EV 计算（基于历史统计）
    # ev = compute_ev(symbol, score)
    # if ev < min_ev_threshold:
    #     return False, f"EV为负({ev:.2f}<{min_ev_threshold})"
    
    return True, f"EV+R:R通过(R:R={rr_ratio:.1f})"


def get_trade_space(symbol: str, entry_price: float, atr: float) -> dict:
    """获取交易空间信息
    Returns:
        {upside_pct, downside_pct, rr_ratio, quality}
    """
    sr = compute_sr_levels(symbol)
    metrics = compute_breakout_metrics(symbol)
    
    if sr["resistance"] > 0 and entry_price > 0:
        upside = (sr["resistance"] - entry_price) / entry_price * 100
        downside = (entry_price - sr["support"]) / entry_price * 100 if sr["support"] > 0 else atr * 2 / entry_price * 100
    else:
        upside = atr * 4 / entry_price * 100
        downside = atr * 2 / entry_price * 100
    
    risk = atr * 2
    reward = atr * 4
    rr = reward / risk if risk > 0 else 0
    
    return {
        "upside_pct": upside,
        "downside_pct": downside,
        "rr_ratio": rr,
        "breakout": metrics.get("breakout", False),
        "volume_ratio": metrics.get("volume_ratio", 0),
    }