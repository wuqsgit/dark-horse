from __future__ import annotations

import json
from pathlib import Path


def replay_report(result: dict) -> dict:
    rows = result.get("rows") or []
    transitions = [
        {
            "time": row.get("candle_close_time"),
            "from": row.get("from_state"),
            "to": row.get("to_state"),
            "action": row.get("action_type"),
            "setup": row.get("setup_type"),
            "prediction": row.get("prediction"),
            "label": row.get("label"),
        }
        for row in rows
        if row.get("changed") or row.get("action_type") != "NONE"
    ]
    return {
        "symbol": result.get("symbol"),
        "market_env": result.get("market_env"),
        "metrics": result.get("metrics") or {},
        "transition_count": len(transitions),
        "transitions": transitions,
    }


def write_replay_report(result: dict, output_path: str) -> str:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(replay_report(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(target)
