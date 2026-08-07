from __future__ import annotations

import os


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENABLED = env_bool("MINUTE_PIPELINE_ENABLED", True)
MODE = os.getenv("MINUTE_PIPELINE_MODE", "live").strip().lower()
SOURCE_ENV = "mainnet"

SPOT_REST_URL = os.getenv(
    "BINANCE_SPOT_DATA_URL",
    "https://api.binance.com",
).rstrip("/")
FUTURES_REST_URL = os.getenv(
    "BINANCE_FUTURES_DATA_URL",
    "https://fapi.binance.com",
).rstrip("/")
ALPHA_REST_URL = "https://www.binance.com"

SPOT_WS_URL = os.getenv(
    "MINUTE_SPOT_WS_URL",
    "wss://stream.binance.com:9443/ws",
)
FUTURES_WS_URL = os.getenv(
    "MINUTE_FUTURES_WS_URL",
    "wss://fstream.binance.com/ws",
)

BOOTSTRAP_MINUTES = max(
    0,
    int(os.getenv("MINUTE_BOOTSTRAP_MINUTES", "180")),
)
REST_CONCURRENCY = max(
    1,
    int(os.getenv("MINUTE_REST_CONCURRENCY", "8")),
)
ALPHA_CONCURRENCY = max(
    1,
    int(os.getenv("MINUTE_ALPHA_CONCURRENCY", "4")),
)
ALPHA_OFFSET_SECONDS = max(
    1,
    int(os.getenv("MINUTE_ALPHA_OFFSET_SECONDS", "3")),
)
WS_RECONNECT_SECONDS = max(
    300,
    int(os.getenv("MINUTE_WS_RECONNECT_SECONDS", "1800")),
)
UNIVERSE_REFRESH_SECONDS = max(
    300,
    int(os.getenv("MINUTE_UNIVERSE_REFRESH_SECONDS", "1800")),
)
RETENTION_DAYS = max(
    1,
    int(os.getenv("MINUTE_RETENTION_DAYS", "4")),
)
GAP_REPAIR_INTERVAL_SECONDS = max(
    15,
    int(os.getenv("MINUTE_GAP_REPAIR_INTERVAL_SECONDS", "30")),
)
FUTURES_FALLBACK_AFTER_SECONDS = max(
    30,
    int(os.getenv("MINUTE_FUTURES_FALLBACK_AFTER_SECONDS", "90")),
)
