import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from trader.exchange import BinanceFutures


class BinanceLiveSnapshotTest(unittest.TestCase):
    def test_snapshot_loads_balance_positions_and_open_orders(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        calls = []

        def request(method, path, signed=False, params=None):
            calls.append((method, path, signed))
            if path == "/fapi/v2/account":
                return {
                    "totalWalletBalance": "1000",
                    "totalMarginBalance": "1012",
                    "totalMaintMargin": "3",
                    "totalInitialMargin": "200",
                    "totalUnrealizedProfit": "12",
                    "availableBalance": "800",
                    "assets": [{"asset": "USDT", "walletBalance": "1000"}],
                }
            if path == "/fapi/v2/positionRisk":
                return [{
                    "symbol": "BTCUSDT", "positionSide": "BOTH",
                    "positionAmt": "0.1", "entryPrice": "100000",
                    "markPrice": "101000", "unRealizedProfit": "100",
                    "leverage": "2", "notional": "10100",
                    "marginType": "cross",
                }]
            if path == "/fapi/v3/positionRisk":
                raise RuntimeError("v3 unavailable")
            if path == "/fapi/v1/openOrders":
                return [{
                    "orderId": 987, "clientOrderId": "DH-stop",
                    "symbol": "BTCUSDT", "side": "SELL",
                    "positionSide": "BOTH", "type": "STOP_MARKET",
                    "status": "NEW", "origQty": "0.1",
                    "executedQty": "0", "price": "0", "stopPrice": "95000",
                    "reduceOnly": True, "closePosition": False,
                    "timeInForce": "GTC",
                }]
            raise AssertionError(path)

        exchange._request = request

        snapshot = exchange.get_live_account_snapshot()

        self.assertEqual(snapshot["balance"]["wallet_balance"], 1000)
        self.assertEqual(snapshot["balance"]["equity"], 1012)
        self.assertEqual(snapshot["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(snapshot["orders"][0]["exchange_order_id"], "987")
        self.assertEqual(calls.count(("GET", "/fapi/v2/account", True)), 1)


class FakeExchange:
    def __init__(self):
        self.calls = 0
        self.closed = False

    def get_live_account_snapshot(self):
        self.calls += 1
        return {
            "balance": {
                "asset": "USDT", "wallet_balance": 1000,
                "equity": 1000, "available_balance": 900,
                "unrealized_pnl": 0, "total_maint_margin": 0,
                "total_initial_margin": 100,
            },
            "positions": [],
            "orders": [],
        }

    def close(self):
        self.closed = True


class QuietStream:
    async def events(self, stop):
        await stop.wait()
        if False:
            yield {}

    async def close(self):
        return None


class OneEventStream:
    async def events(self, stop):
        yield {"e": "ACCOUNT_UPDATE", "E": 1787990400000}
        await stop.wait()

    async def close(self):
        return None


class AccountSynchronizerTest(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_http_reconcile_refreshes_without_user_events(self):
        from account_stream.service import AccountSynchronizer

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        exchange = FakeExchange()
        snapshots = []

        def persist(account_id, balance, positions, orders, **metadata):
            snapshots.append((account_id, metadata["source"]))
            if len(snapshots) == 2:
                loop.call_soon_threadsafe(stop.set)

        synchronizer = AccountSynchronizer(
            {"id": 7, "name": "test", "environment": "testnet"},
            exchange=exchange,
            stream=QuietStream(),
            persist=persist,
            update_state=lambda *args, **kwargs: None,
            reconcile_interval=0.01,
        )

        await asyncio.wait_for(synchronizer.run(stop), timeout=1)

        self.assertEqual(snapshots, [(7, "startup_http"), (7, "periodic_http")])
        self.assertTrue(exchange.closed)

    async def test_account_event_triggers_reconcile_before_periodic_interval(self):
        from account_stream.service import AccountSynchronizer

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        exchange = FakeExchange()
        snapshots = []

        def persist(account_id, balance, positions, orders, **metadata):
            snapshots.append((metadata["source"], metadata["exchange_event_time"]))
            if len(snapshots) == 2:
                loop.call_soon_threadsafe(stop.set)

        synchronizer = AccountSynchronizer(
            {"id": 8, "name": "test", "environment": "testnet"},
            exchange=exchange,
            stream=OneEventStream(),
            persist=persist,
            update_state=lambda *args, **kwargs: None,
            reconcile_interval=60,
        )

        await asyncio.wait_for(synchronizer.run(stop), timeout=1)

        self.assertEqual(snapshots[0][0], "startup_http")
        self.assertEqual(snapshots[1][0], "ws_event")
        self.assertEqual(snapshots[1][1], "2026-08-29T08:00:00Z")

    async def test_listen_key_client_uses_account_environment_and_keepalive(self):
        from account_stream.binance import BinanceUserStreamClient

        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"listenKey": "listen-123"}
        http = AsyncMock()
        http.post.return_value = response
        http.put.return_value = response
        client = BinanceUserStreamClient(
            api_key="key",
            testnet=True,
            http_client=http,
        )

        listen_key = await client.create_listen_key()
        await client.keepalive(listen_key)

        self.assertEqual(listen_key, "listen-123")
        self.assertEqual(client.websocket_url(listen_key), "wss://stream.binancefuture.com/ws/listen-123")
        http.post.assert_awaited_once_with(
            "https://testnet.binancefuture.com/fapi/v1/listenKey"
        )
        http.put.assert_awaited_once_with(
            "https://testnet.binancefuture.com/fapi/v1/listenKey"
        )


if __name__ == "__main__":
    unittest.main()
