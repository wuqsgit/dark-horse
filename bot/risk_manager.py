"""
AlphaDog Crypto Bot - 风控模块
"""
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import json
import os


@dataclass
class TradeRecord:
    """交易记录"""
    time: str
    symbol: str
    side: str  # BUY / SELL
    quantity: float
    price: float
    pnl_pct: float = 0.0
    reason: str = ""  # entry / take_profit / stop_loss / manual


@dataclass
class Position:
    """持仓"""
    symbol: str
    side: str  # LONG / SHORT
    quantity: float
    entry_price: float
    current_price: float = 0.0
    pnl_pct: float = 0.0
    open_time: str = ""


class RiskManager:
    """风控管理器"""
    
    def __init__(self, config: dict):
        # 风控参数
        self.max_position_pct = config.get("max_position_pct", 0.10)  # 最大10%仓位
        self.max_single_loss_pct = config.get("max_single_loss_pct", 0.02)  # 单笔最大2%
        self.max_daily_loss_pct = config.get("max_daily_loss_pct", 0.05)  # 每日最大5%
        self.stop_loss_pct = config.get("stop_loss_pct", 0.015)  # 止损1.5%
        self.take_profit_pct = config.get("take_profit_pct", 0.03)  # 止盈3%
        
        # 状态
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.last_reset_date = datetime.now().date()
        self.positions: List[Position] = []
        self.trade_history: List[TradeRecord] = []
        
        # 文件路径
        self.log_file = config.get("log_file", "./logs/trades.json")
        
        self._load_history()
    
    def _load_history(self):
        """加载历史记录"""
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r") as f:
                    data = json.load(f)
                    self.trade_history = [
                        TradeRecord(**t) for t in data.get("trades", [])
                    ]
            except:
                pass
    
    def _save_history(self):
        """保存历史记录"""
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with open(self.log_file, "w") as f:
            json.dump({
                "trades": [vars(t) for t in self.trade_history[-100:]]
            }, f, indent=2)
    
    def check_daily_reset(self):
        """检查是否需要重置每日统计"""
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset_date = today
    
    def can_open_position(self, total_balance: float) -> tuple[bool, str]:
        """检查是否可以开仓"""
        # 1. 检查每日亏损限制
        self.check_daily_reset()
        if self.daily_pnl <= -self.max_daily_loss_pct:
            return False, f"已达每日亏损上限 {self.max_daily_loss_pct*100}%"
        
        # 2. 检查持仓数量
        if len(self.positions) >= 3:
            return False, "已达最大持仓数限制"
        
        # 3. 检查持仓金额
        total_position_value = sum(p.quantity * p.entry_price for p in self.positions)
        if total_position_value > total_balance * self.max_position_pct:
            return False, "已达最大仓位限制"
        
        return True, "OK"
    
    def should_close_position(self, position: Position) -> tuple[bool, str]:
        """检查是否应该平仓"""
        if position.pnl_pct >= self.take_profit_pct * 100:
            return True, f"止盈 {position.pnl_pct:.2f}%"
        elif position.pnl_pct <= -self.stop_loss_pct * 100:
            return True, f"止损 {position.pnl_pct:.2f}%"
        
        return False, ""
    
    def calculate_position_size(self, total_balance: float, price: float) -> float:
        """计算开仓数量"""
        max_value = total_balance * self.max_position_pct
        return max_value / price
    
    def record_trade(self, trade: TradeRecord):
        """记录交易"""
        self.trade_history.append(trade)
        self.daily_trades += 1
        
        if trade.pnl_pct != 0:
            self.daily_pnl += trade.pnl_pct / 100
        
        self._save_history()
    
    def get_status(self) -> dict:
        """获取当前状态"""
        return {
            "daily_pnl": f"{self.daily_pnl*100:.2f}%",
            "daily_trades": self.daily_trades,
            "open_positions": len(self.positions),
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "pnl": f"{p.pnl_pct:.2f}%"
                }
                for p in self.positions
            ]
        }


def create_risk_manager() -> RiskManager:
    """创建风控管理器"""
    from config import (
        MAX_POSITION_PCT, MAX_SINGLE_LOSS_PCT, MAX_DAILY_LOSS_PCT,
        STOP_LOSS_PCT, TAKE_PROFIT_PCT, LOG_DIR
    )
    
    config = {
        "max_position_pct": MAX_POSITION_PCT,
        "max_single_loss_pct": MAX_SINGLE_LOSS_PCT,
        "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "log_file": f"{LOG_DIR}/trades.json"
    }
    
    return RiskManager(config)