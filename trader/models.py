"""交易数据模型"""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Position:
    """当前持仓"""
    symbol: str
    side: str  # "LONG" / "SHORT"
    quantity: float
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    atr_value: float
    unrealized_pnl: float
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


@dataclass
class TradeRecord:
    """历史交易记录"""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str  # "stop_loss" | "take_profit" | "signal_reversal" | "manual"
    entry_time: str
    exit_time: str
    grade_at_entry: str
    score_at_entry: float
