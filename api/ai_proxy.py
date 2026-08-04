import httpx


class AIServiceProxy:
    def __init__(self, base_url="http://127.0.0.1:8010", *, client=None, timeout_seconds=5.0):
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=float(timeout_seconds),
        )

    async def _get(self, path, *, fallback):
        try:
            response = await self.client.get(path)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            return {**fallback, "status": "error", "error": detail}

    async def status(self):
        return await self._get(
            "/v1/entry-quality/status", fallback={"models": {}},
        )

    async def alpha_strategy_status(self):
        return await self._get(
            "/v2/alpha-strategy/status",
            fallback={
                "execution_mode": "unknown",
                "samples": {},
                "models": [],
            },
        )

    async def decisions(self, limit=100):
        return await self._get(
            "/v1/decisions", fallback={"decisions": []},
        ) if int(limit) == 100 else await self._get(
            f"/v1/decisions?limit={max(1, min(1000, int(limit)))}", fallback={"decisions": []},
        )
