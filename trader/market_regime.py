"""Market Regime Engine (alpha-prd.md §4.6)
市场状态识别系统 - 根据市场状态调整开仓策略
"""
import logging
from typing import Dict, Tuple
from shared.db import fetch_latest_scan

logger = logging.getLogger("market_regime")


def detect_current_regime() -> str:
    """检测当前市场状态
    
    Returns:
        "Bull Market" | "Bear Market" | "Sideways Market" | "Panic Market"
    """
    try:
        scan_id, symbols = fetch_latest_scan()
        if not symbols:
            return "Sideways Market"
        
        # 统计全局趋势
        trends = {"向上": 0, "向下": 0, "横盘": 0}
        volatilities = {"正常": 0, "偏高": 0, "极高": 0}
        
        for s in symbols[:50]:  # 采样前50
            d = dict(s)
            td = d.get("trend_direction", "横盘")
            vl = d.get("volatility_level", "正常")
            if td in trends:
                trends[td] += 1
            if vl in volatilities:
                volatilities[vl] += 1
        
        total = sum(trends.values())
        if total == 0:
            return "Sideways Market"
        
        # 判定逻辑
        down_pct = trends.get("向下", 0) / total
        up_pct = trends.get("向上", 0) / total
        high_vol_pct = (volatilities.get("偏高", 0) + volatilities.get("极高", 0)) / total
        
        if down_pct > 0.4 and high_vol_pct > 0.3:
            return "Panic Market"
        if down_pct > 0.5:
            return "Bear Market"
        if up_pct > 0.4 and high_vol_pct < 0.2:
            return "Bull Market"
        return "Sideways Market"
        
    except Exception as e:
        logger.warning(f"检测市场状态失败: {e}")
        return "Sideways Market"


def adjust_strategy_for_regime(
    regime: str,
    min_score: int = 60,
    max_positions: int = 3,
) -> Tuple[int, int, str]:
    """根据市场状态调整策略参数
    
    Returns:
        (调整后的min_score, 调整后的max_positions, 调整原因)
    """
    if regime == "Panic Market":
        # 恐慌市场：提高门槛，减少仓位
        return min_score + 15, max_positions - 1, f"{regime} +15分门槛, 减仓"
    elif regime == "Bear Market":
        # 熊市：提高门槛
        return min_score + 10, max_positions, f"{regime} +10分门槛"
    elif regime == "Bull Market":
        # 牛市：降低门槛，放开仓位
        return max(50, min_score - 5), min(max_positions + 1, 5), f"{regime} -5分门槛, 放开仓位"
    else:
        # 震荡市场：保持默认
        return min_score, max_positions, "默认"


def get_regime_adjustment_message(regime: str) -> str:
    """获取市场状态提示信息"""
    regime_emojis = {
        "Bull Market": "🐂",
        "Bear Market": "🐻",
        "Sideways Market": "➡️",
        "Panic Market": "😱",
    }
    return f"{regime_emojis.get(regime, '❓')} {regime}"