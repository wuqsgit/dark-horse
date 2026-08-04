#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest.alpha_strategy_v2.feature_source import SQLiteReplayFeatureSource
from backtest.alpha_strategy_v2.replay import AlphaStrategyReplay
from backtest.alpha_strategy_v2.reports import replay_report, write_replay_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay Alpha Strategy V2 with strict event-time inputs.",
    )
    parser.add_argument("symbol", help="Futures symbol, for example AKEUSDT")
    parser.add_argument("--start", required=True, help="UTC/ISO start time")
    parser.add_argument("--end", required=True, help="UTC/ISO end time")
    parser.add_argument(
        "--market-env",
        choices=("testnet", "mainnet"),
        default="mainnet",
    )
    parser.add_argument("--alpha-symbol")
    parser.add_argument(
        "--db",
        default=os.path.join(ROOT, "alphadog.db"),
    )
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()

    source = SQLiteReplayFeatureSource(args.db)
    inputs = source.load(
        futures_symbol=args.symbol,
        market_env=args.market_env,
        start=args.start,
        end=args.end,
        alpha_symbol=args.alpha_symbol,
    )
    result = AlphaStrategyReplay().run(**inputs)
    report = replay_report(result)
    if args.output:
        write_replay_report(result, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
