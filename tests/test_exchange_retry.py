import unittest
from unittest.mock import Mock, patch

import httpx

from trader.exchange import BinanceFutures


class ExchangeRetryTest(unittest.TestCase):
    def _exchange(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        exchange.api_key = "test"
        exchange.api_secret = "secret"
        exchange.base_rest = "https://example.test"
        exchange.time_offset_ms = 0
        exchange._last_time_sync = 1e20
        exchange.client = Mock()
        exchange.client.is_closed = False
        exchange._reset_client = Mock()
        return exchange

    def test_get_retries_read_timeout(self):
        exchange = self._exchange()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        exchange.client.request.side_effect = [httpx.ReadTimeout("slow"), response]

        with patch("trader.exchange.time.sleep"):
            result = exchange._request("GET", "/fapi/v2/account")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(exchange.client.request.call_count, 2)

    def test_post_does_not_retry_ambiguous_timeout(self):
        exchange = self._exchange()
        exchange.client.request.side_effect = httpx.ReadTimeout("slow")

        with self.assertRaises(httpx.ReadTimeout):
            exchange._request("POST", "/fapi/v1/order")

        self.assertEqual(exchange.client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
