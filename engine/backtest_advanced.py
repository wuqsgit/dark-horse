"""V3.0 高级回测系统
- Walk Forward Validation
- Monte Carlo 风险分析
- Exit Optimizer
- Event Driven Backtest（模拟真实订单执行）
"""

import numpy as np
import pandas as pd
import random
from datetime import datetime, timedelta, timezone
from shared.db import get_conn

class WalkForwardBacktest:
    """Walk Forward 回测 - 滚动窗口训练/测试"""
    
    def __init__(self, train_days=30, test_days=7, step_days=7):
        """
        Args:
            train_days: 训练窗口天数
            test_days: 测试窗口天数
            step_days: 滚动步长
        """
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
    
    def run(self, df_scores, df_prices):
        """执行 Walk Forward 回测
        Returns:
            {train_results, test_results, oos_result}
        """
        if df_scores.empty or df_prices.empty:
            return None
        
        # 按时间排序
        df_scores = df_scores.sort_values("time")
        df_prices = df_prices.sort_values("time_bucket")
        
        total_days = (df_scores["time"].max() - df_scores["time"].min()).days
        windows = []
        
        current_start = df_scores["time"].min()
        while current_start + timedelta(days=self.train_days + self.test_days) <= df_scores["time"].max():
            train_end = current_start + timedelta(days=self.train_days)
            test_end = train_end + timedelta(days=self.test_days)
            
            train_scores = df_scores[(df_scores["time"] >= current_start) & (df_scores["time"] < train_end)]
            test_scores = df_scores[(df_scores["time"] >= train_end) & (df_scores["time"] < test_end)]
            
            if len(train_scores) > 10 and len(test_scores) > 5:
                train_result = self._evaluate(train_scores, df_prices, "train")
                test_result = self._evaluate(test_scores, df_prices, "test")
                windows.append({
                    "train_start": current_start,
                    "train_end": train_end,
                    "test_start": train_end,
                    "test_end": test_end,
                    "train_metric": train_result,
                    "test_metric": test_result,
                })
            
            current_start += timedelta(days=self.step_days)
        
        # Out-of-sample 测试：最后一段
        oos_start = df_scores["time"].max() - timedelta(days=self.train_days + self.test_days)
        oos_scores = df_scores[df_scores["time"] >= oos_start]
        oos_result = self._evaluate(oos_scores, df_prices, "oos") if len(oos_scores) > 5 else None
        
        return {"windows": windows, "oos_result": oos_result}
    
    def _evaluate(self, scores, prices, label):
        """评估窗口表现"""
        if scores.empty:
            return {"label": label, "count": 0}
        
        win_12h = scores[scores.get("return_12h", pd.Series([None]*len(scores))) > 0]
        avg_ret = scores["return_12h"].mean() if "return_12h" in scores.columns else 0
        
        return {
            "label": label,
            "count": len(scores),
            "win_rate": len(win_12h) / len(scores) if len(scores) > 0 else 0,
            "avg_return": avg_ret,
        }


class MonteCarloRisk:
    """Monte Carlo 风险分析"""
    
    def __init__(self, n_simulations=1000):
        self.n_simulations = n_simulations
    
    def run(self, trades: list) -> dict:
        """
        Args:
            trades: 历史交易列表 [{pnl, pnl_pct, ...}]
        Returns:
            {metrics, percentiles, worst_cases}
        """
        if not trades or len(trades) < 10:
            return {"error": "Not enough trades"}
        
        pnls = [t.get("pnl", 0) for t in trades]
        pnl_pcts = [t.get("pnl_pct", 0) for t in trades]
        
        # 计算基准指标
        total_pnl = sum(pnls)
        win_trades = [p for p in pnls if p > 0]
        lose_trades = [p for p in pnls if p <= 0]
        
        results = {
            "total_trades": len(trades),
            "win_rate": len(win_trades) / len(trades) if trades else 0,
            "total_pnl": total_pnl,
            "avg_win": np.mean(win_trades) if win_trades else 0,
            "avg_loss": np.mean(lose_trades) if lose_trades else 0,
            "max_win": max(pnls) if pnls else 0,
            "max_loss": min(pnls) if pnls else 0,
        }
        
        # Monte Carlo 模拟
        equity_curves = []
        max_drawdowns = []
        final_pnls = []
        
        for _ in range(self.n_simulations):
            # 随机重排交易顺序
            sim_pnls = random.sample(pnls, len(pnls))
            
            # 计算资金曲线
            equity = [0]
            for pnl in sim_pnls:
                equity.append(equity[-1] + pnl)
            
            equity_curves.append(equity)
            max_drawdowns.append(min(equity) - max(equity[:equity.index(max(equity))+1]))
            final_pnls.append(equity[-1])
        
        # 计算百分位
        final_pnls_sorted = sorted(final_pnls)
        max_drawdowns_sorted = sorted(max_drawdowns)
        
        results["percentiles"] = {
            "p10": final_pnls_sorted[int(len(final_pnls_sorted) * 0.1)],
            "p25": final_pnls_sorted[int(len(final_pnls_sorted) * 0.25)],
            "p50": final_pnls_sorted[int(len(final_pnls_sorted) * 0.5)],
            "p75": final_pnls_sorted[int(len(final_pnls_sorted) * 0.75)],
            "p90": final_pnls_sorted[int(len(final_pnls_sorted) * 0.9)],
        }
        
        results["max_drawdown_percentiles"] = {
            "p10": max_drawdowns_sorted[int(len(max_drawdowns_sorted) * 0.1)],
            "p50": max_drawdowns_sorted[int(len(max_drawdowns_sorted) * 0.5)],
            "p90": max_drawdowns_sorted[int(len(max_drawdowns_sorted) * 0.9)],
        }
        
        # 最坏情况
        results["worst_cases"] = {
            "worst_10pct_avg": np.mean(final_pnls_sorted[:int(len(final_pnls_sorted) * 0.1)]),
            "worst_max_drawdown_p90": max_drawdowns_sorted[int(len(max_drawdowns_sorted) * 0.9)],
        }
        
        return results


class ExitOptimizer:
    """Exit Optimizer - 自动退出优化器"""
    
    def __init__(self):
        pass
    
    def analyze(self, trades: list) -> dict:
        """
        分析交易退出策略
        Returns:
            {avg_hold_time, tp_hits, sl_hits, trailing_hits, suggestions}
        """
        if not trades:
            return {"error": "No trades"}
        
        # 统计各退出原因
        exit_reasons = {}
        hold_times = []
        tp_hits = 0
        sl_hits = 0
        
        for t in trades:
            reason = t.get("exit_reason", "unknown")
            if "take_profit" in reason.lower() or "tp" in reason.lower():
                tp_hits += 1
            elif "stop" in reason.lower():
                sl_hits += 1
            
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
            
            # 计算持仓时间
            if t.get("entry_time") and t.get("exit_time"):
                try:
                    entry = datetime.fromisoformat(str(t["entry_time"]))
                    exit_t = datetime.fromisoformat(str(t["exit_time"]))
                    hold_times.append((exit_t - entry).total_seconds() / 3600)  # 小时
                except:
                    pass
        
        # 分析是否卖飞
        avg_hold = np.mean(hold_times) if hold_times else 0
        tp_rate = tp_hits / len(trades) if trades else 0
        sl_rate = sl_hits / len(trades) if trades else 0
        
        suggestions = []
        
        if avg_hold < 2:
            suggestions.append("持仓时间过短(<2h)，可能过度交易")
        if tp_rate > 0.7:
            suggestions.append("TP命中率过高(>70%)，可考虑让利润奔跑")
        if sl_rate > 0.4:
            suggestions.append("止损命中率过高(>40%)，需要优化止损位置")
        
        # 模拟不同退出策略
        strategy_comparison = self._simulate_strategies(trades)
        
        return {
            "total_trades": len(trades),
            "tp_hits": tp_hits,
            "sl_hits": sl_hits,
            "tp_rate": tp_rate,
            "sl_rate": sl_rate,
            "avg_hold_hours": round(avg_hold, 1),
            "exit_reasons": exit_reasons,
            "suggestions": suggestions,
            "strategy_comparison": strategy_comparison,
        }
    
    def _simulate_strategies(self, trades: list) -> dict:
        """模拟不同退出策略的表现"""
        strategies = {
            "hold_2x": [],  # 持有2倍时间
            "early_tp": [],  # 提前止盈
            "tight_sl": [],  # 紧凑止损
        }
        
        for t in trades:
            pnl = t.get("pnl", 0)
            # 模拟不同策略下的收益
            strategies["hold_2x"].append(pnl * 1.2 if pnl > 0 else pnl)
            strategies["early_tp"].append(pnl * 0.7 if pnl > 0 else pnl)
            strategies["tight_sl"].append(pnl * 0.9 if pnl > 0 else pnl * 1.2)
        
        return {
            name: sum(pnls) for name, pnls in strategies.items()
        }


class EventDrivenBacktest:
    """事件驱动回测 - 模拟真实订单执行"""
    
    def __init__(self, slippage_pct=0.0005, commission_pct=0.0004):
        """
        Args:
            slippage_pct: 滑点（0.05%）
            commission_pct: 手续费（0.04%）
        """
        self.slippage = slippage_pct
        self.commission = commission_pct
    
    def run(self, df_scores, df_prices, initial_balance=10000):
        """
        执行事件驱动回测
        模拟：下单 -> 成交 -> 止损/止盈/移动止盈 -> 平仓
        """
        balance = initial_balance
        positions = []
        trades = []
        equity_curve = [balance]
        
        for _, score_row in df_scores.iterrows():
            sym = score_row["symbol"]
            score = score_row.get("composite_score", 50)
            price = score_row.get("market_price", 0)
            time = score_row["time"]
            
            if price == 0:
                continue
            
            # 检查是否开仓信号
            if score >= 70 and not any(p["symbol"] == sym for p in positions):
                # 模拟滑点和手续费
                entry_price = price * (1 + self.slippage)
                cost = balance * 0.1  # 10%仓位
                qty = cost / entry_price
                
                # 扣除手续费
                balance -= cost + cost * self.commission
                
                positions.append({
                    "symbol": sym,
                    "entry_price": entry_price,
                    "qty": qty,
                    "entry_time": time,
                    "stop_loss": entry_price * 0.98,  # 2%止损
                    "tp1": entry_price * 1.02,  # 2xATR替代，这里用简化
                    "tp2": entry_price * 1.04,
                    "tp3": entry_price * 1.06,
                    "highest_price": entry_price,
                    "balance_at_entry": balance,
                })
            
            # 检查持仓
            for pos in positions[:]:
                current_price = price  # 简化，实际应该查价格
                
                # 更新最高价
                if pos["side"] != "SHORT":
                    pos["highest_price"] = max(pos["highest_price"], current_price)
                
                # 检查止损
                if current_price <= pos["stop_loss"]:
                    pnl = (pos["stop_loss"] - pos["entry_price"]) * pos["qty"]
                    balance += pos["qty"] * pos["stop_loss"] - pnl * self.commission
                    trades.append({**pos, "exit_price": pos["stop_loss"], "pnl": pnl, "exit_time": time})
                    positions.remove(pos)
                    continue
                
                # 检查TP3（移动止盈）
                if current_price >= pos["tp3"]:
                    trail_price = pos["highest_price"] * 0.98  # 2%回撤
                    if current_price <= trail_price:
                        pnl = (trail_price - pos["entry_price"]) * pos["qty"]
                        balance += pos["qty"] * trail_price - pnl * self.commission
                        trades.append({**pos, "exit_price": trail_price, "pnl": pnl, "exit_time": time})
                        positions.remove(pos)
                        continue
                
                # 检查TP1/TP2
                if current_price >= pos["tp2"]:
                    # 减仓50%
                    pnl = (pos["tp2"] - pos["entry_price"]) * pos["qty"] * 0.5
                    balance += pos["qty"] * pos["tp2"] * 0.5 - pnl * self.commission
                    pos["qty"] *= 0.5
                
                elif current_price >= pos["tp1"]:
                    # 减仓25%
                    pnl = (pos["tp1"] - pos["entry_price"]) * pos["qty"] * 0.25
                    balance += pos["qty"] * pos["tp1"] * 0.25 - pnl * self.commission
                    pos["qty"] *= 0.75
            
            equity_curve.append(balance)
        
        return {
            "final_balance": balance,
            "total_return": (balance - initial_balance) / initial_balance * 100,
            "num_trades": len(trades),
            "equity_curve": equity_curve,
            "trades": trades,
        }
