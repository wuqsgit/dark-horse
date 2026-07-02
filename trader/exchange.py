# Binance futures API wrapper with testnet support.
import hashlib
import hmac
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
        if self.client.is_closed:
            self.client = httpx.Client(timeout=10, verify=False, headers={"X-MBX-APIKEY": self.api_key})

    @staticmethod
    def _local_timestamp_ms() -> int:
        return int(time.time() * 1000)

    def _sync_time(self, force: bool = False):
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

    def get_balance(self, include_upnl: bool = False) -> float:
        data = self._request("GET", "/fapi/v2/account", signed=True)
        for asset in data.get("assets", []):
            if asset["asset"] == "USDT":
                wallet = float(asset["walletBalance"])
                if include_upnl:
                    return wallet + float(asset.get("crossUnPnl", 0))
                return wallet
        return 0.0

    def get_margin_balance(self) -> dict:
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

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def get_positions(self) -> list:
        v2_by_key = {}
        try:
            v2_rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
            for row in v2_rows:
                key = (row.get("symbol"), row.get("positionSide", "BOTH"))
                v2_by_key[key] = row
        except Exception:
            v2_rows = []

        try:
            data = self._request("GET", "/fapi/v3/positionRisk", signed=True)
            risk_version = "v3"
        except Exception:
            data = v2_rows or self._request("GET", "/fapi/v2/positionRisk", signed=True)
            risk_version = "v2"

        positions = []
        for pos in data:
            amt = self._safe_float(pos.get("positionAmt", "0"))
            if abs(amt) < 0.001:
                continue

            symbol = pos["symbol"]
            position_side = pos.get("positionSide", "BOTH")
            v2_pos = v2_by_key.get((symbol, position_side), {})
            entry_price = self._safe_float(pos.get("entryPrice"))
            mark_price = self._safe_float(pos.get("markPrice"))
            unrealized_pnl = round(self._safe_float(pos.get("unRealizedProfit")), 2)
            leverage = self._safe_int(pos.get("leverage") or v2_pos.get("leverage"), 0)
            position_initial_margin = self._safe_float(pos.get("positionInitialMargin"))
            initial_margin = self._safe_float(pos.get("initialMargin"))
            margin = position_initial_margin or initial_margin
            notional = abs(self._safe_float(pos.get("notional") or v2_pos.get("notional")))

            positions.append({
                "symbol": symbol,
                "positionSide": position_side,
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": unrealized_pnl,
                "leverage": leverage,
                "margin": margin,
                "initial_margin": initial_margin,
                "maint_margin": self._safe_float(pos.get("maintMargin")),
                "position_initial_margin": position_initial_margin,
                "open_order_initial_margin": self._safe_float(pos.get("openOrderInitialMargin")),
                "isolated_margin": self._safe_float(pos.get("isolatedMargin")),
                "notional": notional,
                "margin_asset": pos.get("marginAsset"),
                "margin_type": pos.get("marginType") or v2_pos.get("marginType"),
                "liquidation_price": self._safe_float(pos.get("liquidationPrice")),
                "break_even_price": self._safe_float(pos.get("breakEvenPrice")),
                "risk_api_version": risk_version,
            })
        return positions

    def fetch_income(self, income_type: str = "REALIZED_PNL", limit: int = 1000) -> list:
        params = {
            "incomeType": income_type,
            "limit": limit,
        }
        try:
            return self._request("GET", "/fapi/v1/income", signed=True, params=params)
        except Exception:
            return []

    def get_trading_symbols(self) -> set:
        try:
            resp = httpx.get(
                f"{self.base_rest}/fapi/v1/exchangeInfo",
                headers={"X-MBX-APIKEY": self.api_key},
                verify=False,
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return {s["symbol"] for s in data["symbols"] if s["status"] == "TRADING"}
        except Exception:
            return set()

    def get_symbol_info(self, symbol: str) -> dict:
        try:
            resp = httpx.get(
                f"{self.base_rest}/fapi/v1/exchangeInfo?symbol={symbol}",
                headers={"X-MBX-APIKEY": self.api_key},
                verify=False,
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            for s in data.get("symbols", []):
                if s["symbol"] == symbol:
                    qty_filter = [f for f in s["filters"] if f["filterType"] == "LOT_SIZE"]
                    if qty_filter:
                        return {
                            "step_size": float(qty_filter[0]["stepSize"]),
                            "min_qty": float(qty_filter[0]["minQty"]),
                            "max_qty": float(qty_filter[0]["maxQty"]),
                        }
                    break
        except Exception:
            pass
        return {"step_size": 0.001, "min_qty": 0.001, "max_qty": 99999}

    def set_leverage(self, symbol: str, leverage: int = 10):
        params = {"symbol": symbol, "leverage": leverage}
        return self._request("POST", "/fapi/v1/leverage", signed=True, params=params)

    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        info = self.get_symbol_info(symbol)
        step = info["step_size"]
        if step > 0:
            precision = len(str(step).split(".")[-1]) if "." in str(step) else 0
            adjusted = int(quantity / step) * step
            return round(adjusted, precision)
        return round(quantity, 3)

    def place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        qty = self.adjust_quantity(symbol, quantity)
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty,
        }
        if reduce_only:
            params["reduceOnly"] = True
        return self._request("POST", "/fapi/v1/order", signed=True, params=params)

    def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        return self.place_market_order(symbol, side, quantity, reduce_only=True)

    def place_stop_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
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
        self.adjust_quantity(symbol, quantity)
        return {"orderId": "testnet_tp_skip", "msg": "testnet skip take-profit order"}

    def get_mark_price(self, symbol: str) -> float:
        data = self._request("GET", f"/fapi/v1/premiumIndex?symbol={symbol}")
        return float(data["markPrice"])

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> list:
        return self._request("GET", f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")

    def get_atr(self, symbol: str, period: int = 14) -> float:
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
        return sum(tr_values[-period:]) / period

    def get_depth(self, symbol: str, limit: int = 20) -> dict:
        return self._request("GET", f"/fapi/v1/depth?symbol={symbol}&limit={limit}")

    def close(self):
        self.client.close()
