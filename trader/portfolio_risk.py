"""Portfolio Risk Engine (alpha-prd.md §5.9)
组合风控引擎 - 开仓前检查组合层面风险
"""
import logging
from typing import Dict, Tuple
from trader.config import PORTFOLIO_RISK, TRADING_CONFIG
from trader.symbol_risk import get_symbol_risk

logger = logging.getLogger("portfolio_risk")


def symbol_risk_category(symbol: str) -> str:
    return str((get_symbol_risk(symbol) or {}).get("class") or "narrative")


def check_category_position_limit(
    positions: list,
    new_symbol: str,
    planned_actions: list | None = None,
    allow_explosive_override: bool = False,
) -> Tuple[bool, str]:
    """Allow at most one live/planned position in each symbol risk class."""
    limit = max(1, int(PORTFOLIO_RISK.get("max_positions_per_category", 1)))
    new_symbol_u = str(new_symbol or "").upper()
    new_category = symbol_risk_category(new_symbol_u)
    actions = planned_actions or []
    closing_symbols = {
        str(action.get("symbol") or "").upper()
        for action in actions
        if action.get("action") == "close"
    }
    occupied = []

    for position in positions or []:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol or symbol in closing_symbols:
            continue
        if symbol_risk_category(symbol) == new_category:
            occupied.append(symbol)

    for action in actions:
        if action.get("action") != "open":
            continue
        symbol = str(action.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol_risk_category(symbol) == new_category:
            occupied.append(symbol)

    if len(occupied) >= limit:
        blocker = ",".join(dict.fromkeys(occupied))
        if allow_explosive_override:
            return (
                True,
                f"category_position_soft_override class={new_category} "
                f"occupied={blocker} limit={limit}",
            )
        return (
            False,
            f"category_position_limit class={new_category} occupied={blocker} limit={limit}",
        )
    return True, "OK"


def check_portfolio_risk(
    positions: list,
    balance: float,
    new_symbol: str,
    new_category: str,
    new_invested: float,
) -> Tuple[bool, str]:
    """检查开仓是否超出组合风控限制
    
    Returns:
        (是否允许开仓, 原因)
    """
    if not positions:
        return True, "OK"
    
    total_invested = sum(p.get("invested", 0) for p in positions)
    total_exposure_pct = (total_invested + new_invested) / balance
    
    # 1. 总仓位限制
    max_total = PORTFOLIO_RISK.get("max_total_exposure_pct", 0.80)
    if total_exposure_pct > max_total:
        logger.info(f"  {new_symbol}: 总仓位{total_exposure_pct:.0%}>{max_total:.0%}, 拒绝")
        return False, f"总仓位{total_exposure_pct:.0%}>{max_total:.0%}"
    
    # 2. 单币仓位限制
    max_single = PORTFOLIO_RISK.get("max_single_exposure_pct", 0.30)
    if new_invested / balance > max_single:
        logger.info(f"  {new_symbol}: 单币{new_invested/balance:.0%}>{max_single:.0%}, 拒绝")
        return False, f"单币{new_invested/balance:.0%}>{max_single:.0%}"
    
    # 3. 同类仓位限制
    cat_invested = sum(
        p.get("invested", 0) 
        for p in positions 
        if p.get("category") == new_category
    )
    max_cat = PORTFOLIO_RISK.get("max_category_exposure_pct", 0.50)
    if (cat_invested + new_invested) / balance > max_cat:
        logger.info(f"  {new_symbol}: 同类{new_category}超限, 拒绝")
        return False, f"同类{new_category}超限"
    
    return True, "OK"


def check_daily_loss_limit(
    daily_pnl: float,
    balance: float,
) -> Tuple[bool, str]:
    """检查日亏损限制
    
    Returns:
        (是否允许开仓, 原因)
    """
    daily_loss_pct = daily_pnl / balance
    max_daily_loss = PORTFOLIO_RISK.get("max_daily_loss_pct", 0.15)
    
    if daily_loss_pct < -max_daily_loss:
        logger.warning(f"  日亏损{daily_loss_pct:.1%}<{-max_daily_loss:.1%}, 停止开仓")
        return False, f"日亏损超限{daily_loss_pct:.1%}"
    
    return True, "OK"


def check_consecutive_losses(
    consecutive_losses: int,
) -> Tuple[bool, str]:
    """检查连续亏损限制
    
    Returns:
        (是否允许开仓, 原因)
    """
    max_losses = PORTFOLIO_RISK.get("max_consecutive_losses", 3)
    
    if consecutive_losses >= max_losses:
        logger.warning(f"  连续{consecutive_losses}笔亏损, 停止开仓")
        return False, f"连续{consecutive_losses}笔亏损"
    
    return True, "OK"
