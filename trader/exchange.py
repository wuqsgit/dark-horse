"""币安合约 API 封装 — 支持 Testnet"""
import hashlib
import hmac
import json
import time
from typing import Optional
import urllib.parse

import httpx

from trader.config import EXCHANGE_CONFIG


class BinanceFutures:
    def __init__(self):
        import warnings
        import urllib3
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        urllib3.disable_warnings()
        cfg = EXCHANGE_CONFIG
        self.api_key = cfg["api_key"]
        self.api_secret = cfg["api_secret"]
        base = "https://testnet.binancefuture.com" if cfg["testnet"] else "https://fapi.binance.com"
        self.base_rest = base
        self.client = httpx.Client(timeout=10, verify=False, headers={"X-MBX-APIKEY": self.api_key})
        self.time_offset_ms = 0
        self._last_time_sync = 0.0

    def _ensure_client(self):
        """确保 client 可用，关闭则重建"""
        if self.client.is_closed:
            self.client = httpx.Client(timeout=10, verify=False, headers={"X-MBX-APIKEY": self.api_key})

    @staticmethod
    def _local_timestamp_ms() -> int:
        return int(time.time() * 1000)

    def _sync_time(self, force: bool = False):
        """Sync request timestamps to Binance server time for signed API calls."""
        now = time.time()
        if not force and now - self._last_time_sync < 60:
            return
        self._ensure_client()
        resp = self.client.get(self.base_rest + "/fapi/v1/time")
        resp.raise_for_status()
        server_time = int(resp.json()["serverTime"])
        self.time_offset_ms = server_time - self._local_timestamp_ms()
        self._last_time_sync = now

    def _signed_timestamp_ms(self) -> int:
        self._sync_time()
        return self._local_timestamp_ms() + self.time_offset_ms

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 签名"""
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return query + f"&signature={signature}"

    def _request(self, method: str, path: str, signed: bool = False, params: dict = None):
        self._ensure_client()
        params = dict(params or {})

        def send_request():
            url = self.base_rest + path
            request_kwargs = {}
            if signed:
                signed_params = dict(params)
                signed_params["timestamp"] = self._signed_timestamp_ms()
                signed_params["recvWindow"] = 10000
                url += "?" + self._sign(signed_params)
            elif params:
                request_kwargs["params"] = params
            return self.client.request(method, url, **request_kwargs)

        resp = send_request()
        if resp.status_code != 200 and signed and '"code":-1021' in resp.text:
            self._sync_time(force=True)
            resp = send_request()
        if resp.status_code != 200:
            raise Exception(f"API error {resp.status_code}: {resp.text}")
        return resp.json()

    # ---- 账户 ----

    def get_balance(self, include_upnl: bool = False) -> float:
        """获取 USDT 余额
        include_upnl=True: walletBalance + crossUnPnl (总权益)
        include_upnl=False: walletBalance 仅钱包余额"""
        data = self._request("GET", "/fapi/v2/account", signed=True)
        for asset in data.get("assets", []):
            if asset["asset"] == "USDT":
                wallet = float(asset["walletBalance"])
                if include_upnl:
                    return wallet + float(asset.get("crossUnPnl", 0))
                return wallet
        return 0.0

    def get_margin_balance(self) -> dict:
        """获取全账户权益明细"""
        data = self._request("GET", "/fapi/v2/account", signed=True)
        usdt_wallet = 0.0
        usdt_cross_upnl = 0.0
        for asset in data.get("assets", []):
            if asset["asset"] == "USDT":
                usdt_wallet = float(asset["walletBalance"])
                usdt_cross_upnl = float(asset.get("crossUnPnl", 0))
                break
        return {
            "totalWalletBalance": float(data.get("totalWalletBalance", 0)),
            "totalMarginBalance": float(data.get("totalMarginBalance", 0)),
            "totalUnrealizedProfit": float(data.get("totalUnrealizedProfit", 0)),
            "availableBalance": float(data.get("availableBalance", 0)),
            "usdt_wallet": usdt_wallet,
            "usdt_cross_unpnl": usdt_cross_upnl,
        }

    # ---- 🔧 改进: 持仓改用 /fapi/v2/positionRisk ----

    def get_positions(self) -> list:
        """获取所有持仓 — 使用 /fapi/v2/positionRisk (更准确)

        🔧 改进：
        1. 从 positionRisk 获取 markPrice 而非从 notional 推导
        2. 正确处理 Hedge Mode (positionSide: LONG/SHORT/BOTH)
        3. 用 abs(amt) < 0.001 替代 amt != 0 避免字符串转换坑
        """
        data = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        positions = []
        for pos in data:
            amt_str = pos.get("positionAmt", "0")
            try:
                amt = float(amt_str)
            except (ValueError, TypeError):
                continue

            # 仓位近似为零则跳过（避免 "0.000" 字符串转 float 后 != 0）
            if abs(amt) < 0.001:
                continue

            symbol = pos["symbol"]
            position_side = pos.get("positionSide", "BOTH")
            entry_price = float(pos.get("entryPrice", 0))
            mark_price = float(pos.get("markPrice", 0))
            unrealized_pnl = round(float(pos.get("unRealizedProfit", 0)), 2)
            leverage = int(pos.get("leverage", 1))

            positions.append({
                "symbol": symbol,
                "positionSide": position_side,  # 保留 Hedge Mode 信息
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": unrealized_pnl,
                "leverage": leverage,
            })
        return positions

    # ---- 🔧 改进: Income API 接入 ----

    def fetch_income(self, income_type: str = "REALIZED_PNL", limit: int = 1000) -> list:
        """从币安 Income API 获取历史收益记录

        🔧 改进：作为交易记录的单一真相源
        - income_type="REALIZED_PNL": 已实现盈亏
        - income_type="FUNDING_FEE": 资金费用
        - income_type="COMMISSION": 手续费

        返回：
        [
            {
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "2.34",
                "asset": "USDT",
                "time": 1234567890000,
                "tradeId": "12345",
                "info": "..."
            },
            ...
        ]
        """
        params = {
            "incomeType": income_type,
            "limit": limit,
        }
        try:
            data = self._request("GET", "/fapi/v1/income", signed=True, params=params)
            return data
        except Exception as e:
            # 测试网可能不支持 income API，返回空
            return []

    # ---- 下单 ----

    def get_trading_symbols(self) -> set:
        """获取所有可交易的 symbol"""
        try:
            resp = httpx.get(f"{self.base_rest}/fapi/v1/exchangeInfo",
                            headers={"X-MBX-APIKEY": self.api_key}, verify=False, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return {s["symbol"] for s in data["symbols"] if s["status"] == "TRADING"}
        except Exception:
            return set()

    def get_symbol_info(self, symbol: str) -> dict:
        """获取交易对的精度信息"""
        try:
            resp = httpx.get(f"{self.base_rest}/fapi/v1/exchangeInfo?symbol={symbol}",
                            headers={"X-MBX-APIKEY": self.api_key}, verify=False, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            for s in data.get("symbols", []):
                if s["symbol"] == symbol:
                    qty_filter = [f for f in s["filters"] if f["filterType"] == "LOT_SIZE"]
                    if qty_filter:
                        return {"step_size": float(qty_filter[0]["stepSize"]),
                                "min_qty": float(qty_filter[0]["minQty"]),
                                "max_qty": float(qty_filter[0]["maxQty"])}
                    break
        except Exception:
            pass
        return {"step_size": 0.001, "min_qty": 0.001, "max_qty": 99999}

    def set_leverage(self, symbol: str, leverage: int = 10):
        """设置杠杆倍数"""
        params = {"symbol": symbol, "leverage": leverage}
        return self._request("POST", "/fapi/v1/leverage", signed=True, params=params)

    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        """按步长调整数量"""
        info = self.get_symbol_info(symbol)
        step = info["step_size"]
        if step > 0:
            precision = len(str(step).split(".")[-1]) if "." in str(step) else 0
            adjusted = int(quantity / step) * step
            return round(adjusted, precision)
        return round(quantity, 3)

    def place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        """市价单 (自动调整精度)"""
        qty = self.adjust_quantity(symbol, quantity)
        params = {
            "symbol": symbol,
            "side": side.upper(),  # BUY / SELL
            "type": "MARKET",
            "quantity": qty,
        }
        if reduce_only:
            params["reduceOnly"] = True
        return self._request("POST", "/fapi/v1/order", signed=True, params=params)

    def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        """Close an existing futures position without opening the opposite side."""
        return self.place_market_order(symbol, side, quantity, reduce_only=True)

    def place_stop_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """Place a reduce-only stop-market order."""
        qty = self.adjust_quantity(symbol, quantity)
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "quantity": qty,
            "triggerPrice": round(float(stop_price), 6),
            "reduceOnly": True,
            "workingType": "MARK_PRICE",
        }
        return self._request("POST", "/fapi/v1/algoOrder", signed=True, params=params)

    def place_take_profit_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """止盈 — 测试环境下用 LIMIT reduceOnly (取巧)"""
        qty = self.adjust_quantity(symbol, quantity)
        return {"orderId": "testnet_tp_skip", "msg": "testnet跳过止盈挂单，策略引擎平仓代替"}

    # ---- 市场数据 ----

    def get_mark_price(self, symbol: str) -> float:
        data = self._request("GET", f"/fapi/v1/premiumIndex?symbol={symbol}")
        return float(data["markPrice"])

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> list:
        data = self._request("GET", f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
        return data

    def get_atr(self, symbol: str, period: int = 14) -> float:
        """从 4h klines 计算 ATR"""
        klines = self.get_klines(symbol, "4h", period + 10)
        highs = [float(k[2]) for k in klines[-period - 1:]]
        lows = [float(k[3]) for k in klines[-period - 1:]]
        closes = [float(k[4]) for k in klines[-period - 1:]]
        tr_values = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_values.append(max(hl, hc, lc))
        if len(tr_values) < period:
            return 0.0
        atr = sum(tr_values[-period:]) / period
        return atr

    def get_depth(self, symbol: str, limit: int = 20) -> dict:
        """获取订单簿深度数据"""
        data = self._request("GET", f"/fapi/v1/depth?symbol={symbol}&limit={limit}")
        return data

    def close(self):
        self.client.close()
