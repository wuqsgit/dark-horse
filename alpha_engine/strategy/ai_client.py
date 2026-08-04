"""HTTP client for the Alpha Strategy V2 AI service."""
from __future__ import annotations

import httpx


class AlphaStrategyAIClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        timeout_seconds: float = 2.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def evaluate(self, payload: dict) -> dict:
        response = httpx.post(
            f"{self.base_url}/v2/alpha-strategy/evaluate",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def observe_many(self, candidates: list[dict]) -> dict:
        response = httpx.post(
            f"{self.base_url}/v2/alpha-strategy/observe",
            json={"candidates": candidates},
            timeout=max(3.0, self.timeout_seconds),
        )
        response.raise_for_status()
        return response.json()
