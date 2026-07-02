#!/usr/bin/env python3
"""
AlphaDog Crypto Bot - 主程序
"""
import os
import sys
import time
import json
from datetime import datetime

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    TEST_MODE, SYMBOL, INTERVAL,
    LOG_DIR
)
from trader import BinanceTrader, MarketData
from risk_manager import RiskManager, create_risk_manager
from strategy import StrategyEngine, create_strategy


class AlphaDogBot:
    """AlphaDog 交易机器人"""
    
    def __init__(self):
        # 初始化各模块
        self.trader = BinanceTrader()
        self.market = MarketData(self.trader)
        self.risk = create_risk_manager()
        self.strategy = create_strategy()
        
        # 状态
        self.running = False
        self.check_interval = 60  # 检查间隔（秒）
        
        # 创建日志目录
        os.makedirs(LOG_DIR, exist_ok=True)
        
        print(f"=== AlphaDog Bot 初始化 ===")
        print(f"模式: {'测试网' if TEST_MODE else '实盘'}")
        print(f"交易对: {SYMBOL}")
        print(f"间隔: {self.check_interval}s")
    
    def log(self, msg: str, level: str = "INFO"):
        """日志输出"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {msg}"
        print(log_msg)
        
        # 写入文件
        log_file = f"{LOG_DIR}/bot_{datetime.now().strftime('%Y%m%d')}.log"
        with open(log_file, "a") as f:
            f.write(log_msg + "\n")
    
    def check_and_trade(self):
        """检查市场并交易"""
        try:
            # 1. 获取市场数据
            self.log("获取市场数据...")
            market_data = self.market.fetch_current(SYMBOL)
            self.log(f"当前价格: {market_data['price']}, 24h涨跌: {market_data['price_change']}%")
            
            # 2. 获取账户余额
            position = self.trader.get_position()
            self.log(f"USDT余额: {position['usdt']}")
            
            # 3. 分析信号
            signal = self.strategy.analyze(market_data)
            self.log(f"信号: {signal.grade} ({signal.side}), 分数: {signal.score}")
            self.log(f"关键因素: {signal.key_factors}")
            
            # 4. 风控检查
            can_open, reason = self.risk.can_open_position(position["usdt"])
            
            # 5. 执行交易
            if can_open:
                should_entry, entry_reason = self.strategy.should_entry(signal)
                if should_entry:
                    self.log(f"=== 入场信号: {entry_reason} ===")
                    self._execute_entry(signal, position["usdt"])
                else:
                    self.log(f"等待: {entry_reason}")
            else:
                self.log(f"禁止入场: {reason}")
            
            # 6. 检查现有持仓
            self._check_positions(market_data)
            
        except Exception as e:
            self.log(f"错误: {e}", "ERROR")
    
    def _execute_entry(self, signal, usdt_balance: float):
        """执行入场"""
        price = signal.entry_price
        
        # 计算仓位
        quantity = self.risk.calculate_position_size(usdt_balance, price)
        
        if quantity < 0.001:
            self.log("仓位太小，跳过")
            return
        
        self.log(f"开{'多' if signal.side == 'LONG' else '空'}: {quantity} @ {price}")
        
        if TEST_MODE:
            self.log("(测试网模式 - 未实际下单)")
        else:
            try:
                if signal.side == "LONG":
                    result = self.trader.buy_market(quantity)
                else:
                    result = self.trader.sell_market(quantity)
                
                self.log(f"下单结果: {result}")
                
                # 记录
                from risk_manager import TradeRecord
                record = TradeRecord(
                    time=datetime.now().isoformat(),
                    symbol=SYMBOL,
                    side=signal.side,
                    quantity=quantity,
                    price=price,
                    pnl_pct=0,
                    reason="entry"
                )
                self.risk.record_trade(record)
                
            except Exception as e:
                self.log(f"下单失败: {e}", "ERROR")
    
    def _check_positions(self, market_data):
        """检查持仓"""
        try:
            open_orders = self.trader.get_open_orders()
            
            # 更新持仓状态
            current_price = market_data["price"]
            
            # 这里简化处理：检查未完成订单
            # 实际应该查询持仓Position
            
        except Exception as e:
            self.log(f"检查持仓失败: {e}")
    
    def start(self):
        """启动机器人"""
        self.running = True
        self.log("=== AlphaDog Bot 启动 ===")
        
        while self.running:
            try:
                self.check_and_trade()
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                self.log("用户中断")
                self.stop()
            except Exception as e:
                self.log(f"循环错误: {e}", "ERROR")
                time.sleep(10)
    
    def stop(self):
        """停止机器人"""
        self.running = False
        self.log("=== AlphaDog Bot 停止 ===")
    
    def status(self):
        """状态"""
        return self.risk.get_status()


def main():
    """主入口"""
    bot = AlphaDogBot()
    
    # 命令行参数
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        
        if cmd == "status":
            print(json.dumps(bot.status(), indent=2, ensure_ascii=False))
        elif cmd == "once":
            bot.check_and_trade()
        elif cmd == "start":
            bot.start()
        else:
            print(f"未知命令: {cmd}")
    else:
        # 默认: 立即执行一次
        bot.check_and_trade()


if __name__ == "__main__":
    main()