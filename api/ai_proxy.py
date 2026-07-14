import httpx


class AIServiceProxy:
    def __init__(self, base_url="http://127.0.0.1:8010", *, client=None, timeout_seconds=0.8):
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=float(timeout_seconds),
        )

    async def _get(self, path, *, fallback):
        try:
            response = await self.client.get(path)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            return {**fallback, "status": "error", "error": str(exc)}

    async def status(self):
        return await self._get("/v1/status", fallback={"models": {}})

    async def decisions(self, limit=100):
        return await self._get(
            "/v1/decisions", fallback={"decisions": []},
        ) if int(limit) == 100 else await self._get(
            f"/v1/decisions?limit={max(1, min(1000, int(limit)))}", fallback={"decisions": []},
        )
