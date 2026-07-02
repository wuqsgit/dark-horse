"""Dune Analytics collector — SQLite backend"""
import os
import asyncio
import logging
from datetime import datetime, timezone

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import insert_onchain

logger = logging.getLogger("dune")

DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
DUNE_QUERY_ID = os.getenv("DUNE_QUERY_ID", "7450094")


class DuneCollector:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=30)
        self.base = "https://api.dune.com/api/v1"

    async def collect_flows(self):
        if not DUNE_API_KEY:
            logger.warning("No DUNE_API_KEY")
            return []

        rows = []
        try:
            # Execute
            exec_resp = await self.http.post(
                f"{self.base}/query/{DUNE_QUERY_ID}/execute",
                headers={"x-dune-api-key": DUNE_API_KEY},
            )
            if exec_resp.status_code != 200:
                logger.error(f"Dune exec error: {exec_resp.status_code}")
                return []

            exec_data = exec_resp.json()
            execution_id = exec_data.get("execution_id")
            if not execution_id:
                return []

            # Wait for completion
            for i in range(15):
                await asyncio.sleep(2)
                s = await self.http.get(
                    f"{self.base}/execution/{execution_id}/status",
                    headers={"x-dune-api-key": DUNE_API_KEY},
                )
                if s.status_code != 200:
                    continue
                state = s.json().get("state", "")
                if state == "QUERY_STATE_COMPLETED":
                    break
            else:
                logger.error("Dune query timeout")
                return []

            # Fetch results
            r = await self.http.get(
                f"{self.base}/execution/{execution_id}/results",
                headers={"x-dune-api-key": DUNE_API_KEY},
                params={"limit": 300},
            )
            if r.status_code != 200:
                return []

            results = r.json()
            data_rows = results.get("result", {}).get("rows", [])
            now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for dr in data_rows:
                # AlphaDog uses ALPHA_NNNUSDT as token mapping
                # We store the raw symbol, the engine will match by name
                rows.append((
                    now,
                    dr.get("alpha_symbol", ""),
                    dr.get("chain", "bnb"),
                    float(dr.get("cex_inflow_usd", 0) or 0),
                    float(dr.get("cex_outflow_usd", 0) or 0),
                    float(dr.get("cex_net_flow_usd", 0) or 0),
                    float(dr.get("cex_net_flow_14d_usd", 0) or 0),
                    float(dr.get("cex_net_outflow_ratio", 0) or 0),
                    int(dr.get("window_hours", 24) or 24),
                ))

            if rows:
                insert_onchain(rows)
            logger.info(f"Dune: {len(rows)} records")
        except Exception as e:
            logger.error(f"Dune error: {e}")

        return rows

    async def close(self):
        await self.http.aclose()
