import json
import unittest

import httpx

from api.ai_proxy import AIServiceProxy


class AIServiceProxyTest(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_status_and_decisions(self):
        def handler(request):
            if request.url.path == "/v1/status":
                return httpx.Response(200, json={"status": "collecting"})
            return httpx.Response(200, json={"decisions": [{"symbol": "B2USDT"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ai")
        proxy = AIServiceProxy(client=client)
        try:
            self.assertEqual((await proxy.status())["status"], "collecting")
            self.assertEqual((await proxy.decisions(5))["decisions"][0]["symbol"], "B2USDT")
        finally:
            await client.aclose()

    async def test_unavailable_service_returns_explicit_error_payload(self):
        def handler(request):
            raise httpx.ConnectError("offline", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ai")
        proxy = AIServiceProxy(client=client)
        try:
            result = await proxy.status()
        finally:
            await client.aclose()

        self.assertEqual(result["status"], "error")
        self.assertIn("offline", result["error"])


if __name__ == "__main__":
    unittest.main()
