"""
AlphaDog Crypto Bot - 交易所接口
支持 Binance 实盘 & 测试网
"""
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from typing import Dict, Optional, List
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    TESTNET_API_KEY, TESTNET_API_SECRET,
    TEST_MODE, SYMBOL
)


class BinanceTrader:
    def __init__(self):
        if TEST_MODE:
            self.base_url = "https://testnet.binance.vision/api"
            self.api_key = TESTNET_API_KEY
            self.secret = TESTNET_API_SECRET
        else:
            self.base_url = "https://api.binance.com/api"
            self.api_key = BINANCE_API_KEY
            self.secret = BINANCE_API_SECRET
        
        self.symbol = SYMBOL
        self.session = requests.Session()
        self.session.headers["X-MBX-APIKEY"] = self.api_key
    
    def _sign(self, params: str) -> str:
        """生成签名"""
        return hmac.new(
            self.secret.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
    
    def _request(self, method: str, endpoint: str, **params) -> Dict:
        """发送请求"""
        # 签名参数
        if self.secret:
            params["timestamp"] = int(time.time() * 1000)
            query = urlencode(sorted(params.items()))
            params["signature"] = self._sign(query)
        
        url = f"{self.base_url}{endpoint}"
        
        if method == "GET":
            resp = self.session.get(url, params=params)
        else:
            resp = self.session.request(method, url, params=params)
        
        if resp.status_code != 200:
            raise Exception(f"API Error: {resp.text}")
        
        return resp.json()
    
    # === 市场数据 ===
    def get_ticker(self, symbol: str = None) -> Dict:
        """获取24h价格统计"""
        symbol = symbol or self.symbol
        return self._request("GET", "/v3/ticker/24hr", symbol=symbol)
    
    def get_klines(self, symbol: str = None, interval: str = "1m", limit: int = 100) -> List:
        """获取K线数据"""
        symbol = symbol or self.symbol
        return self._request(
            "GET", "/v3/klines",
            symbol=symbol, interval=interval, limit=limit
        )
    
    def get_depth(self, symbol: str = None, limit: int = 20) -> Dict:
        """获取订单簿"""
        symbol = symbol or self.symbol
        return self._request("GET", "/v3/depth", symbol=symbol, limit=limit)
    
    def get_balance(self) -> Dict:
        """获取账户余额"""
        return self._request("GET", "/v3/account")
    
    # === 交易操作 ===
    def buy_limit(self, quantity: float, price: float) -> Dict:
        """限价买单"""
        return self._request(
            "POST", "/v3/order",
            symbol=self.symbol,
            side="BUY",
            type="LIMIT",
            timeInForce="GTC",
            quantity=quantity,
            price=price
        )
    
    def sell_limit(self, quantity: float, price: float) -> Dict:
        """限价卖单"""
        return self._request(
            "POST", "/v3/order",
            symbol=self.symbol,
            side="SELL",
            type="LIMIT",
            timeInForce="GTC",
            quantity=quantity,
            price=price
        )
    
    def buy_market(self, quantity: float) -> Dict:
        """市价买单"""
        return self._request(
            "POST", "/v3/order",
            symbol=self.symbol,
            side="BUY",
            type="MARKET",
            quantity=quantity
        )
    
    def sell_market(self, quantity: float) -> Dict:
        """市价卖单"""
        return self._request(
            "POST", "/v3/order",
            symbol=self.symbol,
            side="SELL",
            type="MARKET",
            quantity=quantity
        )
    
    def cancel_order(self, order_id: int) -> Dict:
        """取消订单"""
        return self._request(
            "DELETE", "/v3/order",
            symbol=self.symbol,
            orderId=order_id
        )
    
    def get_open_orders(self) -> List:
        """获取未完成订单"""
        return self._request("GET", "/v3/openOrders", symbol=self.symbol)
    
    def get_position(self) -> Dict:
        """获取当前持仓"""
        balance = self.get_balance()
        usdt_balance = 0.0
        
        for asset in balance.get("balances", []):
            if asset["asset"] == "USDT":
                usdt_balance = float(asset["free"])
                break
        
        return {"usdt": usdt_balance}


class MarketData:
    """市场数据分析"""
    
    def __init__(self, trader: BinanceTrader):
        self.trader = trader
    
    def fetch_current(self, symbol: str = None) -> dict:
        """获取当前市场数据"""
        ticker = self.trader.get_ticker(symbol)
        
        return {
            "symbol": symbol or self.trader.symbol,
            "price": float(ticker["lastPrice"]),
            "price_change": float(ticker["priceChangePercent"]),
            "high_24h": float(ticker["highPrice"]),
            "low_24h": float(ticker["lowPrice"]),
            "volume_24h": float(ticker["volume"]),
            "quote_volume_24h": float(ticker["quoteVolume"]),
        }
    
    def fetch_historical(self, symbol: str = None, interval: str = "1h", limit: int = 100) -> List[dict]:
        """获取历史K线"""
        klines = self.trader.get_klines(symbol, interval, limit)
        
        result = []
        for k in klines:
            result.append({
                "time": k[0] / 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        
        return result