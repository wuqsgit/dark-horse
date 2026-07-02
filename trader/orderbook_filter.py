"""V3.0 Order Book 过滤器
- 获取 Binance Depth 数据
- 分析买卖盘深度比例、盘口失衡程度
- 根据 Order Book 状态调整交易决策
"""

import time
from shared.db import get_conn

# Binance Depth API 返回的档位数
DEPTH_LIMIT = 20

def get_orderbook(symbol: str, exchange) -> dict:
    """获取订单簿数据
    Args:
        symbol: 币种
        exchange: BinanceFutures实例（必须有get_depth方法）
    Returns:
        {bid_depth, ask_depth, imbalance_ratio, top_bid_qty, top_ask_qty, spread}
    """
    try:
        data = exchange.get_depth(symbol, DEPTH_LIMIT)
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        if not bids or not asks:
            return {"bid_depth": 0, "ask_depth": 0, "imbalance_ratio": 1, "top_bid_qty": 0, "top_ask_qty": 0, "spread": 0}
        
        # 计算总深度（买入/卖出总量）
        bid_depth = sum(float(b[1]) for b in bids)
        ask_depth = sum(float(a[1]) for a in asks)
        
        # 深度比例
        imbalance_ratio = ask_depth / bid_depth if bid_depth > 0 else 999
        
        # top bid/ask quantity
        top_bid_qty = float(bids[0][1]) if bids else 0
        top_ask_qty = float(asks[0][1]) if asks else 0
        
        # 买卖价差
        spread = float(asks[0][0]) - float(bids[0][0]) if asks and bids else 0
        
        return {
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "imbalance_ratio": imbalance_ratio,
            "top_bid_qty": top_bid_qty,
            "top_ask_qty": top_ask_qty,
            "spread": spread,
            "top_bid_price": float(bids[0][0]) if bids else 0,
            "top_ask_price": float(asks[0][0]) if asks else 0,
        }
    except Exception as e:
        return {"bid_depth": 0, "ask_depth": 0, "imbalance_ratio": 1, "top_bid_qty": 0, "top_ask_qty": 0, "spread": 0}


def check_orderbook_filter(symbol: str, exchange=None, config: dict = None) -> tuple:
    """检查 Order Book 是否允许开仓
    Args:
        symbol: 币种
        exchange: BinanceFutures实例（必须有get_depth方法）
        config: 配置 {max_imbalance, min_depth_ratio}
    Returns:
        (bool, reason)
    """
    if config is None:
        config = {
            "max_imbalance": 2.0,     # 卖盘/买盘最大比例
            "min_depth_ratio": 0.5,   # 买盘/卖盘最小比例
        }
    
    if exchange is None:
        return True, "无exchange实例,跳过OB检查"
    
    ob = get_orderbook(symbol, exchange)
    
    if ob["bid_depth"] == 0 or ob["ask_depth"] == 0:
        return True, "Order Book数据缺失,跳过检查"  # 没有数据时不过滤
    
    imbalance = ob["imbalance_ratio"]
    
    # 卖压明显大于买盘（失衡严重）
    if imbalance > config["max_imbalance"]:
        return False, f"卖压过大(imbalance={imbalance:.2f}>{config['max_imbalance']})"
    
    # 买盘太小
    if imbalance < config["min_depth_ratio"]:
        return False, f"买盘不足(imbalance={imbalance:.2f}<{config['min_depth_ratio']})"
    
    return True, f"Order Book正常(imbalance={imbalance:.2f})"


def save_orderbook_snapshot(symbol: str, exchange) -> None:
    """保存订单簿快照到数据库"""
    ob = get_orderbook(symbol, exchange)
    
    from datetime import datetime, timezone
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO orderbook_snapshots
            (timestamp, symbol, bid_depth, ask_depth, imbalance_ratio, top_bid_qty, top_ask_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            symbol,
            ob["bid_depth"],
            ob["ask_depth"],
            ob["imbalance_ratio"],
            ob["top_bid_qty"],
            ob["top_ask_qty"],
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except:
            pass