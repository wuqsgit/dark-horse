import unittest
from unittest.mock import patch

from trader.exchange import BinanceFutures


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "symbols": [{
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }]
        }


class ExchangeSymbolInfoTest(unittest.TestCase):
    def test_reads_min_notional_and_tick_size(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        exchange.base_rest = "https://example.test"
        exchange.api_key = "key"
        with patch("trader.exchange.httpx.get", return_value=Response()):
            info = exchange.get_symbol_info("BTCUSDT")

        self.assertEqual(info["min_notional"], 5.0)
        self.assertEqual(info["tick_size"], 0.1)

    def test_symbol_info_failure_does_not_use_unsafe_generic_rules(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        exchange.base_rest = "https://example.test"
        exchange.api_key = "key"
        with patch(
            "trader.exchange.httpx.get",
            side_effect=OSError("exchange info unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot safely size"):
                exchange.get_symbol_info("NEWCOINUSDT")

    def test_stop_trigger_uses_tick_size_and_rounds_away_from_market(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        exchange.get_symbol_info = lambda symbol: {
            "step_size": 0.001,
            "min_qty": 0.001,
            "max_qty": 1000,
            "min_notional": 5.0,
            "tick_size": 0.1,
        }
        exchange.adjust_quantity = lambda symbol, quantity: quantity
        requests = []
        exchange._request = lambda method, path, signed, params: requests.append(params) or {"algoId": 1}

        exchange.place_stop_order("PAXGUSDT", "SELL", 0.266, 3968.09392)
        exchange.place_stop_order("PAXGUSDT", "BUY", 0.266, 3968.09392)

        self.assertEqual(requests[0]["triggerPrice"], 3968.0)
        self.assertEqual(requests[1]["triggerPrice"], 3968.1)

    def test_cancel_protective_stops_uses_algo_order_type_field(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        requests = []

        def request(method, path, signed, params):
            requests.append((method, path, params))
            if method == "GET":
                return [
                    {
                        "algoId": 101,
                        "orderType": "STOP_MARKET",
                        "symbol": "ETHUSDT",
                    },
                    {
                        "algoId": 102,
                        "orderType": "TAKE_PROFIT_MARKET",
                        "symbol": "ETHUSDT",
                    },
                ]
            return {"code": 200}

        exchange._request = request

        cancelled = exchange.cancel_other_protective_stops("ETHUSDT")

        self.assertEqual(cancelled, 1)
        deletes = [item for item in requests if item[0] == "DELETE"]
        self.assertEqual(deletes[0][2]["algoId"], 101)


if __name__ == "__main__":
    unittest.main()
